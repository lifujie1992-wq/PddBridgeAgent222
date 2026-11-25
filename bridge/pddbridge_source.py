# -*- coding: utf-8 -*-
"""PddbridgeSource —— PDD 实时收发数据源适配器（CDP 注入，脱离探域）

把独立 pddbridge 的 CDP 实时捕获能力融合进桥接助手：
- 单守护线程独占 Cdp 连接，所有 CDP eval 串行（drain/发送/__pddBridge_info），
  动作队列投递，避免并发 eval 按 id 吞包，也满足「同一时刻一个 drain 消费者」。
- scanning → listening 状态机；scanning 超过 cdp_fallback_after_seconds 且
  cdp_auto_fallback 时触发 on_fallback 回调（切回探域日志），本线程结束。
- 帧经 protocol.parse_frame 归一化成统一 message dict（对齐 parser 输出），
  喂 BridgeAgent._on_local_event。发送回执按 uid + content 前缀关联 pending。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import random
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

from .parser import ACCOUNT_RE, _canonical_account
from .message_timing import promote_fresh_history
from .pddbridge import cdp as pdd_cdp
from .pddbridge.protocol import buyer_message, buyer_uid, messages_from_list, parse_frame, seller_id

log = logging.getLogger("pddbridge_source")

STOPPED = "stopped"
SCANNING = "scanning"
LISTENING = "listening"

INJECT_RESOURCE = "bridge/pddbridge/inject.js"

_DRAIN_BATCH = 800   # 单次取帧上限：小批量 eval 不会超时，且 peek 不删数据

# 取帧时一并回传注入存活状态与注入层计数。
# 优先用无损接口 peek（只读不删，Python 侧处理成功后再调 ack 删除）；
# 老注入脚本没有 peek 时回退到旧的 drain（先清后传，CDP 失败会丢）。
_DRAIN_EXPR = (
    "(function(){return {"
    " hooked: !!window.__pddBridge_hooked,"
    " attached: (typeof window.__pddBridge_attached === 'undefined' || !!window.__pddBridge_attached),"
    " lossless: (typeof window.__pddBridge_peek === 'function'),"
    " frames: (typeof window.__pddBridge_peek === 'function')"
    "         ? window.__pddBridge_peek(%d)"
    "         : (window.__pddBridge_drain ? window.__pddBridge_drain() : null),"
    " pending: (window.__pddBridge_pending ? window.__pddBridge_pending() : null),"
    " stats: (window.__pddBridge_stats ? window.__pddBridge_stats() : null)};})()" % _DRAIN_BATCH
)
_ACK_EXPR = "(window.__pddBridge_ack ? window.__pddBridge_ack(%d) : -1)"


def _resource_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", ".")) / INJECT_RESOURCE
    return Path(__file__).resolve().parent / "pddbridge" / "inject.js"


def _load_inject() -> str:
    return _resource_path().read_text(encoding="utf-8")


def _stable_hash(obj) -> str:
    raw = json.dumps(obj, ensure_ascii=False, default=str, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _is_fallback_id(value) -> bool:
    """
    True when ``value`` is the local content+time hash used as a msg_id
    fallback (24 hex chars), not a real platform message id.

    The fallback can collide for identical text sent within the same second, so
    it must never be used as a dedup key -- doing so silently drops real buyer
    messages.
    """
    text = str(value or "")
    return len(text) == 24 and all(ch in "0123456789abcdef" for ch in text)


class _SendWaiter(threading.Event):
    result = None
    cancelled = False
    is_expired = None
    deadline = 0.0

    def expired(self):
        return (self.cancelled or time.monotonic() >= self.deadline
                or (callable(self.is_expired) and self.is_expired()))


def _to_buyer_uid(parsed) -> str:
    """send_message 回执帧里的买家 uid（不能是席位自己的 uid，否则会造影子会话）。"""
    msg = parsed.get("obj") or {}
    message = msg.get("message") or {}
    return (buyer_uid([message.get("to"), message.get("from"), msg.get("to")], seller_id(parsed) or "")
            or str(parsed.get("uid") or ""))


def _seat_uid(seller) -> str:
    """客服账号 cs_<mall>:<uid> 里的 uid。"""
    return str(seller or "").partition(":")[2]


class _Session:
    """一个页面上下文 = 一个账号/店铺的实时通道（多店铺就是多个会话）。

    0.7.8：每个会话自带一把锁和一个动作队列——所有 CDP eval 都过同一把锁
    （websocket-client 不是线程安全的），拉历史/发送不再阻塞其他店铺。
    """

    __slots__ = ("port", "target_id", "target_url", "cdp", "cid", "account", "mall_id",
                 "last_health", "push_ok", "frames", "socket_state", "send_capable",
                 "lock", "actions", "worker", "failures", "next_attempt", "next_health",
                 "last_frame_at", "degraded_at", "sender_csid", "sender_csid_at", "last_batch",
                 "evals", "eval_ms_total", "eval_ms_max")

    def __init__(self, port: int, target_url: str, cdp, cid: int, target_id: str = ""):
        self.port = port
        self.target_id = target_id or ""
        self.target_url = target_url or ""
        self.cdp = cdp
        self.cid = cid
        self.account = ""
        self.mall_id = ""
        self.last_health = 0.0
        self.push_ok = False
        self.frames = 0
        self.socket_state = "unknown"
        self.send_capable = False
        self.lock = threading.Lock()
        self.actions: "queue.Queue" = queue.Queue()
        self.worker = None
        self.failures = 0
        self.next_attempt = 0.0
        self.next_health = 0.0
        self.last_frame_at = 0.0
        self.last_batch = 0        # 上一轮取到几帧（worker 用它决定活跃/空闲节奏）
        self.evals = 0             # 本店累计 eval 次数
        self.eval_ms_total = 0.0   # 本店累计 eval 耗时（秒）
        self.eval_ms_max = 0.0     # 本店单次最慢 eval（秒）
        self.degraded_at = 0.0
        self.sender_csid = ""
        self.sender_csid_at = 0.0

    def key(self) -> str:
        # 多个 middle_panel 目标可能共用同一个 executionContextId 和 URL（实测 7 个目标
        # cid 都是 11），只用 port|cid|url 会把它们当成同一个会话而跳过。必须带唯一 target id。
        return "%s|%s|%s" % (self.port, self.target_id or self.cid, self.target_url)

    def label(self) -> str:
        return "port=%s ctx=%s account=%s" % (self.port, self.cid, self.account or "?")

    def close(self) -> None:
        try:
            self.cdp.close()
        except Exception:
            pass


class _PushServer:
    """注入脚本的推送入口（127.0.0.1 随机端口, token 在路径里）。

    页面用 fetch(POST) 把每条帧直接推进来, 绕开 200ms 轮询和注入层缓冲上限；
    页面侧推不出去时会留在缓冲里由 drain 兜底, 所以既不重复也不丢。
    收到后只入队, 帧处理仍然在主循环线程里串行做。
    """

    def __init__(self, on_payload=None, maxsize: int = 50000):
        self.token = secrets.token_hex(16)
        self.on_payload = on_payload
        self.queue: "queue.Queue[dict]" = queue.Queue(maxsize=maxsize)
        self.received = 0
        self.dropped = 0
        self.rejected = 0
        self.httpd = None
        self.thread = None
        self.port = None

    def start(self) -> int:
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _cors(self) -> None:
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "content-type")
                self.send_header("Access-Control-Max-Age", "600")

            def _end(self, code: int) -> None:
                self.send_response(code)
                self._cors()
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_OPTIONS(self):  # noqa: N802 (CORS 预检)
                if urlsplit(self.path).path.rstrip("/") == "/push/" + server.token:
                    self._end(204)
                else:
                    self._end(404)

            def do_POST(self):  # noqa: N802
                if urlsplit(self.path).path.rstrip("/") != "/push/" + server.token:
                    server.rejected += 1
                    self._end(404)
                    return
                try:
                    length = int(self.headers.get("content-length") or 0)
                except (TypeError, ValueError):
                    length = 0
                body = self.rfile.read(length) if length > 0 else b""
                entries = server._parse(body)
                if server.queue.qsize() + len(entries) > server.queue.maxsize:
                    server.dropped += len(entries)
                    self._end(503)
                    return
                server.received += 1
                for entry in entries:
                    server.queue.put_nowait(entry)
                if callable(server.on_payload):
                    server.on_payload()   # 唤醒主循环, 不然推送也要等 200ms 轮询
                self._end(200)

            def log_message(self, *args):  # 静默: 别把每条帧都写进 stderr
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = int(self.httpd.server_address[1])
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True,
                                       name="pddbridge-push")
        self.thread.start()
        return self.port

    def stop(self) -> None:
        httpd, self.httpd = self.httpd, None
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:
                pass

    def drain(self, limit: int = 2000) -> list:
        out: list = []
        while len(out) < limit:
            try:
                out.append(self.queue.get_nowait())
            except queue.Empty:
                break
        return out

    @staticmethod
    def _parse(body: bytes) -> list:
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except Exception:
            return []
        if isinstance(data, dict):
            inner = data.get("entries")
            data = inner if isinstance(inner, list) else [data]
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]


class PddbridgeSource:
    """PDD CDP 实时收发数据源。跑在独立后台线程, 归一化消息经 on_event 回调输出。"""

    def __init__(self, cfg: dict | None = None, on_event=None, on_fallback=None, on_recover=None):
        self.cfg = cfg or {}
        self.on_event = on_event
        self.on_fallback = on_fallback
        self.state = STOPPED
        self.sessions: list = []   # 多店铺 = 多个 _Session, 每个一条 CDP 连接
        self.port = None           # 主会话端口（兼容旧字段）
        self._account_hint = ""
        self.msg_count = 0
        # 采集可观测性：注入成功但零帧是"假正常"的最大来源
        # （现场：socket=none / 探域没跑 → 全程 pending=0，而界面一切正常）
        self._frame_stamps: "deque[float]" = deque(maxlen=20000)
        # Phase-3：卖家最后一次回复时间（按店铺+买家），用于判断"这条还没人回过"
        self._last_seller_ts: dict = {}
        # 已经以 history 身份发出去的消息（msg_id -> 时间），用于 push 后到时升级
        self._history_emitted: dict = {}
        self._last_frame_at = 0.0
        self._frames_seen = 0
        # DLL 直发就绪状态（带缓存：管道是单实例串行，不能每次心跳都去抢）
        self._direct_ready = False
        self._direct_probe_at = 0.0
        self.send_count = 0
        self.last_error = ""

        self._seen: dict = {}    # (kind,msg_id)->ts 去重
        self._seen_any: dict = {}  # msg_id->ts 跨帧去重（push 与 send_message 互去）
        self._pending_sends: dict = {}  # "uid|content前48" -> 记录
        self._act: list = []
        self._lock = threading.Lock()
        self._frame_lock = threading.Lock()   # 多店并行后，帧处理必须串行
        self._stop_evt = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._last_health = 0.0
        self._fallback_done = False
        # 降级不永久化: CDP 修好后（比如重装了正确版本的 PDD 工作台）自动切回。
        # 旧行为是降级后线程直接退出, 客户机只能重启/重装桥接才能恢复 —— 现场高频事故。
        self.on_recover = on_recover
        self._degraded = False
        self._next_retry = 0.0
        self._push = _PushServer(on_payload=self._wake.set)
        # 可选直发通道（默认关）：注入 DLL 就绪时直调工作台内部发送接口，
        # 拿同步返回值；未就绪则返回 None 自动回退原 CDP 路径。
        self.direct_send = None
        # DLL 直发返回 OK 0 后回退页面发送的次数（排查用，进 status）
        self.direct_fallback_count = 0
        self.last_direct_fallback: dict | None = None
        self._inject_watcher = None
        self._native_recv = None
        if bool(self.cfg.get("send_via_dll", False)):
            try:
                from .pdd_send_direct import DirectSender

                self.direct_send = DirectSender(str(self.cfg.get("send_via_dll_pipe") or ""))
            except Exception as exc:
                log.warning("直发通道初始化失败, 继续用 CDP: %s", exc)
        # 各静默丢弃点的计数（原来全是无声的, 只看得到 pending=0）
        self.drops: dict = {
            "hook_lost": 0,           # 页面重载/换页导致注入消失
            "buf_overflow": 0,        # 注入层缓冲溢出丢最老的
            "js_would_drop": 0,       # 页面里旧版包装本会误丢的条数（已被拦下）
            "js_exact_repeat": 0,     # 字节完全相同的重复帧（只计不丢）
            "push_skip": 0,           # 推送通道不可用, 改走缓冲
            "push_retry": 0,          # 推送失败后补进缓冲
            "push_unknown_session": 0,  # 推送帧的会话 id 认不出
            "push_overload": 0,       # 推送队列满
            "spill": 0,               # localStorage spill 写入的帧数
            "spill_replayed": 0,      # localStorage spill 重放的帧数
            "context_unattached": 0,  # 发现上下文但注入失败（该店铺收不到消息）
            "dedup_py": 0,            # Python 侧判重命中（同一条只报一次）
            "unparsed_frame": 0,      # 只有 report_all=false 时才会计数
            "frame_error": 0,         # 帧处理异常
        }
        self._js_stats: dict = {}
        self._eval_count = 0        # 累计 CDP eval 次数（诊断 20 店负载用）
        self._eval_slow = 0         # 耗时超过超时预算一半的次数
        self._warn_at: dict = {}
        # 全部上报: 除「按平台 msg_id 去重」以外不再丢弃任何帧, 回不回由中心(大脑)决定。
        self.report_all = bool(self.cfg.get("report_all", True))
        self.dedup_off = str(self.cfg.get("dedup_mode") or "platform_id").strip().lower() == "off"
        self.reported: dict = {
            "no_buyer": 0,           # 取不到买家 uid: 照报, 带原因
            "seat_uid_as_buyer": 0,  # 帧里只带席位一端: 照报, 带原因
            "system_frame": 0,       # 系统/心跳/协商帧: 照报为诊断事件
            "unknown_kind": 0,       # 未识别帧: 照报为诊断事件
            "empty_frame": 0,        # 空帧: 照报为诊断事件
            "out_direction": 0,      # 自己发出的方向(由回执负责, 不重复报)
            "history_frame": 0,      # 历史消息: 照报(带 is_history)
        }
        queue_path = str(self.cfg.get("local_queue_path") or "bridge_queue.jsonl")
        self.raw_archive_path = Path(str(self.cfg.get("raw_archive_path") or "").strip()
                                     or queue_path.replace(".jsonl", "_frames_raw.jsonl"))
        self._raw_archive_bytes = -1

    # ---------------- 生命周期 ----------------
    def start(self) -> "PddbridgeSource":
        if self._thread and self._thread.is_alive():
            return self
        self._stop_evt.clear()
        self._fallback_done = False
        # 原生注入通道：发送（send_via_dll）或接收（recv_via_dll）任一开启都需要注入
        native_send = bool(self.cfg.get("send_via_dll", False))
        native_recv = bool(self.cfg.get("recv_via_dll", False))
        if native_recv and self._native_recv is None:
            try:
                from .pdd_recv import NativeReceiver

                self._native_recv = NativeReceiver(self.feed_native_frame, self.cfg).start()
            except Exception as exc:
                log.warning("原生接收通道启动失败: %s", exc)
        if (native_send or native_recv) and self._inject_watcher is None:
            try:
                from .pdd_direct_inject import InjectWatcher, ensure_injected

                ensure_injected(self.cfg)          # 尽力同步注入一次（不阻塞太久）
                self._inject_watcher = InjectWatcher(self.cfg)
                self._inject_watcher.start()
            except Exception as exc:
                log.warning("注入器启动失败, 继续用 CDP: %s", exc)
        # 探针/转发配置必须在注入完成后下发（管道由 DLL 创建）; 失败自动重试
        if native_recv and self._native_recv is not None:
            self._apply_native_recv_config()
        if self._push.port is None:
            try:
                log.info("pddbridge 推送通道监听 127.0.0.1:%s", self._push.start())
            except Exception as exc:
                log.warning("推送通道启动失败, 全走 drain 轮询: %s", exc)
        self._thread = threading.Thread(target=self._run, daemon=True, name="pddbridge-cdp")
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop_evt.set()
        self._wake.set()
        if self._inject_watcher is not None:
            try:
                self._inject_watcher.stop()
            except Exception:
                pass
            self._inject_watcher = None
        if self._native_recv is not None:
            try:
                self._native_recv.stop()
            except Exception:
                pass
            self._native_recv = None
        self._close_sessions()
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None
        self._push.stop()
        self.state = STOPPED

    def _direct_send_ready(self) -> bool:
        """注入 DLL 的发送通道是否就绪（`STATE -> READY 1`）。

        页面上没有 JS socket 时（PDD 3.5.0.40 就是这样），`send_capable` 永远是 False，
        但 DLL 直发其实是可用的 —— 界面上却报“工作台发送未就绪”，把人带沟里。
        """
        if not bool(self.cfg.get("send_via_dll", False)):
            return False
        now = time.time()
        if now - self._direct_probe_at < 10.0:
            return self._direct_ready
        self._direct_probe_at = now
        try:
            from .pdd_direct_inject import pipe_state, send_candidates

            for pipe in send_candidates(self.cfg):
                state = pipe_state(pipe)
                if state and state.startswith("READY 1"):
                    self._direct_ready = True
                    return True
            self._direct_ready = False
        except Exception as exc:
            log.debug("直发就绪探测失败: %s", exc)
            self._direct_ready = False
        return self._direct_ready

    def status(self) -> dict:
        now = time.time()
        frames_5m = sum(1 for ts in self._frame_stamps if now - ts <= 300.0)
        idle = (now - self._last_frame_at) if self._last_frame_at else None
        return {
            "state": self.state,
            "port": self.sessions[0].port if self.sessions else None,
            "injected": bool(self.sessions),
            "send_ready": ((self.state == LISTENING and any(s.send_capable for s in self.sessions))
                            or self._direct_send_ready()),
            "page_send_ready": (self.state == LISTENING
                                and any(s.send_capable for s in self.sessions)),
            "dll_send_ready": self._direct_ready,
            "dll_send": (dict(self.direct_send.stats(),
                              fallback_to_page=self.direct_fallback_count,
                              last_fallback=self.last_direct_fallback)
                         if self.direct_send is not None else None),
            "msg_count": self.msg_count,
            "send_count": self.send_count,
            # 采集是否真的在跑（injected 只代表注入成功，不代表有数据）
            "frames_total": self._frames_seen,
            "frames_last_5m": frames_5m,
            "last_frame_at": self._last_frame_at,
            "frame_idle_seconds": idle,
            "capture_ok": bool(self.sessions) and frames_5m > 0,
            "sessions": [
                {"port": s.port, "ctx": s.cid, "account": s.account, "mall_id": s.mall_id,
                 "push": s.push_ok, "frames": s.frames, "url": (s.target_url or "")[:80],
                 "socket_state": s.socket_state, "failures": s.failures,
                 "last_frame_at": s.last_frame_at}
                for s in self.sessions
            ],
            "accounts": [s.account for s in self.sessions if s.account],
            "push_port": self._push.port,
            "push": {"received": self._push.received, "dropped": self._push.dropped,
                     "queue": self._push.queue.qsize()},
            "report_all": self.report_all,
            "dedup_mode": "off" if self.dedup_off else "platform_id",
            "reported": dict(self.reported),
            "drops": dict(self.drops),
        }

    def diagnostics(self) -> dict:
        return {
            "source": "pdd_cdp",
            "state": self.state,
            "degraded": self._degraded,
            "port": self.sessions[0].port if self.sessions else None,
            "injected": bool(self.sessions),
            "sessions": len(self.sessions),
            "msg_count": self.msg_count,
            "send_count": self.send_count,
            "last_error": self.last_error,
            "report_all": self.report_all,
            "dedup_mode": "off" if self.dedup_off else "platform_id",
            "reported": dict(self.reported),
            "raw_archive": str(self.raw_archive_path),
            "drops": dict(self.drops),
        }

    # ---------------- 动作投递（线程安全） ----------------
    def pull_history(self, uid, account="", *, size: int | None = None, begin_msg_id=0,
                     start_index: int = 0, pre_msg_id=0, timeout: float = 5.0) -> dict:
        """直接按买家 uid 拉历史（服务端分页）。

        比走 open_chat 让页面自己加载快得多：不切客服界面焦点、页大小自己定（默认 100，
        页面自带的是 20）、也不用等 UI 渲染。应答是 cmd:"list" 帧，走既有帧流由
        messages_from_list 归一化上报，不多发任何消息。
        """
        if not self._thread or not self._thread.is_alive():
            return {"ok": False, "status": "failed", "via": "cdp_history",
                    "error_user": "CDP 通道未启动，请确认 PDD 商家工作台已打开"}
        page = int(size if size is not None else (self.cfg.get("history_page_size") or 100))
        page = max(1, min(page, 200))          # 服务端有上限，贪多会被拒
        event = _SendWaiter()
        event.deadline = time.monotonic() + timeout
        with self._lock:
            self._act.append(("pull_history",
                              (str(uid), str(account or ""), page, str(begin_msg_id or 0),
                               int(start_index or 0), str(pre_msg_id or 0), event)))
        event.wait(timeout)
        if event.result is None:
            return {"ok": False, "status": "timeout", "via": "cdp_history",
                    "error_user": "拉取历史超时（页面未响应）"}
        return event.result

    def send_image(self, uid, image_path, account, *, csid=None) -> dict:
        """发本地图片（DLL SendImageMsg）。没有页面兼容路径，不可用就明确报错。"""
        if self.direct_send is not None:
            # 和文字发送同一口径补客服身份：DLL 拿不到 sender label 就返回 OK 0
            direct_csid = csid or self._page_sender_csid(self._session_for_send(account))
            result = self.direct_send.send_image(uid, image_path, direct_csid, account)
            if result is not None:
                self.send_count += 1
                log.info("image send uid=%s account=%s ok=%s cost=%sms via=%s",
                         uid, account, result.get("ok"), result.get("cost_ms"), result.get("via"))
                return result
        return {
            "ok": False, "status": "unsupported", "real_send": False,
            "retryable": False, "via": "dll_direct_image",
            "error": "image_channel_unavailable",
            "error_user": "当前版本不支持发图片（需要注入 pdd_send_v6.dll）",
        }

    def send_and_wait(self, uid, content, account, *, timeout: float = 5.0, csid=None, is_expired=None) -> dict:
        """同步发送并等待回执。供 agent 命令线程调用（阻塞, 守护线程执行 eval）。"""
        if not self._thread or not self._thread.is_alive():
            return {
                "ok": False, "status": "failed", "real_send": False, "via": "cdp",
                "error_user": "CDP 通道未启动，请确认 PDD 商家工作台已打开",
            }
        event = _SendWaiter()
        event.is_expired = is_expired
        event.deadline = time.monotonic() + timeout
        with self._lock:
            self._act.append(("send", (str(uid), str(content), str(account or ""), event, timeout, csid)))
        event.wait(timeout)
        if event.result is None:
            key = (str(account), str(uid), str(content))
            with self._lock:
                event.cancelled = True
                self._act = [(name, args) for name, args in self._act
                             if not (name == "send" and args[3] is event)]
                if key in self._pending_sends:
                    del self._pending_sends[key]
            # 已派发到某个店铺队列的也要撤回来，否则超时后还会发出去
            self._cancel_dispatched_send(event)
            return {
                "ok": True,
                "status": "indeterminate",
                "real_send": False,
                "via": "cdp",
                "error_user": "已提交工作台但未收到发送回执，请人工核对会话",
            }
        return event.result

    def reconnect(self) -> None:
        with self._lock:
            self._act.append(("reconnect", ()))

    def _cancel_dispatched_send(self, event) -> None:
        """从各店铺的动作队列里摘掉一个已超时的发送动作。"""
        with self._lock:
            sessions = list(self.sessions)
        for session in sessions:
            kept: list = []
            while True:
                try:
                    item = session.actions.get_nowait()
                except queue.Empty:
                    break
                name, args = item
                if name == "send" and len(args) > 3 and args[3] is event:
                    continue
                kept.append(item)
            for item in kept:
                session.actions.put(item)

    # ---------------- 事件/状态 ----------------
    def _note_seller_reply(self, parsed: dict, hint: str = "") -> None:
        """记下「这个会话最后一次卖家回复的平台时间」，供补拉提升判断。"""
        try:
            ts = float(parsed.get("ts") or 0)
        except (TypeError, ValueError):
            return
        if ts <= 0:
            return
        obj = parsed.get("obj") or {}
        message = obj.get("message") or {}
        endpoints = (message.get("to"), message.get("from"))
        buyer = _to_buyer_uid({"obj": obj}) if obj else ""
        if not buyer:
            buyer = str(parsed.get("uid") or "")
        account = str(hint or "")
        if not account:
            for endpoint in endpoints:
                if isinstance(endpoint, dict) and endpoint.get("role") == "mall_cs":
                    mall = str(endpoint.get("mall_id") or endpoint.get("uid") or "")
                    target = str(obj.get("target_id") or "")
                    if mall and target:
                        account = "cs_%s:%s" % (mall, target)
                    break
        for key in {(account, buyer), ("", buyer)}:
            if not key[1]:
                continue
            prev = self._last_seller_ts.get(key, 0.0)
            if ts > prev:
                self._last_seller_ts[key] = ts

    def _upgrade_history_event(self, msg: dict) -> bool:
        """同一 msg_id 先以 history 发出、后收到实时帧 → 升级为实时（Phase-3）。"""
        mid = str(msg.get("msg_id") or "")
        if not mid:
            return False
        emitted_at = self._history_emitted.pop(mid, 0.0)
        if not emitted_at or time.time() - emitted_at > 300.0:
            return False
        msg = dict(msg)
        msg["is_history"] = False
        msg["auto_reply_eligible"] = True
        msg["supersedes_history"] = True
        msg["history_reason"] = "fresh_push_supersedes_history"
        msg["source"] = "pdd_cdp"
        msg["captured_at"] = time.time()
        msg["captured_at_ms"] = int(round(msg["captured_at"] * 1000))
        self.msg_count += 1
        log.info("history event upgraded by live push msg_id=%s", mid)
        self._emit(msg)
        return True

    def _promote_if_fresh(self, msg: dict) -> dict:
        """补拉到的消息如果很新、且还没人回复过，就按实时上送（Phase-3）。"""
        if not bool(self.cfg.get("promote_fresh_pull", True)):
            return msg
        try:
            max_age = float(self.cfg.get("fresh_pull_max_age_seconds") or 30.0)
        except (TypeError, ValueError):
            max_age = 30.0
        account = str(msg.get("account") or "")
        buyer = str(msg.get("buyer_id") or "")
        try:
            ts = float(msg.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        seller_ts = max(self._last_seller_ts.get((account, buyer), 0.0),
                        self._last_seller_ts.get(("", buyer), 0.0))
        promoted = promote_fresh_history(
            msg, now=time.time(), max_age=max_age, has_seller_reply=seller_ts > ts > 0)
        if promoted is not msg:
            log.info("fresh pull promoted msg_id=%s age=%.1fs account=%s",
                     promoted.get("msg_id"), max(0.0, time.time() - ts), account)
        return promoted

    def _emit(self, msg: dict) -> None:
        from .message_timing import stamp_message
        msg = stamp_message(msg)
        if self.on_event:
            self.on_event(msg)

    def _set_state(self, st: str, **extra) -> None:
        self.state = st
        if extra:
            log.info("pddbridge source state=%s extra=%s", st, extra)

    def _close_sessions(self) -> None:
        with self._lock:
            sessions, self.sessions = list(self.sessions), []
        for session in sessions:
            session.close()
            session.worker = None
        self.port = None
        self._account_hint = ""

    # 旧名保留: 只有一个连接时等价于关全部
    _close_cdp = _close_sessions

    def _sleep(self, sec: float) -> None:
        self._stop_evt.wait(sec)

    # ---------------- 取帧与存活判定 ----------------
    # ---------------- CDP 调用（每会话一把锁，锁内才有超时） ----------------
    def _eval_timeout(self) -> float:
        try:
            value = float(self.cfg.get("cdp_eval_timeout_seconds") or 6.0)
        except (TypeError, ValueError):
            value = 3.0
        return max(0.5, min(value, 8.0))

    # 兜底表达式：注入脚本太旧（没有 __pddBridge_csid）时用，和 inject.js 同一套取值
    _PAGE_SENDER_CSID_EXPR = (
        "(function(){try{var a=document.getElementById('app');var vm=a&&a.__vue__;"
        "var u=vm&&vm.$store&&vm.$store.state.userInfo;"
        "return (u&&(u.nickname||u.username))||null;}catch(e){return null;}})()"
    )
    _CSID_CACHE_SECONDS = 120.0

    def _page_sender_csid(self, session: _Session) -> str:
        """页面当前客服身份（PDD 发送要求的 sender label），取不到返回空串。

        权威口径在注入层：`window.__pddBridge_csid()` 与 `__pddBridge_sendText`
        内部完全相同。传 `cs_mall:uid` 这种账号串给 DLL，pdd 会直接返回 false
        （现场"直发 OK 0、一条也没发出去"就是这个）。
        """
        if session is None:
            return ""
        cached = getattr(session, "sender_csid", "")
        cached_at = getattr(session, "sender_csid_at", 0.0)
        if cached and time.time() - cached_at < self._CSID_CACHE_SECONDS:
            return cached
        value = ""
        for expr in ("(window.__pddBridge_csid ? window.__pddBridge_csid() : null)",
                     self._PAGE_SENDER_CSID_EXPR):
            try:
                got = self._eval(session, expr)
            except Exception as exc:
                log.debug("读取页面客服身份失败: %s", exc)
                continue
            text = str(got).strip() if isinstance(got, (str, int)) and str(got).strip() else ""
            if text and text.lower() not in {"null", "undefined", "none"}:
                value = text
                break
        if value:
            session.sender_csid = value
            session.sender_csid_at = time.time()
        return value

    def _eval(self, session: _Session, expr, timeout: float | None = None):
        """会话内串行 eval。--timeout 缺省用 cdp_eval_timeout_seconds（默认 3s）。

        旧实现一律 8 秒且不串行：20 店时一次慢调用就卡住整片，期间其他店铺的帧
        全部堵在 CDP 连接上，健康探测也和取帧抢同一条 websocket。
        """
        limit = timeout or self._eval_timeout()
        started = time.perf_counter()
        try:
            with session.lock:
                return session.cdp.eval(expr, session.cid, timeout=limit)
        finally:
            elapsed = time.perf_counter() - started
            self._eval_count += 1
            session.evals += 1
            session.eval_ms_total += elapsed
            if elapsed > session.eval_ms_max:
                session.eval_ms_max = elapsed
            # 超过一半超时预算就算"慢"：20 店时这些慢调用会连锁拖死整片
            if elapsed > limit * 0.5:
                self._eval_slow += 1

    def eval_stats(self) -> dict:
        """CDP eval 负载快照（20 店排障用：次数、慢调用、单店最慢、轮询节奏）。"""
        with self._lock:
            sessions = list(self.sessions)
        slowest = 0.0
        per = []
        for session in sessions:
            if session.eval_ms_max > slowest:
                slowest = session.eval_ms_max
            per.append({"session": session.label(), "evals": session.evals,
                        "max_ms": int(session.eval_ms_max * 1000)})
        per.sort(key=lambda x: x["max_ms"], reverse=True)
        try:
            idle_poll = float(self.cfg.get("cdp_poll_interval_idle") or 2.0)
        except (TypeError, ValueError):
            idle_poll = 2.0
        return {"evals": self._eval_count, "slow": self._eval_slow,
                "max_ms": int(slowest * 1000), "sessions": len(sessions),
                "idle_poll": idle_poll, "per_session": per[:5]}

    def _drain(self, session: _Session):
        """取一帧批次, 同时判定该页面注入是否还活着。

        返回 (frames, hooked, lossless)。
        lossless=True 表示用了 peek（未删数据），调用方处理完必须 _ack；
        旧注入脚本无 peek 时返回 False，行为同旧版（先清后传，CDP 失败即丢）。
        """
        r = self._eval(session, _DRAIN_EXPR)
        if r is None:
            raise RuntimeError("cdp eval 无响应(连接断开/超时)")
        if not isinstance(r, dict):
            return [], False, False
        stats = r.get("stats")
        if isinstance(stats, dict):
            self._note_js_stats(stats, session)
        frames = r.get("frames")
        hooked = bool(r.get("hooked")) and bool(r.get("attached", True))
        return (frames if isinstance(frames, list) else []), hooked, bool(r.get("lossless"))

    def _ack(self, session: _Session, count: int) -> None:
        """确认已成功接手 count 帧，注入层才会从缓冲里删除。失败也不抛（下轮重取）。"""
        if count <= 0:
            return
        try:
            self._eval(session, _ACK_EXPR % int(count))
        except Exception as exc:
            log.debug("drain ack 失败(下轮会重取): %s", exc)

    def _note_js_stats(self, stats: dict, session: _Session | None = None) -> None:
        """把注入层自己记的计数搬到 Python 侧并告警（JS 溢出/推送失败/误丢原来是静默的）。"""
        for key, field in (("overflow_drop", "buf_overflow"), ("dedup_skip", "js_would_drop"),
                           ("exact_repeat", "js_exact_repeat"),
                           ("push_skip", "push_skip"), ("push_retry", "push_retry"),
                           ("spilled", "spill"), ("spill_replayed", "spill_replayed")):
            try:
                value = int(stats.get(key) or 0)
            except (TypeError, ValueError):
                continue
            previous = self._js_stats.get(key, 0)
            if value > previous:
                self.drops[field] += value - previous
                if field == "js_would_drop":
                    log.warning(
                        "pddbridge 页面里的旧版注入包装本会误丢 %d 条消息（已拦下）；"
                        "刷新工作台页面可彻底清除旧包装",
                        value - previous,
                    )
                elif field == "js_exact_repeat":
                    log.info("pddbridge 注入层看到字节完全相同的重复帧 +%d（未丢）", value - previous)
                else:
                    log.warning("pddbridge 注入层计数 %s +%d (累计 %d)", key, value - previous, value)
            self._js_stats[key] = value
        if session is not None:
            try:
                if int(stats.get("pushed") or 0) > 0:
                    session.push_ok = True
            except (TypeError, ValueError):
                pass

    def _warn_throttled(self, key: str, message: str, interval: float = 60.0) -> None:
        now = time.time()
        if now - self._warn_at.get(key, 0.0) >= interval:
            self._warn_at[key] = now
            log.warning(message)

    def _pump_session(self, session: _Session) -> str:
        """LISTENING 单轮: 取帧 + 判活。返回 ok | lost | error。"""
        session.last_batch = 0
        try:
            frames, hooked, lossless = self._drain(session)
        except Exception as exc:
            log.warning("cdp drain error -> 重扫 (%s): %s", session.label(), exc)
            return "error"
        if not hooked:
            self.drops["hook_lost"] += 1
            log.warning("pddbridge 注入丢失(页面重载/换页) -> 重新注入 %s", session.label())
            return "lost"
        frame_error = False
        for e in frames:
            try:
                self._handle_frame(e, session)
            except Exception as exc:
                frame_error = True
                self.drops["frame_error"] += 1
                self.last_error = "frame: %r" % exc
                self._warn_throttled("frame_error", "frame error: %s" % exc)
                self._forget_frame(e)
        # 处理异常时不确认，宁可下一轮重复也不静默丢失。
        if lossless and not frame_error:
            self._ack(session, len(frames))
        session.frames += len(frames)
        session.last_batch = len(frames)
        if frames:
            session.last_frame_at = time.time()
        return "ok"

    # ---------------- 逐店隔离：每个会话一个 poller 线程 ----------------
    def _backoff_max(self) -> float:
        try:
            return max(1.0, float(self.cfg.get("cdp_session_backoff_max_seconds") or 30.0))
        except (TypeError, ValueError):
            return 30.0

    def _health_interval(self) -> float:
        try:
            return max(2.0, float(self.cfg.get("cdp_health_interval_seconds") or 90.0))
        except (TypeError, ValueError):
            return 30.0

    def _ensure_workers(self) -> None:
        """给每个会话确保一个存活的 poller 线程（20 店 = 20 条并行链路）。"""
        with self._lock:
            sessions = list(self.sessions)
        for session in sessions:
            if session.worker is not None and session.worker.is_alive():
                continue
            worker = threading.Thread(target=self._session_worker, args=(session,),
                                      daemon=True,
                                      name="pddbridge-session-%s" % (session.account or session.cid))
            session.worker = worker
            worker.start()

    def _note_session_failure(self, session: _Session) -> float:
        """单店失败退避：1s→2s→4s→…→上限（默认 30s）。只影响这一家店。"""
        session.failures += 1
        delay = min(self._backoff_max(), 2.0 ** min(session.failures - 1, 5))
        session.next_attempt = time.time() + delay
        return delay

    def _session_worker(self, session: _Session) -> None:
        """单店取帧循环。一家店卡住/超时只退避它自己，不影响其他店铺。

        节奏自适应：活跃店铺 0.2s 一轮保证实时；空闲（没推送、上轮没取到帧）
        降到 cdp_poll_interval_idle（默认 2s）。20 店时两者相差 10 倍——
        原来 20×5=100 次 eval/s 会把渲染进程和 GIL 压死，表现就是集体超时。
        """
        poll = 0.2
        try:
            poll = max(0.05, float(self.cfg.get("cdp_poll_interval") or 0.2))
        except (TypeError, ValueError):
            poll = 0.2
        idle_poll = 2.0
        try:
            idle_poll = max(poll, float(self.cfg.get("cdp_poll_interval_idle") or 2.0))
        except (TypeError, ValueError):
            idle_poll = 2.0
        # 启动错峰：20 个 worker 同时醒会同刻抢 CDP，错开 0~1 轮
        rng = random.Random(hash(session.key()) & 0xFFFFFFFF)
        self._stop_evt.wait(poll * rng.random())
        idle_rounds = 0
        while not self._stop_evt.is_set() and session in self.sessions:
            self._run_session_actions(session)
            now = time.time()
            if session.next_attempt > now:
                self._stop_evt.wait(min(poll, session.next_attempt - now))
                continue
            try:
                result = self._pump_session(session)
            except Exception as exc:            # 防御：worker 绝不能死
                log.debug("session worker error %s: %s", session.label(), exc)
                result = "error"
            if result == "ok":
                if session.failures:
                    log.info("pddbridge 会话恢复 %s（连续失败 %d 次后）",
                             session.label(), session.failures)
                session.failures = 0
                session.next_attempt = 0.0
            elif result == "lost":
                # 页面重载/切店：只重注入这一家，退避重试，不碰其他店铺
                self._reinject_session(session)
            else:
                delay = self._note_session_failure(session)
                if session.failures == 1:
                    log.warning("pddbridge 会话读取失败 %s，单店退避重试（不影响其他店铺）",
                                session.label())
                else:
                    log.debug("pddbridge 会话仍在退避 %s failures=%d next=%.1fs",
                              session.label(), session.failures, delay)
            now = time.time()
            if now >= session.next_health:
                session.next_health = now + self._health_interval()
                self._health_check_session(session)
            # 这轮取到帧 = 活跃；连续取不到就退到空闲节奏
            if getattr(session, "last_batch", 0) > 0:
                idle_rounds = 0
                wait = poll
            else:
                idle_rounds = min(idle_rounds + 1, 6)
                # 前 3 轮仍按活跃节奏（消息刚处理完的余波），之后退到空闲
                wait = poll if idle_rounds <= 3 else idle_poll
            # 抖动 ±20%：避免 20 家店长期同相撞车
            self._stop_evt.wait(wait * (0.8 + 0.4 * rng.random()))

    def _reinject_session(self, session: _Session) -> None:
        """只重注入当前会话；失败则退避，不拉黑整个通道。"""
        try:
            r = self._eval(session, self._inject_payload(session))
            if isinstance(r, dict) and r.get("ok") is True and r.get("attached") == 1:
                session.failures = 0
                session.next_attempt = 0.0
                session.degraded_at = 0.0
                log.info("pddbridge 会话已重新注入 %s", session.label())
                return
            reason = "inject not ready: %s" % (r,)
        except Exception as exc:
            reason = str(exc)
        delay = self._note_session_failure(session)
        session.degraded_at = time.time()
        self._warn_throttled("reinject:%s" % session.key(),
                             "pddbridge 重注入失败 %s: %s（%.0fs 后重试）"
                             % (session.label(), reason, delay), interval=60.0)

    def _run_session_actions(self, session: _Session) -> None:
        """在会话自己的工作线程里执行发送/拉历史，避免阻塞主循环和别的店铺。"""
        for _ in range(4):
            try:
                name, args = session.actions.get_nowait()
            except queue.Empty:
                return
            try:
                if name == "send":
                    self._do_send_action(*args)
                elif name == "pull_history":
                    self._do_pull_history(*args)
            except Exception as exc:
                log.warning("session action %s failed (%s): %s", name, session.label(), exc)

    # ---------------- 推送通道（页面直推, 免轮询） ----------------
    def _push_url(self) -> str:
        if not self._push.port:
            return ""
        return "http://127.0.0.1:%d/push/%s" % (self._push.port, self._push.token)

    def _inject_payload(self, session: _Session) -> str:
        prelude = (
            "window.__pddBridge_push_url = %r;\n"
            "window.__pddBridge_session = %r;\n"
        ) % (self._push_url(), session.key())
        return prelude + _load_inject()

    def _session_by_sid(self, sid: str):
        for session in self.sessions:
            if session.key() == sid:
                return session
        # Execution-context IDs are only unique inside one browser target.
        # Accept an old identifier only when it is unambiguous.
        legacy = [s for s in self.sessions if "%s|%s" % (s.port, s.cid) == sid]
        if len(legacy) == 1:
            return legacy[0]
        return None

    def _pump_push(self) -> None:
        """处理页面直推的帧。帧处理仍在本线程串行做, HTTP 线程只入队。"""
        for entry in self._push.drain():
            sid = str(entry.get("sid") or "")
            session = self._session_by_sid(sid)
            if session is None and sid:
                self.drops["push_unknown_session"] += 1
                self._warn_throttled("push_unknown_session",
                                     "推送帧的会话认不出(已按全局账号处理): sid=%s" % sid[:40])
            try:
                self._handle_frame(entry, session)
            except Exception as exc:
                self.drops["frame_error"] += 1
                self.last_error = "push frame: %r" % exc
                self._warn_throttled("frame_error", "push frame error: %s" % exc)
                self._archive_raw(entry, session, "frame_error")
        if self._push.dropped > self.drops["push_overload"]:
            delta = self._push.dropped - self.drops["push_overload"]
            self.drops["push_overload"] = self._push.dropped
            log.warning("pddbridge 推送队列满, 丢 %d 条 (页面缓冲会兜底)", delta)

    # ---------------- 去重（按平台 msg_id；dedup_mode=off 时完全关掉，交给中心判） ----------------
    def _dedup_window(self) -> float:
        """msg_id 去重窗口（秒）。

        旧实现在这里写死 120 秒，而回扫窗口是 `history_report_max_age_seconds`（1800 秒）：
        同一条消息 2 分钟后就不再算“见过”，于是回扫每轮都会再上报一次（实测 8 分钟前的
        AI 回复被当新消息重传）。中心若按消息触发回复，就会重复应答。
        取两者最大值，并允许显式配置。
        """
        try:
            explicit = self.cfg.get("dedup_window_seconds")
            if explicit is not None:
                return max(0.0, float(explicit))
        except (TypeError, ValueError):
            pass
        try:
            history = float(self.cfg.get("history_report_max_age_seconds") or 1800.0)
        except (TypeError, ValueError):
            history = 1800.0
        return max(300.0, history)

    def _dedup(self, kind: str, msg_id) -> bool:
        if self.dedup_off or not msg_id:
            return False
        key_str = str(msg_id)
        if _is_fallback_id(key_str):
            # 没有平台 msg_id 时用 content+time 哈希兜底, 同秒同内容会撞哈希,
            # 绝不能拿它当去重依据(否则会误杀真实消息)。
            return False
        now = time.time()
        window = self._dedup_window()
        key = (kind, key_str)
        last = self._seen.get(key)
        if last is not None and now - last < window:
            self._seen[key] = now
            return True
        self._seen[key] = now
        self._trim_seen()
        return False

    def _forget_frame(self, entry: dict) -> None:
        try:
            p = parse_frame(entry.get("data"))
            msg_id = str(p.get("msg_id") or "")
            if msg_id:
                self._seen.pop(("push", msg_id), None)
                self._seen.pop(("send_message", msg_id), None)
                self._seen_any.pop(msg_id, None)
        except Exception:
            pass

    def _trim_seen(self) -> None:
        """去重表不能无限涨（长跑 20 店一天几万条）：超量就清掉最旧的。"""
        cap = 50000
        if len(self._seen) <= cap and len(self._seen_any) <= cap:
            return
        window = self._dedup_window()
        now = time.time()
        for table in (self._seen, self._seen_any):
            if len(table) <= cap:
                continue
            for key in [k for k, ts in table.items() if now - ts > window]:
                table.pop(key, None)
            while len(table) > cap:
                table.pop(next(iter(table)), None)

    def _seen_before(self, msg_id) -> bool:
        if self.dedup_off or not msg_id:
            return False
        key_str = str(msg_id)
        if _is_fallback_id(key_str):
            return False
        now = time.time()
        last = self._seen_any.get(key_str)
        if last is not None and now - last < self._dedup_window():
            return True
        self._seen_any[key_str] = now
        self._trim_seen()
        return False

    # ---------------- 原始帧归档（“全部上报”的安全网：任何帧都会落一份本地副本） ----------------
    def _archive_raw(self, e: dict, session: _Session | None, reason: str, kind: str = "") -> None:
        """把不上报或带异常标记的帧原样追到本地 jsonl, 超过 20MiB 轮转一份。

        中心有它自己的校验规则(例如 invalid_message 会拒收单号); 拒收不能变成“没发生过”。
        """
        try:
            path = self.raw_archive_path
            path.parent.mkdir(parents=True, exist_ok=True)
            if self._raw_archive_bytes < 0:
                self._raw_archive_bytes = path.stat().st_size if path.exists() else 0
            if self._raw_archive_bytes > 20 * 1024 * 1024:
                backup = path.with_suffix(path.suffix + ".1")
                try:
                    if backup.exists():
                        backup.unlink()
                    path.replace(backup)
                except OSError:
                    pass
                self._raw_archive_bytes = 0
            row = {
                "at": time.time(),
                "reason": reason,
                "frame_kind": kind,
                "account": (session.account if session is not None else self._account_hint),
                "port": (session.port if session is not None else None),
                "dir": e.get("dir"),
                "t": e.get("t"),
                "data": e.get("data"),
            }
            line = json.dumps(row, ensure_ascii=False) + "\n"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
            self._raw_archive_bytes += len(line.encode("utf-8"))
        except Exception as exc:
            self._warn_throttled("raw_archive", "原始帧归档写入失败: %s" % exc)

    # ---------------- 帧入口 ----------------
    def feed_native_frame(self, raw) -> None:
        """原生 DLL 通道的 push 帧：拆信封后与注入脚本的 entry 同构，复用同一条帧管线。

        工作台原生回调发的是 ``{"push_type":2,"push_data":{"data":[{"message":...}]}}``
        这种信封，一次推送可能带多条消息；这里逐条拆成 ``{"cmd":"push",...}``。
        已经是页面帧形状的直接放行，认不出的按诊断帧上报（不静默丢）。
        """
        from .pddbridge.protocol import native_push_messages

        frames = native_push_messages(raw)
        stamp = int(time.time() * 1000)
        if not frames:
            self._handle_frame({"dir": "in", "data": raw, "t": stamp, "chan": "native"})
            return
        for frame in frames:
            self._handle_frame({"dir": "in", "data": frame, "t": stamp, "chan": "native"})

    # ---------------- 扫描/注入 ----------------
    def _try_recover(self) -> bool:
        """降级期间每 30s 重试一次 CDP; 成功则切回并通知 agent 停掉日志通道。

        调用顺序: 先置状态再调 on_recover —— on_recover 里 agent 会停掉日志通道,
        之后才会再 pump 帧, 保证降级期间不会出现双通道重复上报。
        """
        if time.time() < self._next_retry:
            return False
        self._next_retry = time.time() + 30.0
        if not self._scan_and_attach():
            self._warn_throttled("cdp_recover",
                                 "pddbridge 降级中, CDP 重试未就绪（工作台未启动/页面未进接待页）")
            return False
        self._degraded = False
        self._set_state(LISTENING)
        log.info("pddbridge CDP 已恢复, 切回 CDP 数据源")
        if self.on_recover:
            try:
                self.on_recover()
            except Exception as exc:
                log.warning("on_recover error: %s", exc)
        return True

    def _scan_and_attach(self) -> bool:
        """扫描所有调试端口的所有 socketUtil 上下文并逐个注入。

        多店铺 = 多个会话: 老实现只取 hits[0] 并在第一个成功的端口上 return,
        第二个账号 / 第二个客户端的消息会整段静默消失。
        """
        fixed = self.cfg.get("cdp_port")
        if fixed:
            ports = [int(fixed)] if pdd_cdp.is_alive(int(fixed)) else []
        else:
            discovered = pdd_cdp.discover_ports(only_pdd=True)
            ports = [p for p in sorted(discovered) if pdd_cdp.is_alive(p)]
            if not ports:
                self._warn_throttled("no_cdp_port",
                                     "pddbridge 未发现可用 CDP 端口 discovered=%s" % sorted(discovered),
                                     interval=30.0)
                return False
        with self._lock:
            known = {s.key() for s in self.sessions}
            known_targets = {s.target_id for s in self.sessions if s.target_id}
        skipped: list = []
        for port in ports:
            try:
                hits = pdd_cdp.find_socketutil_sessions(port, skip_ids=known_targets)
            except Exception as exc:
                log.warning("find_socketutil_sessions port=%s error: %s", port, exc)
                continue
            for hit in hits:
                session = _Session(port, hit.get("url") or "", hit["cdp"], hit["cid"],
                                   hit.get("id") or "")
                if session.key() in known:
                    session.close()      # 已在挂, 不重复开连接
                    continue
                try:
                    r = self._eval(session, self._inject_payload(session))
                except Exception as exc:
                    log.warning("inject eval port=%s error: %s", port, exc)
                    r = None
                if not (isinstance(r, dict) and r.get("ok") is True
                        and r.get("attached") == 1 and not r.get("reloading")):
                    log.warning("inject not ready port=%s => %s", port, r)
                    session.close()
                    skipped.append(session.target_url or str(port))
                    continue
                session.next_health = time.time() + self._health_interval()
                with self._lock:
                    self.sessions.append(session)
                known.add(session.key())
                self._refresh_account_hint(session)
                session.last_health = time.time()
                log.info("pddbridge 会话已挂 %s push=%s url=%s attached=%s",
                         session.label(), bool(self._push_url()),
                         (session.target_url or "")[:60], r.get("attached"))
        if skipped:
            # 发现了但没挂上 = 这些店铺以后收不到消息, 必须可见
            self.drops["context_unattached"] += len(skipped)
            log.warning("pddbridge %d 个上下文未注入(这些店铺的消息将缺失): %s",
                        len(skipped), ", ".join(s[:60] for s in skipped))
        if len(self.sessions) > 1:
            log.info("pddbridge 多账号在线: %s",
                     ", ".join(s.account or s.label() for s in self.sessions))
        return bool(self.sessions)

    def _drop_session(self, session: _Session) -> None:
        """丢弃单个会话：只停它自己的线程和连接，不碰其他店铺。"""
        session.close()
        with self._lock:
            try:
                self.sessions.remove(session)
            except ValueError:
                pass
            remaining = bool(self.sessions)
            primary = self.sessions[0].port if self.sessions else None
        session.worker = None
        if not remaining:
            self.port = None
        elif primary:
            self.port = primary
        log.info("pddbridge 会话已移除 %s（剩余 %d 个）", session.label(), len(self.sessions))

    def _native_instance_ready(self, recv) -> bool:
        """问 DLL 有没有拿到 CMChatImpl 实例（STATE 里的 INSTANCE 非 0）。"""
        try:
            reply = recv._command("STATE")
        except Exception:
            return False
        if not isinstance(reply, str) or "INSTANCE" not in reply:
            return False
        try:
            token = reply.split("INSTANCE", 1)[1].split()[0]
            return int(token, 16) != 0
        except (IndexError, ValueError):
            return False

    def _apply_native_recv_config(self) -> None:
        """启用原生接收。

        三种模式（优先级从高到低）：
          1. ``recv_slot >= 0``      → 按已确认的虚表槽位注册；
          2. ``recv_probe = true``   → 全槽位探针（**会崩客户端**，仅副本机排障）；
          3. 默认                    → 导出 detour（NotifyWSMessage/CheckWSMsg），
                                      不需要槽位、不靠 RVA，实测 3.5.0.40 可用。
        """
        slot = int(self.cfg.get("recv_slot") or -1)
        want_probe = bool(self.cfg.get("recv_probe", False))
        recv = self._native_recv
        if recv is None or getattr(recv, "_native_cfg_done", False):
            return

        def apply() -> None:
            probe_path = Path(str(self.cfg.get("local_queue_path") or "bridge_queue.jsonl"))
            probe_path = probe_path.with_name("pdd_recv_probe.log")
            mode = str(self.cfg.get("recv_engine") or "auto").strip().lower()

            # ---- 第 1 段：先把实例拿到手 ----
            # 构造函数钉不住（IAT 里没有、挂起注入也抓不到）时，用导出 detour 的副作用：
            # NotifyWSMessage 第一次被调用时会把 this 记为实例。心跳几秒一次，很快就有。
            if mode != "detour" and slot < 0 and not want_probe:
                for attempt in range(8):
                    if self._stop_evt.is_set():
                        return
                    if self._native_instance_ready(recv):
                        break
                    reply = recv.hook_install()
                    if attempt == 0:
                        log.info("原生接收：先用导出 detour 捕获工作台实例: %s", reply)
                    for _ in range(10):        # 最多等 20 秒，等第一次回调
                        if self._stop_evt.is_set() or self._native_instance_ready(recv):
                            break
                        time.sleep(2.0)
                    if self._native_instance_ready(recv):
                        log.info("原生接收：已捕获工作台实例，切换到全槽位嗅探")
                        break
                else:
                    log.warning("原生接收：未能捕获工作台实例（工作台未登录？），暂用页面通道")

            # 不再"重试 6 次就放弃"：工作台可能几十秒后才登录完成，
            # 放弃了就永远停在页面通道（现场"重启后一直没有原生"就是这个）。
            failures = 0
            while not self._stop_evt.is_set():
                failures += 1
                if slot >= 0:
                    reply = recv.configure()
                    ok = reply and not reply.startswith("pipe_")
                    what = "虚表槽位 mode=slot(%s)" % slot
                elif want_probe:
                    reply = recv.probe(str(probe_path))
                    ok = reply and not reply.startswith("pipe_")
                    what = "探针（注意：3.5.0.40 会崩，仅排障用）"
                elif mode == "detour":
                    reply = recv.hook_install()
                    ok = bool(reply) and "hook=0" in reply
                    what = "导出 detour（只收心跳/出口帧）"
                else:
                    # 默认：全槽位嗅探，进程级覆盖全部店铺/会话
                    reply = recv.configure_auto()
                    ok = bool(reply) and "reg=1" in reply
                    what = "全槽位嗅探（进程级）"
                if ok:
                    recv._native_cfg_done = True
                    log.info("原生接收已启动（%s）: %s", what, reply)
                    return
                if reply and not reply.startswith("pipe_"):
                    if failures <= 3 or failures % 12 == 0:
                        log.warning("原生接收启动被拒（%s，第 %d 次）: %s", what, failures, reply)
                elif failures <= 3 or failures % 12 == 0:
                    log.info("原生接收：等待工作台就绪（第 %d 次）: %s", failures, reply)
                time.sleep(10.0)

        threading.Thread(target=apply, daemon=True, name="pdd-native-cfg").start()

    def _health_check_session(self, session: _Session) -> None:
        """单店健康探测。跑在会话自己的 worker 里，不和别的店铺抢锁。"""
        if not pdd_cdp.is_alive(session.port):
            log.warning("health check failed %s -> 丢弃该会话", session.label())
            self._drop_session(session)
            return
        # socket 存活检查: 钩子活着但工作台未建 IM WebSocket（无打开会话）时
        # 不会有任何推送, 静默等价于不收消息 —— 必须大声说出来。
        try:
            r = self._eval(
                session,
                "(function(){var su=window.socketUtil;"
                "return JSON.stringify({uid:new URLSearchParams(location.search).get('uid')||'',"
                " st:(su&&su.socket)?(su.socket.readyState==null?'none':su.socket.readyState):'no_socket'});})()",
                timeout=min(2.0, self._eval_timeout()))
            info = json.loads(r) if isinstance(r, str) else {}
            session.socket_state = str(info.get("st") or "unknown")
            if session.socket_state not in ("1", "open"):
                # 页面没真 socket 时，实时消息只能靠原生通道；把话说清楚，别让人去猜。
                hint = ""
                if session.socket_state in ("none", "no_socket"):
                    hint = "（页面无 JS socket；实时收消息需开启 recv_via_dll 原生接收）"
                self._warn_throttled(
                    "socket_dead:%s" % session.key(),
                    "工作台聊天 socket 未连接(state=%s uid=%s account=%s): 未打开会话时 PDD 不推送, "
                    "打开任意买家会话即可恢复接收%s"
                    % (session.socket_state, info.get("uid", ""), session.account or "?", hint),
                    interval=120.0)
        except Exception:
            session.socket_state = "probe_failed"
        self._refresh_account_hint(session)
        session.last_health = time.time()
        self._last_health = session.last_health

    def _health_check(self) -> None:
        """兼容旧入口：逐个会话探测（现已由各会话 worker 自行驱动）。"""
        with self._lock:
            sessions = list(self.sessions)
        for session in sessions:
            self._health_check_session(session)

    def _handle_actions(self) -> bool:
        """把发送/拉历史派发到目标店铺自己的 worker，自己不阻塞。"""
        with self._lock:
            acts, self._act = self._act, []
        reconnect = False
        for name, a in acts:
            if name == "reconnect":
                reconnect = True
                continue
            account = str(a[2] if name == "send" else a[1] or "")
            session = self._session_for_send(account)
            if session is None:
                # 没主的动作按旧路径就地执行（发送会返回 blocked，不会串台）
                if name == "send":
                    self._do_send_action(*a)
                elif name == "pull_history":
                    self._do_pull_history(*a)
                continue
            session.actions.put((name, a))
            self._wake.set()
        return reconnect

    def _do_pull_history(self, uid, account, size, begin_msg_id, start_index, pre_msg_id, event) -> None:
        session = self._session_for_send(account)
        if session is None:
            event.result = {"ok": False, "status": "blocked", "via": "cdp_history",
                            "error_user": "无法确定目标店铺会话，请在对应店铺窗口确认"}
            event.set()
            return
        expr = ("(window.__pddBridge_pullHistory ? "
                "window.__pddBridge_pullHistory(%r, %d, %r, %d, %r) : "
                "{ok:false,err:'no helper'})"
                % (uid, size, begin_msg_id, start_index, pre_msg_id))
        try:
            r = self._eval(session, expr)
        except Exception as exc:
            event.result = {"ok": False, "status": "failed", "via": "cdp_history", "error": str(exc)}
            event.set()
            return
        if not isinstance(r, dict):
            event.result = {"ok": False, "status": "failed", "via": "cdp_history",
                            "error_user": "页面没响应拉历史（注入可能需要刷新）"}
            event.set()
            return
        r = dict(r)
        r.setdefault("via", "cdp_history")
        r["buyer_id"] = uid
        r["size"] = size
        event.result = r
        event.set()

    def _session_for_send(self, account: str):
        """按目标 account 的店铺选会话。选不到宁可不发（串台比不发更糟）。"""
        if not self.sessions:
            return None
        target_mall = ""
        if account:
            from .pdd_context import mall_id_from_account
            target_mall = str(mall_id_from_account(account) or "")
        if target_mall:
            for session in self.sessions:
                if session.mall_id and session.mall_id == target_mall:
                    return session
        if account:
            for session in self.sessions:
                if session.account and session.account == account:
                    return session
        if len(self.sessions) == 1 and not target_mall:
            return self.sessions[0]
        return None

    def _do_send_action(self, uid, content, account, event, timeout, csid) -> None:
        session = self._session_for_send(account)
        if session is None:
            event.result = {
                "ok": False, "status": "blocked", "real_send": False, "via": "cdp_blocked",
                "error_user": "在线 %d 个账号但无法确定目标会话，请人工在对应店铺窗口确认"
                              % len(self.sessions),
            }
            event.set()
            return
        self._do_send(session, uid, content, account, event, timeout, csid)

    # ---------------- 守护线程主循环 ----------------
    def _run(self) -> None:
        self._set_state(SCANNING)
        fallback_after = max(60.0, float(self.cfg.get("cdp_fallback_after_seconds") or 60))
        poll = float(self.cfg.get("cdp_poll_interval") or 0.2)
        rescan = float(self.cfg.get("cdp_rescan_seconds") or 20)
        scan_deadline = time.time() + fallback_after
        next_rescan = 0.0

        while not self._stop_evt.is_set():
            if self._handle_actions():
                # 重连：只重新注入现有会话，不再“全关再挂”（20 店时那等于集体断线）
                for session in list(self.sessions):
                    self._reinject_session(session)

            self._pump_push()

            if self.state == SCANNING:
                if self._degraded:
                    # 降级巡检: 日志通道在跑, 这里只定期探 CDP, 挂上就切回
                    self._try_recover()
                    self._wake.wait(poll)
                    self._wake.clear()
                    continue
                if self._scan_and_attach():
                    self._set_state(LISTENING)
                    next_rescan = time.time() + rescan
                elif (
                    self.cfg.get("cdp_auto_fallback")
                    and self.on_fallback
                    and not self._fallback_done
                    and time.time() > scan_deadline
                ):
                    self._fallback_done = True
                    log.warning("pddbridge CDP 未就绪, 自动降级到探域日志")
                    try:
                        self.on_fallback()
                    except Exception as exc:
                        log.warning("on_fallback error: %s", exc)
                    # 不再退出线程: 修好工作台后要能自动切回（见 _try_recover）
                    self._degraded = True
                    self._next_retry = time.time() + 30.0
                else:
                    self._wake.wait(poll)   # 重扫期间也要能被推送唤醒
                    self._wake.clear()
                continue

            # LISTENING：主循环只做巡检与派发；取帧由各店铺自己的 worker 并行做。
            now = time.time()
            if now >= next_rescan:
                # 运行时新增账号 / 切店铺后页面重载: 补挂新上下文, 不动已挂的
                self._scan_and_attach()
                next_rescan = now + rescan
            self._reap_dead_sessions()
            self._ensure_workers()
            if not self.sessions:
                log.warning("pddbridge 全部会话注入丢失 -> 重新扫描")
                self._set_state(SCANNING)
                scan_deadline = time.time() + fallback_after
                next_rescan = 0.0
                continue
            self._wake.wait(poll)   # 有推送就立刻醒; 没推送就 200ms 轮一次
            self._wake.clear()

        self._close_sessions()

    def _reap_dead_sessions(self) -> None:
        """只清理"调试端口都没了"的会话；单次 eval 超时不算死，交给 worker 退避。"""
        with self._lock:
            sessions = list(self.sessions)
        for session in sessions:
            if not pdd_cdp.is_alive(session.port):
                log.warning("health check failed %s -> 丢弃该会话", session.label())
                self._drop_session(session)

    # ---------------- 帧处理 ----------------
    def _handle_frame(self, e: dict, session: _Session | None = None) -> None:
        """帧入口：多店并行后（每店一个 worker），帧处理必须串行。

        解析/去重/待确认表都不是线程安全的，且一条帧里可能触发发送回执确认；
        用一把粗粒度锁即可（每帧只做纯 CPU 工作，不阻塞 IO）。
        """
        # 排障开关：把收到的原始帧原样追加到文件（默认关）。
        # 图片/商品卡这类消息的 type 和附加字段只能从原始帧里看。
        dump = str(self.cfg.get("raw_frame_dump_path") or "").strip()
        if dump:
            try:
                with open(dump, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(
                        {"at": time.time(), "chan": e.get("chan"), "data": e.get("data")},
                        ensure_ascii=False) + "\n")
            except OSError:
                pass
        with self._frame_lock:
            self._frames_seen += 1
            self._last_frame_at = time.time()
            self._frame_stamps.append(self._last_frame_at)
            self._handle_frame_locked(e, session)

    def _handle_frame_locked(self, e: dict, session: _Session | None = None) -> None:
        hint = session.account if session is not None else ""
        if e.get("dir") == "send_error":
            key = (hint, str(e.get("uid") or ""), str(e.get("content") or ""))
            content = str(e.get("content") or "")
            with self._lock:
                pending = self._pending_sends.get(key)
                if pending and hint and pending.get("account") != hint:
                    pending = None
                if not pending and content:
                    # key 带上 account 之后，命令没带 account / 只给商城号的事件
                    # 会整条配不上。这里按内容唯一兜底，任意一侧为空即视为可用。
                    matches = [(item_key, row) for item_key, row in self._pending_sends.items()
                               if str(row.get("content") or "") == content
                               and (not hint or not row.get("account")
                                    or row.get("account") == hint)]
                    if len(matches) == 1:
                        key, pending = matches[0]
                if pending:
                    self._pending_sends.pop(key, None)
            if pending:
                error = str(e.get("error") or "native send rejected")
                pending["event"].result = {"ok": False, "status": "failed", "real_send": False,
                    "via": "cdp", "error_user": "拼多多客户端拒绝发送: " + error}
                pending["event"].set()
                log.warning("native send rejected buyer=%s error=%s", e.get("uid"), error)
            return
        # localStorage 兕底重放的帧：是早前未送达的旧消息，按历史帧上报（中心自决要不要用）
        history_flag = bool(e.get("replayed"))
        if e.get("dir") == "out":
            # 自己发出的方向: 由 send_message 回执负责上报, 不重复报
            self.reported["out_direction"] += 1
            return
        raw = e.get("data")
        if not raw:
            self._report_frame(e, session, "empty_frame", "")
            return
        p = parse_frame(raw)
        kind = p.get("kind")
        if kind == "push":
            # Native workbench reports our sent messages as a mall_cs push,
            # rather than a separate send_message response.
            if p.get("from_role") == "mall_cs":
                self._handle_send_receipt(p, hint)
                self._note_seller_reply(p, hint)
            if self._dedup("push", p.get("msg_id")):
                self.drops["dedup_py"] += 1
                return
            bm = buyer_message(p)
            msg = self._normalize_message(bm, history=history_flag, account_hint=hint)
            if msg and not self._seen_before(msg.get("msg_id")):
                self.msg_count += 1
                # 标记真实采集通道：原生（进程级，覆盖全部会话）/ 页面注入（仅当前会话）。
                # 两条都在时以先到的为准，后到的按 msg_id 去重，日志里能直接看出谁接住的。
                if e.get("chan") == "native":
                    msg["source"] = "pdd_native"
                    msg["capture_source"] = "pdd_native"
                    self.reported["native_msg"] = self.reported.get("native_msg", 0) + 1
                elif str(e.get("chan") or "").startswith("page"):
                    self.reported["page_msg"] = self.reported.get("page_msg", 0) + 1
                msg["captured_at_ms"] = e.get("t")
                self._emit(msg)
            elif msg:
                # 这一条之前以 history 身份发过（补拉先到），现在实时帧才到：
                # 允许把待发队列里的历史副本升级成实时，否则它会被 90 秒窗口压住，
                # 到中心正好 stale（现场 35/36 轮未回复就是这个）。
                self._upgrade_history_event(msg)
                self.drops["dedup_py"] += 1
        elif kind == "send_message":
            self._handle_send_receipt(p, hint)
            msg = self._normalize_send_message(p, account_hint=hint)
            if msg and not self._seen_before(msg.get("msg_id")):
                self.msg_count += 1
                self._emit(msg)
        elif kind == "list":
            # 历史消息也报(report_all 时), 中心可用 is_history 自行决定要不要回复
            if self.cfg.get("cdp_emit_history") or self.report_all:
                for bm in messages_from_list(p):
                    self.reported["history_frame"] += 1
                    msg = self._normalize_message(bm, history=True, account_hint=hint)
                    if msg:
                        # 显式打历史标记：_normalize_message 只设了 source，
                        # 而提升判断需要 is_history（老代码靠 stamp_message 事后补，
                        # 结果 Phase-3 的提升在 emit 之前永远看不到这个标记）。
                        msg["is_history"] = True
                        msg["history_reason"] = msg.get("history_reason") or "replay"
                        msg["capture_source"] = msg.get("capture_source") or "pdd_cdp_history"
                    if msg and not self._seen_before(msg.get("msg_id")):
                        self.msg_count += 1
                        # 历史帧必须带消息自己的平台时间, 而不是重扫那一刻;
                        # 否则中心会算出 (重扫时间 - 消息时间) 的几十分钟假延迟。
                        try:
                            ts_ms = int(float(msg.get("ts") or 0) * 1000)
                        except (TypeError, ValueError):
                            ts_ms = 0
                        if ts_ms > 0:
                            msg["captured_at_ms"] = ts_ms
                        msg = self._promote_if_fresh(msg)
                        if msg.get("is_history"):
                            self._history_emitted[str(msg.get("msg_id") or "")] = time.time()
                        self._emit(msg)
        elif kind in ("auth", "heartbeat", "sync_card_status", "push_read_state",
                      "conciliation_msg", "mall_system_msg"):
            self._report_frame(e, session, "system_frame", kind or "")
        else:
            self._report_frame(e, session, "unknown_kind", kind or "")

    # ---------------- 消息归一化 ----------------
    def _refresh_account_hint(self, session: _Session | None = None) -> None:
        """读该页面的店铺/席位信息, 绑定到会话（多账号各认各的）。"""
        if session is None:
            return
        try:
            info = self._eval(
                session,
                "(function(){var i=window.__pddBridge_info ? window.__pddBridge_info() : null;"
                "if(i)i.sendTextReady=!!(window.socketUtil && typeof window.socketUtil.sendMsg==='function'"
                " && typeof window.__pddBridge_sendText==='function');return i;})()"
            )
        except Exception:
            return
        if not isinstance(info, dict):
            return
        session.send_capable = bool(info.get("sendTextReady"))
        account = str(info.get("csidGuess") or "").strip()
        mall_id = str(info.get("globalMallId") or "").strip()
        uid = str(info.get("globalUid") or "").strip()
        if not account and mall_id.isdigit() and uid.isdigit():
            account = "cs_%s:%s" % (mall_id, uid)
        if mall_id:
            session.mall_id = mall_id
        if account:
            session.account = account
            self._account_hint = account
        self.port = self.sessions[0].port if self.sessions else self.port

    def _base_message(self, buyer_id, role, content, ts_ms, msg_id, pre_msg_id, csid, nickname):
        from .parser import _normalize_ts, _platform_time_key

        if role == "buyer":
            role = "user"
        if role not in ("user", "mall_cs"):
            role = "user"
        if not msg_id:
            msg_id = _stable_hash({"r": role, "u": buyer_id, "c": content, "t": ts_ms})
        return {
            "platform": "pdd",
            "msg_id": str(msg_id),
            "platform_message_id": str(msg_id),
            "identity_kind": "message_id",
            "buyer_id": str(buyer_id or ""),
            "role": role,
            "content": str(content or ""),
            "ts": int(_normalize_ts(ts_ms)),
            "platform_ts_key": str(_platform_time_key(ts_ms) or ts_ms or ""),
            "parent_msg_id": str(pre_msg_id or ""),
            "account": _canonical_account(csid or ""),
            "buyer_nick": str(nickname or ""),
            "source": "pdd_cdp",
            "delivery_status": "",
            "raw_type": None,
        }

    def _account_for_frame(self, raw: dict, hint: str = "") -> str:
        """Resolve the platform's explicit mall before any page/global hint."""
        from .pdd_context import mall_id_from_account
        message = raw.get("message") or raw
        mall = ""
        for endpoint in (message.get("to"), message.get("from")):
            if isinstance(endpoint, dict) and endpoint.get("role") == "mall_cs":
                mall = str(endpoint.get("mall_id") or endpoint.get("mallId") or endpoint.get("uid") or "")
                if mall:
                    break
        if mall:
            if hint and str(mall_id_from_account(hint) or "") == mall:
                return str(hint)
            accounts = {s.account for s in self.sessions if s.mall_id == mall and s.account}
            if len(accounts) == 1:
                return accounts.pop()
            return ""
        if hint and ACCOUNT_RE.fullmatch(_canonical_account(hint)):
            return str(hint)
        accounts = {s.account for s in self.sessions if s.account}
        return next(iter(accounts)) if len(accounts) == 1 else ""

    def _normalize_message(self, bm: dict, history: bool = False,
                           account_hint: str = "") -> dict | None:
        """买家方向消息归一化。异常帧不再丢弃: 带原因标记照报, 回不回由大脑决定。"""
        buyer_id = str(bm.get("buyer_id") or "")
        role = bm.get("from_role") or "user"
        explicit_account = _canonical_account(bm.get("seller_id") or "")
        csid = self._account_for_frame(bm.get("raw") or {},
            explicit_account if ACCOUNT_RE.fullmatch(explicit_account) else account_hint)
        if not csid:
            self._warn_throttled("unresolved_message_shop", "消息店铺归属无法确定，等待页面通道补报")
            return None
        seat_uid = _seat_uid(_canonical_account(csid))
        reason = ""
        if not buyer_id:
            reason = "no_buyer"
        elif seat_uid and buyer_id == seat_uid:
            # 帧里只带了席位一端: 中心可能因此建出“只含 AI 发言”的影子会话,
            # 所以打标上报(skipped_reason/is_diagnostic), 要不要它由大脑定。
            reason = "seat_uid_as_buyer"
        if reason:
            self.reported[reason] = self.reported.get(reason, 0) + 1
            self._archive_raw({"dir": "in", "t": bm.get("ts"),
                               "data": json.dumps(bm.get("raw") or {}, ensure_ascii=False)},
                              None, reason)
            self._warn_throttled(reason, "%s: msg_id=%s account=%s" % (reason, bm.get("msg_id"), csid))
            if not self.report_all:
                return None
        msg = self._base_message(
            buyer_id, role, bm.get("content"), bm.get("ts"),
            bm.get("msg_id"), bm.get("pre_msg_id"), csid,
            bm.get("nickname") if role == "user" else "",
        )
        msg["raw_type"] = bm.get("type")
        if reason:
            msg["skipped_reason"] = reason
            msg["is_diagnostic"] = True
        if history:
            msg["source"] = "pdd_cdp_history"
        return msg

    def _report_frame(self, e: dict, session: _Session | None, reason: str, kind: str) -> None:
        """非买家消息（系统/未知/空帧): 不再静默丢弃。

        report_all=true 时作为诊断事件照报给中心(带 skipped_reason/is_diagnostic),
        中心自有校验(如非法 role/buyer)可能拒收 —— 拒收也不等于丢, 原始帧已落本地归档。
        """
        self.reported[reason] = self.reported.get(reason, 0) + 1
        self._archive_raw(e, session, reason, kind)
        if not self.report_all:
            if reason in ("unknown_kind", "empty_frame"):
                self.drops["unparsed_frame"] += 1
                self._warn_throttled("unparsed_frame",
                                     "未识别帧(可能是二进制/新协议): kind=%s data=%s"
                                     % (kind, str(e.get("data"))[:80]))
            return
        self._emit(self._diagnostic_message(e, session, reason, kind))

    def _diagnostic_message(self, e: dict, session: _Session | None, reason: str, kind: str) -> dict:
        from .parser import _normalize_ts, _platform_time_key

        body = str(e.get("data") or "")
        account = _canonical_account((session.account if session is not None else self._account_hint) or "")
        digest = _stable_hash({"r": reason, "k": kind, "a": account, "b": body[:200], "t": e.get("t")})
        return {
            "platform": "pdd",
            "msg_id": "frame-" + digest,
            "platform_message_id": "frame-" + digest,
            "identity_kind": "frame_digest",
            "buyer_id": "",
            "role": "platform",
            "content": body[:500],
            "ts": int(_normalize_ts(e.get("t"))),
            "platform_ts_key": str(_platform_time_key(e.get("t")) or e.get("t") or ""),
            "parent_msg_id": "",
            "account": account,
            "buyer_nick": "",
            "source": "pdd_cdp_diagnostic",
            "delivery_status": "",
            "raw_type": kind,
            "frame_kind": kind,
            "skipped_reason": reason,
            "is_diagnostic": True,
        }

    def _normalize_send_message(self, p: dict, account_hint: str = "") -> dict | None:
        """send_message 回执帧 → 客服(mall_cs)方向消息（对齐探域 business_message 双向语义）。"""
        from_ = (p.get("obj") or {}).get("message", {}).get("from") or {}
        csid = self._account_for_frame(p.get("obj") or {}, seller_id(p) or account_hint)
        to_uid = _to_buyer_uid(p)
        seat_uid = _seat_uid(_canonical_account(csid))
        if not to_uid or (seat_uid and to_uid == seat_uid):
            # 回执帧自带的买家不可信（空 / 就是席位自己）：改用“我们自己发给谁”纠偏，
            # 否则会在中心生成一个只含 AI 发言的影子会话。帧里买家正常时维持原判，
            # 避免同内容并发发送被错配。
            pending = self._match_pending_send(p)
            if pending and pending.get("uid"):
                to_uid = str(pending["uid"])
                if pending.get("account"):
                    csid = str(pending["account"])
                    seat_uid = _seat_uid(_canonical_account(csid))
        reason = ""
        if not to_uid:
            reason = "no_buyer"
        elif seat_uid and str(to_uid) == seat_uid:
            reason = "seat_uid_as_buyer"
        if reason:
            self.reported[reason] = self.reported.get(reason, 0) + 1
            self._warn_throttled(reason, "send receipt %s: msg_id=%s account=%s"
                                 % (reason, p.get("msg_id"), csid))
            if not self.report_all:
                return None
        msg = self._base_message(
            to_uid, "mall_cs", p.get("content"), p.get("ts"),
            p.get("msg_id"), p.get("pre_msg_id"), csid, "",
        )
        msg["raw_type"] = p.get("type")
        if reason:
            msg["skipped_reason"] = reason
            msg["is_diagnostic"] = True
        return msg

    # ---------------- 发送 ----------------
    def _do_send(self, session: _Session, uid, content, account, event, timeout, csid) -> None:
        if event.expired():
            event.result = {"ok": False, "status": "expired", "real_send": False,
                            "retryable": False, "via": "expired"}
            event.set()
            return
        if session not in self.sessions or self.state != LISTENING:
            event.result = {
                "ok": False, "status": "failed", "real_send": False, "via": "cdp",
                "error_user": "CDP 通道未就绪，请确认 PDD 商家工作台已打开",
            }
            event.set()
            return
        # 防串台: 该会话页面的 globalMallId 与目标 account 的 mall 比对
        try:
            info = self._eval(session, "window.__pddBridge_info ? window.__pddBridge_info() : null")
        except Exception as exc:
            log.warning("__pddBridge_info error: %s", exc)
            info = None
        if isinstance(info, dict) and info.get("globalMallId"):
            from .pdd_context import mall_id_from_account

            target = mall_id_from_account(account)
            if target and str(info.get("globalMallId")) != str(target):
                event.result = {
                    "ok": False, "status": "blocked", "real_send": False, "via": "cdp_blocked",
                    "error_user": "当前工作台登录的店铺与目标账号不一致",
                    "mall_id": info.get("globalMallId"), "buyer_id": uid,
                }
                event.set()
                return
        # Serialize JavaScript arguments as JSON: Python None/repr are not JS literals.
        arguments = ", ".join(json.dumps(value, ensure_ascii=True)
                              for value in (str(uid), str(content), csid or None))
        expr = ("window.__pddBridge_sendText ? window.__pddBridge_sendText("
                + arguments + ") : {ok:false,err:'no bridge'}")
        if event.expired():
            event.result = {"ok": False, "status": "expired", "real_send": False,
                            "retryable": False, "via": "expired"}
            event.set()
            return
        # 直发快路：注入 DLL 能接就拿同步结果，接不上返回 None 继续走 CDP。
        # DLL 的 SendTextMsg 需要客服身份 csid；中心命令通常不带，按页面发送同一口径
        # （userInfo.nickname/username）补上。DLL 同步返回 "OK 0" 表示没发出去
        # （90科技 2026-11-25 实测 17 次 OK 0 均无出站回显），此时回退页面发送，
        # 不能直接判失败——否则页面通道可用也一条发不出去。
        if self.direct_send is not None:
            direct_csid = csid or self._page_sender_csid(session)
            direct = self.direct_send.send(uid, content, direct_csid, account)
            if direct is not None:
                if direct.get("ok") or str(direct.get("reply") or "").strip() != "OK 0":
                    event.result = direct
                    event.set()
                    return
                self.direct_fallback_count += 1
                self.last_direct_fallback = {
                    "at": time.time(), "uid": str(uid), "csid": direct_csid or "",
                    "reply": direct.get("reply"),
                }
                log.warning("DLL 直发返回 OK 0（uid=%s csid=%s），回退页面发送",
                            uid, direct_csid or "")
                if event.expired():
                    event.result = {"ok": False, "status": "expired", "real_send": False,
                                    "retryable": False, "via": "expired"}
                    event.set()
                    return
        key = (str(account), str(uid), str(content))
        with self._lock:
            self._pending_sends[key] = {
                "uid": str(uid), "content": str(content), "event": event,
                "deadline": time.time() + timeout, "account": str(account or ""),
            }
        try:
            r = self._eval(session, expr)
        except Exception as exc:
            log.warning("sendText eval error: %s", exc)
            r = None
        self.send_count += 1
        if event.result is not None:
            return
        if not (isinstance(r, dict) and r.get("ok") is True):
            with self._lock:
                self._pending_sends.pop(key, None)
            err = ""
            if isinstance(r, dict):
                err = str(r.get("err") or r)
            event.result = {
                "ok": False, "status": "failed", "real_send": False, "via": "cdp", "raw": r,
                "error_user": "发送调用失败: %s" % (err or "unknown"),
            }
            event.set()
            return
        # Pending is registered before eval so an immediate native echo can
        # confirm it while the CDP call is still returning.
        log.info("send accepted uid=%s account=%s csid=%s (等待回执)", uid, account, r.get("csid") or csid)

    def _match_pending_send(self, p: dict) -> dict | None:
        """回执帧里的 to.uid 不可信：优先认我们自己登记过的那次发送（uid 是我们发出的） 。

        按内容唯一匹配；同内容多条在途时不猜（先后由待确认表自身去重）。
        """
        content = str(p.get("content") or "")[:48]
        if not content:
            return None
        with self._lock:
            matches = [row for row in self._pending_sends.values()
                       if str(row.get("content") or "")[:48] == content]
        return matches[0] if len(matches) == 1 else None

    def _handle_send_receipt(self, p: dict, account_hint: str = "") -> None:
        if not p.get("msg_id") or str(p.get("msg_id")) == "99999999999999":
            return
        if (p.get("obj") or {}).get("result") not in (None, "ok"):
            return
        receipt_account = self._account_for_frame(p.get("obj") or {}, account_hint)
        to_uid = _to_buyer_uid(p)
        content = str(p.get("content") or "")
        key = (receipt_account, to_uid, content)
        with self._lock:
            pend = self._pending_sends.get(key)
            if pend and str(pend.get("content") or "") != content:
                pend = None
            if not pend:
                # 回执帧的 to.uid 可能是席位自己的 uid → 按内容唯一匹配兜底，
                # 否则老板收到的“已发送确认”会全部配不上而报未确认。
                # 0.8 把 key 改成带 account 后，命令没带 account / 页面只给商城号
                # 会整条配不上（已复现：pending 留 5s 后判 indeterminate）。
                # 所以兜底不能要求 account 完全相等：任意一侧为空即可用。
                matches = [(item_key, row) for item_key, row in self._pending_sends.items()
                           if str(row.get("content") or "") == content
                           and (not receipt_account or not row.get("account")
                                or row.get("account") == receipt_account)]
                if len(matches) != 1:
                    return
                key, pend = matches[0]
            del self._pending_sends[key]
        pend["event"].result = {
            "ok": True, "status": "confirmed", "real_send": True, "via": "cdp+ws",
            "request_id": p.get("request_id"), "msg_id": p.get("msg_id"),
            "buyer_id": str(pend.get("uid") or to_uid), "content": content,
        }
        pend["event"].set()
        log.info("send receipt confirmed buyer=%s msg_id=%s", to_uid, p.get("msg_id"))


