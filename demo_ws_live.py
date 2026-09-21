# -*- coding: utf-8 -*-
"""本地端到端演示：真实 bridge 代码 + 假大脑（HTTP 18900 / WS 18901）。

**不连生产中心**。演示链路：
    合成探域日志 -> LogWatcher -> parser -> BridgeAgent._flush_events
        -> center_ws.send_events -> 假大脑 ack/result -> 出队/落死信

看的是新版 WS 上行：帧内容、ack、result、出队、状态计数。
运行：python demo_ws_live.py [秒数]
"""
import asyncio
import json
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import websockets

HTTP_PORT = 18900
WS_PORT = 18901

RECEIVED = []          # 假大脑收到的 event 帧
SENT_RESULT = []       # 已回 result 的 event_id
LOCK = threading.Lock()


# ----------------------------------------------------------------- 假大脑 HTTP
class FakeHTTP(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, payload, code=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {}
        path = self.path.split("?", 1)[0]
        if path.endswith("/register"):
            self._json({"ok": True, "agent_id": payload.get("agent_id")})
        elif path.endswith("/heartbeat"):
            self._json({"ok": True})
        elif path.endswith("/result"):
            self._json({"ok": True, "command_ack": {"committed": True}})
        else:
            self._json({"ok": True})

    def do_GET(self):
        if "/commands" in self.path:
            self._json({"ok": True, "commands": []})
        else:
            self._json({"ok": True})


def start_http_brain():
    server = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), FakeHTTP)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ------------------------------------------------------------------- 假大脑 WS
async def ws_handler(ws):
    await ws.send(json.dumps({
        "type": "ready", "connection_id": "demo-conn",
        "max_inflight": 64, "protocol_version": 1, "agent_id": "demo-agent",
    }))
    log("WS", "已握手，收到 ready（protocol_version=1）")
    async for raw in ws:
        try:
            frame = json.loads(raw)
        except ValueError:
            continue
        if frame.get("type") != "event":
            continue
        with LOCK:
            RECEIVED.append(frame)
            index = len(RECEIVED)
        log("WS", "收到 event #%d  event_id=%s  account=%s  buyer=%s  role=%s  content=%r"
            % (index, frame.get("event_id"), frame.get("account"),
               frame.get("buyer_id"), frame.get("role"), (frame.get("content") or "")[:24]))
        await ws.send(json.dumps({
            "type": "ack", "event_id": frame["event_id"],
            "status": "accepted", "server_received_at_ms": int(time.time() * 1000),
        }))
        # 每 3 条回一条业务终态 result，演示 result 路径
        if index % 3 == 0:
            await ws.send(json.dumps({
                "type": "result", "event_id": frame["event_id"],
                "status": "processed", "error_code": "",
                "server_processed_at_ms": int(time.time() * 1000),
            }))


def start_ws_brain():
    loop = asyncio.new_event_loop()

    def run():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(websockets.serve(ws_handler, "127.0.0.1", WS_PORT,
                                                 ping_interval=None))
        loop.run_forever()

    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------------- 工具
def log(tag, message):
    # 控制台默认 GBK，中文/符号混排会炸；直接按 UTF-8 输出，装不下就替换。
    try:
        print("%s [%s] %s" % (time.strftime("%H:%M:%S"), tag, message), flush=True)
    except UnicodeEncodeError:
        print(("%s [%s] %s" % (time.strftime("%H:%M:%S"), tag, message))
              .encode("utf-8", "replace").decode("utf-8", "replace"), flush=True)


