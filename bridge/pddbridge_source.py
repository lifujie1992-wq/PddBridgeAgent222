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
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .parser import _canonical_account
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
    """一个页面上下文 = 一个账号/店铺的实时通道（多店铺就是多个会话）。"""

    __slots__ = ("port", "target_url", "cdp", "cid", "account", "mall_id",
                 "last_health", "push_ok", "frames")

    def __init__(self, port: int, target_url: str, cdp, cid: int):
        self.port = port
        self.target_url = target_url or ""
        self.cdp = cdp
        self.cid = cid
        self.account = ""
        self.mall_id = ""
        self.last_health = 0.0
        self.push_ok = False
        self.frames = 0

    def key(self) -> str:
        return "%s|%s|%s" % (self.port, self.cid, self.target_url)

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
        self.send_count = 0
        self.last_error = ""

        self._seen: dict = {}    # (kind,msg_id)->ts 去重
        self._seen_any: dict = {}  # msg_id->ts 跨帧去重（push 与 send_message 互去）
        self._pending_sends: dict = {}  # "uid|content前48" -> 记录
        self._act: list = []
        self._lock = threading.Lock()
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
            "context_unattached": 0,  # 发现上下文但注入失败（该店铺收不到消息）
            "dedup_py": 0,            # Python 侧判重命中（同一条只报一次）
            "unparsed_frame": 0,      # 只有 report_all=false 时才会计数
            "frame_error": 0,         # 帧处理异常
        }
        self._js_stats: dict = {}
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
                log.info("原生接收通道已启动（slot=%s）", self.cfg.get("recv_slot"))
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

    def status(self) -> dict:
        return {
            "state": self.state,
            "port": self.sessions[0].port if self.sessions else None,
            "injected": bool(self.sessions),
            "msg_count": self.msg_count,
            "send_count": self.send_count,
            "sessions": [
                {"port": s.port, "ctx": s.cid, "account": s.account, "mall_id": s.mall_id,
                 "push": s.push_ok, "frames": s.frames, "url": (s.target_url or "")[:80]}
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
            key = "%s|%s" % (str(uid), str(content)[:48])
            with self._lock:
                event.cancelled = True
                self._act = [(name, args) for name, args in self._act
                             if not (name == "send" and args[3] is event)]
                if key in self._pending_sends:
                    del self._pending_sends[key]
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

    # ---------------- 事件/状态 ----------------
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
        for session in list(self.sessions):
            session.close()
        self.sessions = []
        self.port = None
        self._account_hint = ""

    # 旧名保留: 只有一个连接时等价于关全部
    _close_cdp = _close_sessions

    def _sleep(self, sec: float) -> None:
        self._stop_evt.wait(sec)

    # ---------------- 取帧与存活判定 ----------------
    def _drain(self, session: _Session):
        """取一帧批次, 同时判定该页面注入是否还活着。

        返回 (frames, hooked, lossless)。
        lossless=True 表示用了 peek（未删数据），调用方处理完必须 _ack；
        旧注入脚本无 peek 时返回 False，行为同旧版（先清后传，CDP 失败即丢）。
        """
        r = session.cdp.eval(_DRAIN_EXPR, session.cid)
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
            session.cdp.eval(_ACK_EXPR % int(count), session.cid)
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
        return "ok"

    # ---------------- 推送通道（页面直推, 免轮询） ----------------
    def _push_url(self) -> str:
        if not self._push.port:
            return ""
        return "http://127.0.0.1:%d/push/%s" % (self._push.port, self._push.token)

    def _inject_payload(self, session: _Session) -> str:
        prelude = (
            "window.__pddBridge_push_url = %r;\n"
            "window.__pddBridge_session = %r;\n"
        ) % (self._push_url(), "%s|%s" % (session.port, session.cid))
        return prelude + _load_inject()

    def _session_by_sid(self, sid: str):
        for session in self.sessions:
            if "%s|%s" % (session.port, session.cid) == sid:
                return session
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
    def _dedup(self, kind: str, msg_id) -> bool:
        if self.dedup_off or not msg_id:
            return False
        key_str = str(msg_id)
        if _is_fallback_id(key_str):
            # 没有平台 msg_id 时用 content+time 哈希兜底, 同秒同内容会撞哈希,
            # 绝不能拿它当去重依据(否则会误杀真实消息)。
            return False
        now = time.time()
        key = (kind, key_str)
        last = self._seen.get(key)
        if last is not None and now - last < 120:
            self._seen[key] = now
            return True
        self._seen[key] = now
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

    def _seen_before(self, msg_id) -> bool:
        if self.dedup_off or not msg_id:
            return False
        key_str = str(msg_id)
        if _is_fallback_id(key_str):
            return False
        now = time.time()
        last = self._seen_any.get(key_str)
        if last is not None and now - last < 120:
            return True
        self._seen_any[key_str] = now
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
    def feed_native_frame(self, raw: str) -> None:
        """原生 DLL 通道的 push 帧（v0.7）：与注入脚本的 entry 同构，复用帧处理管线。"""
        self._handle_frame({"dir": "in", "data": raw, "t": int(time.time() * 1000),
                            "chan": "native"})

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
        known = {s.key() for s in self.sessions}
        skipped: list = []
        for port in ports:
            try:
                hits = pdd_cdp.find_socketutil_sessions(port)
            except Exception as exc:
                log.warning("find_socketutil_sessions port=%s error: %s", port, exc)
                continue
            for hit in hits:
                session = _Session(port, hit.get("url") or "", hit["cdp"], hit["cid"])
                if session.key() in known:
                    session.close()      # 已在挂, 不重复开连接
                    continue
                try:
                    r = session.cdp.eval(self._inject_payload(session), session.cid)
                except Exception as exc:
                    log.warning("inject eval port=%s error: %s", port, exc)
                    r = None
                if not (isinstance(r, dict) and r.get("ok") is True
                        and r.get("attached") == 1 and not r.get("reloading")):
                    log.warning("inject not ready port=%s => %s", port, r)
                    session.close()
                    skipped.append(session.target_url or str(port))
                    continue
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
        session.close()
        try:
            self.sessions.remove(session)
        except ValueError:
            pass
        if not self.sessions:
            self.port = None

    def _health_check(self) -> None:
        for session in list(self.sessions):
            if not pdd_cdp.is_alive(session.port):
                log.warning("health check failed %s -> 丢弃该会话", session.label())
                self._drop_session(session)
                continue
            self._refresh_account_hint(session)
        self._last_health = time.time()

    def _handle_actions(self) -> bool:
        """执行投递动作, 返回是否要求重连。"""
        with self._lock:
            acts, self._act = self._act, []
        reconnect = False
        for name, a in acts:
            if name == "send":
                self._do_send_action(*a)
            elif name == "pull_history":
                self._do_pull_history(*a)
            elif name == "reconnect":
                reconnect = True
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
            r = session.cdp.eval(expr, session.cid)
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
        health = float(self.cfg.get("health_interval") or 5.0)
        rescan = float(self.cfg.get("cdp_rescan_seconds") or 20)
        scan_deadline = time.time() + fallback_after
        next_rescan = 0.0

        while not self._stop_evt.is_set():
            if self._handle_actions():
                self._close_sessions()
                self._set_state(SCANNING)
                scan_deadline = time.time() + fallback_after
                next_rescan = 0.0

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

            # LISTENING
            now = time.time()
            if now >= next_rescan:
                # 运行时新增账号 / 切店铺后页面重载: 补挂新上下文, 不动已挂的
                self._scan_and_attach()
                next_rescan = now + rescan
            for session in list(self.sessions):
                if self._pump_session(session) != "ok":
                    self._drop_session(session)
            if not self.sessions:
                log.warning("pddbridge 全部会话注入丢失 -> 重新扫描")
                self._set_state(SCANNING)
                scan_deadline = time.time() + fallback_after
                next_rescan = 0.0
                continue
            if now - self._last_health > health:
                self._health_check()
            self._wake.wait(poll)   # 有推送就立刻醒; 没推送就 200ms 轮一次
            self._wake.clear()

        self._close_sessions()

    # ---------------- 帧处理 ----------------
    def _handle_frame(self, e: dict, session: _Session | None = None) -> None:
        hint = session.account if session is not None else ""
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
            if self._dedup("push", p.get("msg_id")):
                self.drops["dedup_py"] += 1
                return
            bm = buyer_message(p)
            msg = self._normalize_message(bm, history=history_flag, account_hint=hint)
            if msg and not self._seen_before(msg.get("msg_id")):
                self.msg_count += 1
                msg["captured_at_ms"] = e.get("t")
                self._emit(msg)
            elif msg:
                self.drops["dedup_py"] += 1
        elif kind == "send_message":
            self._handle_send_receipt(p)
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
            info = session.cdp.eval(
                "window.__pddBridge_info ? window.__pddBridge_info() : null", session.cid
            )
        except Exception:
            return
        if not isinstance(info, dict):
            return
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

    def _normalize_message(self, bm: dict, history: bool = False,
                           account_hint: str = "") -> dict | None:
        """买家方向消息归一化。异常帧不再丢弃: 带原因标记照报, 回不回由大脑决定。"""
        buyer_id = str(bm.get("buyer_id") or "")
        role = bm.get("from_role") or "user"
        csid = bm.get("seller_id") or account_hint or self._account_hint
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
            bm.get("nickname"),
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
        csid = seller_id(p) or account_hint or self._account_hint or from_.get("uid")
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
            info = session.cdp.eval("window.__pddBridge_info ? window.__pddBridge_info() : null", session.cid)
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
        expr = ("window.__pddBridge_sendText ? "
                "window.__pddBridge_sendText(%r, %r, %r) : "
                "{ok:false,err:'no bridge'}" % (str(uid), str(content), csid or None))
        if event.expired():
            event.result = {"ok": False, "status": "expired", "real_send": False,
                            "retryable": False, "via": "expired"}
            event.set()
            return
        # 直发快路：注入 DLL 能接就拿同步结果，接不上返回 None 继续走 CDP
        if self.direct_send is not None:
            direct = self.direct_send.send(uid, content, csid, account)
            if direct is not None:
                event.result = direct
                event.set()
                return
        try:
            r = session.cdp.eval(expr, session.cid)
        except Exception as exc:
            log.warning("sendText eval error: %s", exc)
            r = None
        self.send_count += 1
        if not (isinstance(r, dict) and r.get("ok") is True):
            err = ""
            if isinstance(r, dict):
                err = str(r.get("err") or r)
            event.result = {
                "ok": False, "status": "failed", "real_send": False, "via": "cdp", "raw": r,
                "error_user": "发送调用失败: %s" % (err or "unknown"),
            }
            event.set()
            return
        # 登记 pending 回执, 等 drain 里的 send_message 帧
        key = "%s|%s" % (str(uid), str(content)[:48])
        with self._lock:
            self._pending_sends[key] = {
                "uid": str(uid), "content": str(content), "event": event,
                "deadline": time.time() + timeout, "account": str(account or ""),
            }
        log.info("send accepted uid=%s csid=%s (等待回执)", uid, csid)

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

    def _handle_send_receipt(self, p: dict) -> None:
        to_uid = _to_buyer_uid(p)
        content = str(p.get("content") or "")
        key = "%s|%s" % (to_uid, content[:48])
        with self._lock:
            pend = self._pending_sends.get(key)
            if not pend:
                # 回执帧的 to.uid 可能是席位自己的 uid → 按内容唯一匹配兜底，
                # 否则老板收到的“已发送确认”会全部配不上而报未确认。
                matches = [(item_key, row) for item_key, row in self._pending_sends.items()
                           if str(row.get("content") or "")[:48] == content[:48]]
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
    return {
        "dll_ready": injected,
        "dll_port": port,
        "cdp_port": port,
        "workbench_pid": None,
        "port_discovery": discovery,
        "receive_ready": injected,
        "send_ready": injected and st.get("state") == "listening",
        "sessions": len(st.get("sessions") or []),
        "accounts": list(st.get("accounts") or []),
        "hint": ("CDP 注入已就绪（%d 个店铺账号）" % len(st.get("accounts") or []))
                if (injected and st.get("accounts")) else
                ("CDP 注入已就绪（PDD 实时）" if injected else "CDP 扫描中…"),
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
    try:
        return src.send_and_wait(buyer_id, content, account, timeout=5.0,
                                 is_expired=cfg.get("_command_is_expired"))
    except Exception as exc:
        log.warning("send_text_cdp error: %s", exc)
        return {
            **base, "ok": False, "status": "failed", "real_send": False,
            "error_user": "发送异常: %s" % exc,
        }