# ======================================================================
# CDP 版平台辅助函数（platforms/pdd.py 分发入口，签名与 channel 对齐）
# ======================================================================

def channel_status_cdp(cfg: dict | None = None) -> dict:
    """CDP 通道状态（dll_ready=注入成功, cdp_port=实际端口）。"""
    cfg = cfg or {}
    src = cfg.get("_pddbridge_source")
    st = src.status() if src is not None else {}
    fixed = cfg.get("cdp_port")
    if fixed:
        try:
            alive = [p for p in (int(fixed),) if pdd_cdp.is_alive(int(fixed))]
        except (TypeError, ValueError):
            alive = []
        discovery = "configured"
    else:
        ports = pdd_cdp.discover_ports(only_pdd=True)
        alive = [p for p in sorted(ports) if pdd_cdp.is_alive(p)]
        discovery = "cdp_auto"
    port = alive[0] if alive else None
    injected = bool(st.get("injected"))
    page_ready = bool(st.get("page_send_ready", st.get("send_ready")))
    dll_ready = bool(st.get("dll_send_ready"))
    send_ready = page_ready or dll_ready
    if dll_ready:
        hint = "工作台发送：DLL 直发就绪"
    elif page_ready:
        hint = "工作台发送：页面通道就绪"
    elif injected:
        hint = "CDP 注入已就绪，但发送通道未就绪（页面无 socket 时需开 send_via_dll）"
    else:
        hint = "CDP 扫描中…"
    return {
        "dll_ready": send_ready,
        "dll_port": port,
        "cdp_port": port,
        "workbench_pid": None,
        "port_discovery": discovery,
        "receive_ready": injected,
        "send_ready": send_ready,
        "page_send_ready": page_ready,
        "dll_send_ready": dll_ready,
        "sessions": len(st.get("sessions") or []),
        "accounts": list(st.get("accounts") or []),
        "hint": hint,
        "source": "pdd_cdp",
    }