def write_config(root: Path) -> Path:
    cfg_path = root / "bridge_config.json"
    cfg_path.write_text(json.dumps({
        "platform": "pdd",
        "server_url": "http://127.0.0.1:%d" % HTTP_PORT,
        "center_ws_enabled": True,
        "center_ws_url": "ws://127.0.0.1:%d/api/bridge/v1/ws" % WS_PORT,
        "agent_token": "demo-token",
        "agent_id": "demo-agent",
        "agent_name": "demo-seat",
        "data_source": "tanyu_logs",
        "tanyu_log_dir": str(root / "logs"),
        "local_workbench_url": "http://127.0.0.1:18767",
        "local_queue_path": str(root / "bridge_queue_pdd.jsonl"),
        "command_journal_path": str(root / "bridge_commands_pdd.json"),
        "heartbeat_seconds": 5,
        "poll_interval_ms": 200,
        "upload_batch_size": 50,
        "local_first_max_wait_seconds": 0.2,
        "delivery_ledger_path": str(root / "bridge_ledger.jsonl"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg_path


def inject(path: Path, index: int, content: str):
    message = {
        "platform": "pdd", "msg_id": "demo-%d" % index,
        "platform_message_id": "demo-%d" % index, "identity_kind": "message_id",
        "account": "cs_100000003:200000001", "buyer_id": "3000000000001",
        "buyer_nick": "测试买家", "shop_name": "演示店铺",
        "role": "user", "content": content, "ts": int(time.time()),
        "raw_type": 0,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write("buyer_msg=" + json.dumps(message, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------- 主流程
def start_gateway(cfg_path: Path):
    """起真实本地网关（web UI + 本地坐席），绑到演示配置。"""
    import subprocess
    exe = sys.executable
    here = Path(__file__).resolve().parent
    proc = subprocess.Popen(
        [exe, str(here / "run_frontend_service.py"),
         "--host", "127.0.0.1", "--port", "18767",
         "--ui-role", "seat", "--web-dir", str(here / "web"),
         "--bridge-config", str(cfg_path)],
        cwd=str(here),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        time.sleep(0.25)
        try:
            with urllib.request.urlopen("http://127.0.0.1:18767/api/health", timeout=1):
                pass
            return proc
        except Exception:  # noqa: BLE001
            if proc.poll() is not None:
                return None
    return proc


def main():
    try:  # 让中文输出不依赖控制台代码页
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    serve = "--serve" in sys.argv
    argv_nums = [a for a in sys.argv[1:] if not a.startswith("-")]
    seconds = int(argv_nums[0]) if argv_nums else 30
    root = Path(tempfile.mkdtemp(prefix="pba_demo_"))
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log_file = logs / "inside_demo.log"
    log_file.write_text("", encoding="utf-8")

    log("SETUP", "演示目录 %s" % root)
    cfg_path = write_config(root)
    log("SETUP", "配置：WS 已开启 -> ws://127.0.0.1:%d/api/bridge/v1/ws" % WS_PORT)

    start_http_brain()
    start_ws_brain()
    time.sleep(0.5)
    log("SETUP", "假大脑已起：HTTP :%d / WS :%d（均只在本机，不连生产中心）"
        % (HTTP_PORT, WS_PORT))

    from bridge.config import load_config
    from run_pdd_client import install_local_first
    install_local_first()
    from bridge.agent import BridgeAgent

    cfg = load_config(cfg_path)
    agent = BridgeAgent(cfg)
    log("SETUP", "WS 通道已创建：%s" % (agent.center_ws.ws_url if agent.center_ws else "无"))

    thread = threading.Thread(target=agent.run_forever, name="demo-agent", daemon=True)
    thread.start()

    # 等 WS 连上
    deadline = time.time() + 10
    while time.time() < deadline and not (agent.center_ws and agent.center_ws.available):
        time.sleep(0.1)
    if agent.center_ws and agent.center_ws.available:
        log("WS", "已连接 [OK]")
    else:
        log("WS", "未连上 [FAIL] %s"
            % (agent.center_ws.last_error if agent.center_ws else "通道未建"))
        return 1

    gateway = None
    if serve:
        gateway = start_gateway(cfg_path)
        if gateway is not None:
            log("SETUP", "本地网关已起：http://127.0.0.1:18767/  （浏览器打开就能看到坐席界面）")
        else:
            log("WARN", "本地网关未起来，界面不可用；链路演示不受影响")

    log("DEMO", "开始注入买家消息（真实链路：日志->解析->事件->WS 上行）")
    samples = [
        "你好，这件衣服有货吗？",
        "我要退货，订单号 240001234567890123",
        "什么时候发货",
        "尺码偏小，能换货吗",
        "12345678901234567",
        "收到货了，谢谢",
    ]
    if serve:
        log("DEMO", "常驻模式：持续注入，Ctrl+C 结束")
        samples = (samples * 1000)
    for i, text in enumerate(samples):
        inject(log_file, i, text)
        log("LOG", "写入探域日志第 %d 条：%r" % (i + 1, text))
        time.sleep(2.0)
        with LOCK:
            got = len(RECEIVED)
        st = agent.center_ws.status()
        log("STATUS", "已发 %d 帧 / 已收 %d ack / %d result / 队列剩余 %d"
            % (st["frames_sent"], st["acks_received"], st["results_received"],
               len(agent._pending)))
        if got < i + 1:
            log("WARN", "第 %d 条还没到 WS（可能仍在本地优先宽限/批量窗口）" % (i + 1))

    log("DEMO", "等待排空……")
    time.sleep(max(2.0, seconds - len(samples) * 2.0))
    st = agent.center_ws.status()
    with LOCK:
        received = list(RECEIVED)
    print()
    print("=" * 72)
    print("结果")
    print("=" * 72)
    print("WS 状态      : %s" % json.dumps(
        {k: st[k] for k in ("connected", "frames_sent", "acks_received",
                            "results_received", "frames_sent")}, ensure_ascii=False))
    print("假大脑收到   : %d 条 event 帧" % len(received))
    print("bridge 队列  : 剩余 %d 条" % len(agent._pending))
    print("每条内容     : %s" % [f["content"] for f in received])
    dead = root / "bridge_queue_pdd_refused.jsonl"
    print("死信文件     : %s" % ("有，%d 行" % len(dead.read_text(encoding='utf-8').splitlines())
                                 if dead.is_file() else "无"))
    print("演示目录     : %s" % root)
    print("=" * 72)
    agent.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
