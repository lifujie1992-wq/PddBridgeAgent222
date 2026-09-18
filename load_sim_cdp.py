# -*- coding: utf-8 -*-
"""CDP 模式进线压测：对比「页面直推(--transport push)」与「缓冲+200ms 轮询(--transport drain)」。

链路（除页面/中心两处桩以外全是生产代码）：
    合成页面帧 ──真实 PddbridgeSource(注入存活判定/多会话/推送通道)──> 真实 BridgeAgent
      ├─ 真实 LocalDeliveryQueue ──HTTP──> 本机真实网关(FrontHandler)
      └─ 真实 _pending/_flush_events ──> 桩中心（只记到达时间）
    订单上下文查询用等延迟桩（--lookup-ms）替代，它要占真实工作台的 CDP。

跑法:
    python load_sim_cdp.py --transport push  --rate 300 --minutes 1
    python load_sim_cdp.py --transport drain --rate 300 --minutes 1     # 旧行为对照
    python load_sim_cdp.py --transport push  --rate 600 --shops 3 --minutes 1

输出「页面→本机管线」「页面→中心」两段延迟分位、链路计数与丢弃计数。
页面/中心都是桩，不会碰真实工作台，也不会发出任何真实消息。
"""
from __future__ import annotations

import argparse
import json
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path

import bridge.pddbridge_source as pds
import bridge.pdd_context as pdd_context
import run_pdd_client
from bridge.agent import BridgeAgent
from bridge.pdd_context import normalize_order_response
from run_frontend_service import FrontHandler, LocalSeatState, WEB

SHOP_ACCOUNTS = ["cs_427302374:164945148", "cs_427302375:164945149", "cs_427302376:164945150",
                 "cs_427302377:164945151"]
BUYER_ID = "4764375385604"


def frame_json(msg_id: str, ts_ms: int, account: str) -> str:
    mall, uid = account.split(":")[0].replace("cs_", ""), account.split(":")[1]
    return json.dumps({
        "response": "push",
        "message": {
            "from": {"role": "user", "uid": BUYER_ID},
            "to": {"role": "mall_cs", "uid": uid},
            "msg_id": msg_id,
            "content": f"压测消息{msg_id}",
            "type": 0,
            "ts": ts_ms,
            "nickname": "压测买家",
        },
        "target_id": int(mall),
    }, ensure_ascii=False)


class RecordingCenter:
    """桩中心：只记到达时间，不模拟大脑侧队列。"""

    def __init__(self, created: dict, lock: threading.Lock) -> None:
        self.created = created
        self.lock = lock
        self.arrived: dict[str, float] = {}

    def server_now(self) -> float:
        return time.time()

    def register(self) -> dict:
        return {}

    def heartbeat(self, _status: dict) -> dict:
        return {}

    def pull_commands(self, *, wait_seconds: float = 0.0) -> list:
        return []

    def upload_events(self, events: list) -> dict:
        now = time.time()
        with self.lock:
            for event in events:
                msg_id = str(event.get("platform_message_id") or event.get("msg_id") or "")
                if msg_id:
                    self.arrived.setdefault(msg_id, now)
        return {
            "ack_version": 1,
            "event_acks": [
                {"event_id": event.get("event_id"), "status": "accepted",
                 "committed": True, "retryable": False}
                for event in events
            ],
        }

    def written_at(self, msg_id: str) -> float:
        with self.lock:
            return float(self.created.get(msg_id) or 0.0)


class FakePage:
    """假页面：实现真实注入脚本对外的 drain 契约，并可选承载账号信息。"""

    def __init__(self, account: str) -> None:
        self.account = account
        self.frames: list[dict] = []
        self.lock = threading.Lock()

    def buffer_frame(self, entry: dict) -> None:
        with self.lock:
            self.frames.append(entry)

    def eval(self, expression, context_id=None):
        if "__pddBridge_push_url =" in expression:
            return {"ok": True, "attached": 1}
        if "__pddBridge_hooked" in expression:
            with self.lock:
                frames, self.frames = self.frames, []
            return {"hooked": True, "frames": frames, "stats": None}
        if "__pddBridge_info" in expression:
            mall = self.account.split(":")[0].replace("cs_", "")
            return {"csidGuess": self.account, "globalMallId": mall}
        return None

    def close(self) -> None:
        pass