def open_chat_pdd_cdp(
    buyer_id: str,
    account: str,
    *,
    buyer_nick: str = "",
    cfg: dict | None = None,
) -> dict:
    """CDP 模式下的一键打开会话：CDP 注入不提供稳定的聚焦接口，返回引导提示。

    工作台会话切换是 UI 焦点操作，注入层没有可靠的跳转句柄；为避免误触，
    一律返回 unsupported，让使用者手动在官方客户端打开对应会话。
    """
    cfg = cfg or {}
    src = cfg.get("_pddbridge_source")
    st = src.status() if src is not None else {}
    return {
        "ok": False,
        "status": "unsupported",
        "real_send": False,
        "via": "open_chat_cdp",
        "buyer_id": str(buyer_id),
        "account": str(account),
        "buyer_nick": str(buyer_nick or ""),
        "cdp_injected": bool(st.get("injected")),
        "error_user": "CDP 模式暂不支持一键跳转会话，请在 PDD 工作台手动打开该买家会话",
    }


def send_text_cdp(
    buyer_id: str,
    content: str,
    account: str,
    *,
    cfg: dict | None = None,
    dry_run: bool = False,
) -> dict:
    """CDP 发送：走 PddbridgeSource.send_and_wait，等 send_message 帧回执。

    dry_run=True 时只做通道自检，不真正调用 __pddBridge_sendText。
    """
    cfg = cfg or {}
    src = cfg.get("_pddbridge_source")
    base = {
        "buyer_id": str(buyer_id),
        "content": str(content),
        "account": str(account),
        "via": "cdp",
    }
    if dry_run:
        return {
            **base, "ok": True, "status": "accepted", "real_send": False,
            "via": "dry_run", "error_user": "",
        }
    if src is None:
        return {
            **base, "ok": False, "status": "failed", "real_send": False,
            "error_user": "CDP 通道未初始化，请确认 PDD 商家工作台已打开",
        }
    st = src.status()
    if st.get("state") != "listening":
        return {
            **base, "ok": False, "status": "failed", "real_send": False,
            "error_user": "CDP 通道未就绪（当前 %s），请确认 PDD 商家工作台已打开"
            % st.get("state"),
        }
    if not st.get("send_ready"):
        return {
            **base, "ok": False, "status": "failed", "real_send": False,
            "error": "send_channel_not_ready",
            "error_user": "工作台发送通道未就绪",
        }
    try:
        return src.send_and_wait(buyer_id, content, account, timeout=5.0,
                                 is_expired=cfg.get("_command_is_expired"))
    except Exception as exc:
        log.warning("send_text_cdp error: %s", exc)
        return {
            **base, "ok": False, "status": "failed", "real_send": False,
            "error_user": "发送异常: %s" % exc,
        }