def build_gateway(work: Path, shop_ids: list[str]):
    state = LocalSeatState(work / "seat_state.json")
    state.configure(backend="http://127.0.0.1:1", agent_token="sim-token", agent_id="sim-agent")
    with state.lock:
        state.active_shop_ids = {"mall_" + shop for shop in shop_ids}
        state.shop_names = {"mall_" + shop: "压测店铺" + shop for shop in shop_ids}
        state._save_locked()

    class Handler(FrontHandler):
        local_state = state
        ui_role = "seat"
        seat_agent_token = "sim-token"
        seat_agent_id = "sim-agent"
        backend_base = "http://127.0.0.1:1"
        web_root = WEB

        def log_message(self, _fmt, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def install_lookup_stub(lookup_ms: int) -> None:
    # 继承真实类，只把“真正查一次”换成等延迟桩 → 结果缓存/并发池是真实代码。
    real = pdd_context.PddOrderContextLookup

    class StubLookup(real):
        def _lookup_uncached(self, mall_id, buyer_id):
            if lookup_ms:
                time.sleep(lookup_ms / 1000.0)
            return normalize_order_response(mall_id, {"ok": True, "total": 0, "orders": []})

    pdd_context.PddOrderContextLookup = StubLookup


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def line(values_ms: list[float]) -> str:
    if not values_ms:
        return "无样本"
    return ("p50=%.1fms p95=%.1fms p99=%.1fms max=%.1fms avg=%.1fms"
            % (percentile(values_ms, .5), percentile(values_ms, .95), percentile(values_ms, .99),
               max(values_ms), sum(values_ms) / len(values_ms)))


def main() -> int:
    parser = argparse.ArgumentParser(description="CDP 模式进线压测（推送 vs 轮询）")
    parser.add_argument("--transport", choices=("push", "drain"), default="push")
    parser.add_argument("--rate", type=int, default=300, help="每分钟进线条数（全部店铺合计）")
    parser.add_argument("--minutes", type=float, default=1.0, help="压测时长")
    parser.add_argument("--shops", type=int, default=1, help="并发店铺/账号数")
    parser.add_argument("--workers", type=int, default=8, help="推送并发连接数")
    parser.add_argument("--lookup-ms", type=int, default=0, help="桩订单查询耗时(毫秒)")
    parser.add_argument("--drain-seconds", type=float, default=60.0, help="停止进线后等排空的时间")
    args = parser.parse_args()

    created: dict[str, float] = {}
    lock = threading.Lock()
    work = Path(tempfile.mkdtemp(prefix="load-cdp-"))
    shops = min(args.shops, len(SHOP_ACCOUNTS))
    shop_ids = [SHOP_ACCOUNTS[i].split(":")[0].replace("cs_", "") for i in range(shops)]
    server = build_gateway(work, shop_ids)
    local_url = f"http://127.0.0.1:{server.server_port}"
    install_lookup_stub(args.lookup_ms)
    run_pdd_client.install_local_first()

    # 绝不碰真实工作台: 端口发现置空, 只挂我们自己的假页面
    pds.pdd_cdp.discover_ports = lambda only_pdd=False: {}
    pds.pdd_cdp.is_alive = lambda port: True

    cfg = {
        "platform": "pdd", "server_url": "http://127.0.0.1:1", "agent_token": "sim-token",
        "agent_id": "sim-agent", "agent_name": "load-sim-cdp", "device_id": "device-sim",
        "data_source": "cdp", "cdp_auto_fallback": False, "cdp_rescan_seconds": 3600,
        "tanyu_log_dir": str(work / "tanyu_logs"),
        "local_queue_path": str(work / "bridge_queue.jsonl"),
        "command_journal_path": str(work / "bridge_commands.json"),
        "local_workbench_url": local_url, "dual_write_local_workbench": True,
        "parser_profile_cache_path": str(work / "parser_profile.json"),
        "heartbeat_seconds": 5.0, "command_poll_seconds": 1.5,
    }
    agent = BridgeAgent(cfg)
    agent.client = RecordingCenter(created, lock)
    threading.Thread(target=lambda: agent.run_forever(), daemon=True, name="agent-run").start()
    time.sleep(2.0)

    source = agent.pddbridge_source
    if source is None:
        print("agent 没起 CDP 源, 检查 data_source")
        return 2



    pages: list[FakePage] = []
    for index in range(shops):
        page = FakePage(SHOP_ACCOUNTS[index])
        session = pds._Session(57165 + index, "sim-shop-%d" % index, page, 1)
        session.account = SHOP_ACCOUNTS[index]
        session.mall_id = SHOP_ACCOUNTS[index].split(":")[0].replace("cs_", "")
        page.session = session
        pages.append(page)
        source.sessions.append(session)
    source._set_state(pds.LISTENING)

    hop: list[float] = []      # 页面生成 → 本机管线 on_event
    original_emit = source.on_event

    def on_event(message: dict) -> None:
        msg_id = str(message.get("msg_id") or "")
        with lock:
            born = created.get(msg_id)
        if born:
            hop.append((time.time() - born) * 1000.0)
        original_emit(message)

    source.on_event = on_event
    time.sleep(0.5)

    push_url = source._push_url()
    print("=" * 78)
    print(f"transport={args.transport}  shops={shops}  rate={args.rate}/分钟  "
          f"时长={args.minutes}分钟  订单查询桩={args.lookup_ms}ms  推送通道={push_url}")

    total = max(1, int(args.rate * args.minutes))
    per_line = 60.0 / max(1, args.rate)
    executor = ThreadPoolExecutor(max_workers=args.workers)
    counter = {"n": 0}

    def fire(index: int) -> None:
        page = pages[index % len(pages)]
        msg_id = "sim-%d" % index
        ts_ms = int(time.time() * 1000)
        born = time.time()
        with lock:
            created[msg_id] = born
        payload = frame_json(msg_id, ts_ms, page.account)
        if args.transport == "push":
            body = json.dumps({"sid": "%s|1" % page.session.port, "t": ts_ms,
                               "dir": "in", "data": payload}).encode()
            try:
                urllib.request.urlopen(urllib.request.Request(
                    push_url, data=body, headers={"content-type": "application/json"}), timeout=10)
            except Exception as exc:
                print("推送失败:", exc)
        else:
            page.buffer_frame({"sid": "%s|1" % page.session.port, "t": ts_ms,
                               "dir": "in", "data": payload})

    started = time.time()
    while counter["n"] < total and time.time() - started < args.minutes * 60:
        slot = time.time()
        executor.submit(fire, counter["n"])
        counter["n"] += 1
        wait = per_line - (time.time() - slot)
        if wait > 0:
            time.sleep(wait)
    write_finished = time.time()

    deadline = time.time() + args.drain_seconds
    while time.time() < deadline:
        try:
            local_pending = int(agent._local_delivery.status().get("pending") or 0)
        except Exception:
            local_pending = 0
        if not agent._pending and not local_pending and not source.sessions[0].cdp.frames:
            break
        time.sleep(0.2)
    drained = time.time()
    executor.shutdown(wait=True)
    status = source.status()
    source.stop()
    agent._stop.set()
    time.sleep(1.0)
    server.shutdown()
    server.server_close()

    center = agent.client
    e2e = []
    missing = 0
    for msg_id, arrived_at in center.arrived.items():
        born = center.written_at(msg_id)
        if born:
            e2e.append((arrived_at - born) * 1000.0)
        else:
            missing += 1
    elapsed = max(0.001, write_finished - started)

    print(f"进线 {counter['n']} 条 / {elapsed:.1f}s   排空 {drained - write_finished:.1f}s")
    print(f"链路计数: 页面生成 {counter['n']} → 本机管线 {len(hop)} → 中心收到 {len(center.arrived)}"
          f"（管线丢 {max(0, counter['n'] - len(hop))}，上报丢 "
          f"{max(0, len(hop) - len(center.arrived))}）")
    print(f"页面→本机管线: {line(hop)}")
    print(f"页面→中心    : {line(e2e)}")
    print(f"吞吐 {len(center.arrived) * 60 / elapsed:.0f}/分钟   未匹配 {missing}")
    print(f"会话数={len(status['sessions'])}  账号={status['accounts']}")
    print(f"推送通道: {json.dumps(status['push'], ensure_ascii=False)}")
    print(f"丢弃计数: {json.dumps({k: v for k, v in status['drops'].items() if v}, ensure_ascii=False)}")
    print(f"本地队列残留={agent._local_delivery.status().get('pending')}  "
          f"上传 last_error={agent._last_error!r}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
