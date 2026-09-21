# -*- coding: utf-8 -*-
"""中心大脑 WebSocket 上行通道（protocol_version 1）。

职责边界（对照服务端实现）：
    上行事件 event → ack / result、ping/pong、ready。
    **下行指令仍走 HTTP 长轮询** —— 服务端尚未实现 send_command 的 WS 通道，
    所以本模块只替换 `BridgeClient.upload_events`，register / heartbeat /
    pull_commands / report_command_result 全部保持 HTTP 不变。

出队判据（关键，决定会不会丢消息）：
    - `ack: accepted`  已 INSERT 进服务端落表。服务端自己的 consumer 线程会
      claim 并处理，**与客户端是否在线无关** → 可安全出队。
    - `ack: duplicate` 已存在（重连补投）→ 可安全出队。
    - `ack: rejected` + MALFORMED_FRAME / UNKNOWN_FRAME_TYPE → 客户端 bug，
      重发无意义 → 终态，落死信。
    - `ack: rejected` + PERSISTENCE_UNAVAILABLE → 缺 account/buyer_id 的帧
      （诊断帧）是确定性拒绝，按终态处理；其余按可重试。
    - `result` 是业务终态（processed/rejected/retry），异步且**服务端不补投**，
      单独交给调用方记账/落死信，不参与出队。

`send_events` 返回与 HTTP 完全相同的 `{"ack_version": 1, "event_acks": [...]}`，
因此 `bridge/agent.py::_flush_events` 的去重、台账、死信逻辑可以原样复用。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from . import __version__

log = logging.getLogger("pdd.bridge")

PROTOCOL_VERSION = 1
DEFAULT_WS_PATH = "/api/bridge/v1/ws"
MAX_FRAME_BYTES = 4 * 1024 * 1024          # 服务端 max_size，超了会被直接断开
PING_INTERVAL = 20.0                        # 服务端 ping_interval=20s / ping_timeout=10s
PING_TIMEOUT = 10.0
RECONNECT_MIN_SECONDS = 1.0
RECONNECT_MAX_SECONDS = 60.0
READY_TIMEOUT_SECONDS = 15.0
_RESULT_BACKLOG_LIMIT = 10000

# 帧顶层字段。其余全部塞进 payload，保证"换传输层不丢字段"。
_FRAME_FIELDS = frozenset((
    "event_id", "sequence", "account", "buyer_id", "role", "content", "msg_id",
    "captured_at_ms", "payload",
))
# 不随事件上行：agent_id 由服务端按 token 解析覆盖（契约明确要求不要传）。
_PAYLOAD_DROP = frozenset(("agent_id", "idempotency_key"))


class CenterWsError(RuntimeError):
    """WS 通道不可用。调用方应回落到 HTTP 上行。"""


class CenterWsUnavailable(CenterWsError):
    """当前没有可用连接（未启动 / 断线中 / 协议不匹配）。"""


class _AuthFailed(CenterWsError):
    """服务端 close(4001)：token 或设备绑定不对，快速重试没有意义。"""


class _ProtocolMismatch(CenterWsError):
    """ready.protocol_version 不是 1。"""


def ws_url_from_server_url(server_url: str, path: str = DEFAULT_WS_PATH) -> str:
    """由现有 server_url 推导 WS 地址：http→ws / https→wss + 路径。

    中心 HTTP 与 WS 同主机同端口（实测 18765），不需要新增主机配置。
    """
    base = str(server_url or "").strip().rstrip("/")
    if not base:
        raise CenterWsUnavailable("server_url is empty")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    elif not base.startswith(("ws://", "wss://")):
        raise CenterWsUnavailable("server_url 既不是 http(s) 也不是 ws(s)")
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def _connect_header_kwarg() -> str:
    """websockets 13.x 用 extra_headers，14.0 起改名 additional_headers。

    服务端锁 13.1，但冻结构建里打包的是 16.1.1（本地开发环境是 13.1），
    两代都必须能跑，所以运行时探测。
    """
    try:
        import inspect

        import websockets

        parameters = inspect.signature(websockets.connect).parameters
        if "additional_headers" in parameters:
            return "additional_headers"
        if "extra_headers" in parameters:
            return "extra_headers"
    except Exception:  # noqa: BLE001 - 探测失败按新版兜底
        pass
    return "additional_headers"


def build_event_frame(event: dict, sequence: int) -> dict:
    """把内部 event dict 拍平成契约要求的顶层帧。

    契约要求顶层必须有 account / buyer_id（嵌套信封默认被拒），其余字段原样进
    payload，避免换传输层丢上下文。
    """
    event = event if isinstance(event, dict) else {}
    captured_ms = event.get("captured_at_ms")
    if not captured_ms:
        try:
            captured_ms = int(round(float(event.get("captured_at") or 0.0) * 1000))
        except (TypeError, ValueError):
            captured_ms = 0
    payload = {
        key: value for key, value in event.items()
        if key not in _FRAME_FIELDS and key not in _PAYLOAD_DROP
    }
    return {
        "type": "event",
        "event_id": str(event.get("event_id") or event.get("idempotency_key") or ""),
        "sequence": int(sequence),
        "account": str(event.get("account") or ""),
        "buyer_id": str(event.get("buyer_id") or ""),
        "role": str(event.get("role") or ""),
        "content": event.get("content"),
        # 落表与 HTTP 兜底路径共用同一消息身份，优先用真正的平台 id。
        "msg_id": str(event.get("platform_message_id") or event.get("msg_id") or ""),
        "captured_at_ms": int(captured_ms or 0),
        "payload": payload,
    }


def ack_row_for(event: dict, status: str, error_code: str) -> Optional[dict]:
    """WS ack 帧 → `_flush_events` 认的 event_acks 行（含 event_id）。

    status 取值必须落在 `_flush_events` 的既有判据上：
      - `terminal_ids = committed is True or retryable is False`
      - `refused`（写死信）只收 status ∈ {ignored, rejected}
    所以**可重试**的拒收用 `status="retry"`：既保住待重试，又不会被误写进死信。
    """
    event_id = str(event.get("event_id") or event.get("idempotency_key") or "")
    if status in ("accepted", "duplicate"):
        return {"event_id": event_id, "status": "accepted",
                "committed": True, "retryable": False}
    if error_code == "PERSISTENCE_UNAVAILABLE":
        # 缺 account/buyer_id 的帧（诊断帧）是确定性拒绝，重发也不会变 → 终态。
        missing_key = not str(event.get("account") or "").strip() \
            or not str(event.get("buyer_id") or "").strip()
        if missing_key:
            return {"event_id": event_id, "status": "rejected", "committed": False,
                    "retryable": False, "reason": error_code}
        return {"event_id": event_id, "status": "retry", "committed": False,
                "retryable": True, "reason": error_code}
    # MALFORMED_FRAME / UNKNOWN_FRAME_TYPE：客户端 bug，重发无意义。
    return {"event_id": event_id, "status": "rejected", "committed": False,
            "retryable": False, "reason": error_code or status or "rejected"}


class _Batch:
    """一次 send_events 的等待槽。ack 按 event_id 归集。"""

    __slots__ = ("events", "rows", "done", "_lock")

    def __init__(self, events: List[dict]):
        self.events: Dict[str, dict] = {
            str(e.get("event_id") or e.get("idempotency_key") or ""): e for e in events
        }
        self.rows: Dict[str, dict] = {}
        self.done = threading.Event()
        self._lock = threading.Lock()

    def on_ack(self, event_id: str, row: dict) -> None:
        with self._lock:
            if event_id not in self.events or event_id in self.rows:
                return
            self.rows[event_id] = row
            complete = len(self.rows) >= len(self.events)
        if complete:
            self.done.set()


class CenterEventChannel:
    """进程内单例即可：10 个店铺共用一条 WS 连接。"""

    def __init__(
        self,
        *,
        ws_url: str,
        token: str,
        agent_id: str,
        device_id: str = "",
        platform: str = "pdd",
    ) -> None:
        self.ws_url = str(ws_url or "")
        self.token = str(token or "")
        self.agent_id = str(agent_id or "")
        self.device_id = str(device_id or "")
        self.platform = str(platform or "pdd")

        self._state_lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws: Any = None
        self._outbound: Optional[asyncio.Queue] = None
        self._thread: Optional[threading.Thread] = None
        self._connected = False
        self._stop = threading.Event()
        self._sequence = 0

        # event_id -> 等待槽。WS 线程读、调用线程写，用锁护住。
        self._owner_lock = threading.Lock()
        self._owner: Dict[str, _Batch] = {}

        # 业务终态 result，交给 agent 的记账线程取走
        self._results: List[dict] = []
        self._results_lock = threading.Lock()

        self.last_error = ""
        self.max_inflight = 0
        self.connected_at = 0.0
        # 本轮重连尝试**是否握上手过**。用来区分"压根连不上"和"连上又断了"：
        # 后者是网络抖动，退避必须回到最小（见 _run 的 except 分支）。
        self._attempt_established = False
        self.frames_sent = 0
        self.acks_received = 0
        self.results_received = 0

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> bool:
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            try:
                import websockets  # noqa: F401
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"websockets 不可用: {exc}"
                log.warning("中心 WS 通道未启动：%s", self.last_error)
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._thread_main, name="center-ws", daemon=True)
            self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        # 关掉活动连接，否则 `async for raw in ws` 会一直挂着等到下一条帧。
        with self._state_lock:
            loop, ws, thread = self._loop, self._ws, self._thread
        if loop is not None and ws is not None and not loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(ws.close(), loop)
            except Exception:  # noqa: BLE001
                pass
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        with self._state_lock:
            self._thread = None
            self._loop = None
            self._ws = None
            self._outbound = None
            self._connected = False

    @property
    def available(self) -> bool:
        with self._state_lock:
            return bool(self._connected)

    def status(self) -> dict:
        with self._state_lock:
            connected = bool(self._connected)
            connected_at = self.connected_at
            outbound = self._outbound
        pending = outbound.qsize() if outbound is not None else 0
        with self._owner_lock:
            waiting = len(self._owner)
        return {
            "connected": connected,
            "url": self.ws_url,
            "protocol_version": PROTOCOL_VERSION,
            "max_inflight": self.max_inflight,
            "connected_seconds": round(time.time() - connected_at, 1) if connected else 0.0,
            "frames_sent": self.frames_sent,
            "acks_received": self.acks_received,
            "results_received": self.results_received,
            "pending_frames": pending,
            "awaiting_batches": waiting,
            "last_error": self.last_error,
        }

    # ------------------------------------------------------------ 上行（同步 API）
    def send_events(self, batch: List[dict], *, timeout: float = 10.0) -> dict:
        """阻塞直到本批 ack 归集齐备或超时；返回 HTTP 同形的 ack dict。

        连接不可用时抛 `CenterWsUnavailable`，调用方回落 HTTP。
        多条上传线程会并发调用（upload_concurrency 默认 3），各批 event_id 由
        `_flush_events` 的 `_inflight_ids` 保证互不重叠，所以按 event_id 归集安全。
        """
        events = [e for e in (batch or []) if isinstance(e, dict)]
        events = [e for e in events
                  if str(e.get("event_id") or e.get("idempotency_key") or "")]
        if not events:
            return {"ack_version": 1, "event_acks": []}

        with self._state_lock:
            loop, outbound = self._loop, self._outbound
            if not self._connected or loop is None or outbound is None or loop.is_closed():
                raise CenterWsUnavailable(self.last_error or "中心 WS 未连接")

        waiter = _Batch(events)
        with self._owner_lock:
            for event_id in waiter.events:
                self._owner[event_id] = waiter

        sent = 0
        try:
            for event in events:
                with self._state_lock:
                    self._sequence += 1
                    sequence = self._sequence
                encoded = json.dumps(build_event_frame(event, sequence), ensure_ascii=False)
                if len(encoded.encode("utf-8")) > MAX_FRAME_BYTES:
                    # 超帧会被服务端直接断开连接，宁可本地跳过一条也不能拖垮整条通道。
                    log.warning("中心 WS 单帧超过 %d 字节，已跳过 event_id=%s",
                                MAX_FRAME_BYTES, event.get("event_id"))
                    continue
                asyncio.run_coroutine_threadsafe(outbound.put(encoded), loop)
                sent += 1
            if not sent:
                raise CenterWsUnavailable("本批全部超过单帧上限")
            self.frames_sent += sent
        except CenterWsUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise CenterWsUnavailable(f"投递到 WS 发送队列失败: {exc}") from exc
        finally:
            if sent == 0:
                with self._owner_lock:
                    for event_id in waiter.events:
                        self._owner.pop(event_id, None)

        waiter.done.wait(timeout=max(0.1, float(timeout)))
        with self._owner_lock:
            for event_id in waiter.events:
                self._owner.pop(event_id, None)
        # 只回本批真正发出去的事件；被跳过的超帧不出现在 ack 里，
        # `_flush_events` 会把它留在 pending 等下一轮。
        with waiter._lock:
            rows = [waiter.rows[i] for i in waiter.events if i in waiter.rows]
        return {"ack_version": 1, "event_acks": rows}

    def drain_results(self, limit: int = 200) -> List[dict]:
        """取走并清空已收到的业务终态 result（processed/rejected/retry）。"""
        with self._results_lock:
            rows = self._results[:limit]
            del self._results[:limit]
            return rows

    # ---------------------------------------------------------------- 线程主循环
    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"ws 线程退出: {exc}"
            log.exception("中心 WS 线程异常退出")
        finally:
            with self._state_lock:
                self._connected = False

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        with self._state_lock:
            self._loop = loop
            self._outbound = asyncio.Queue(maxsize=4096)
        delay = RECONNECT_MIN_SECONDS
        while not self._stop.is_set():
            self._attempt_established = False
            try:
                await self._session()
                delay = RECONNECT_MIN_SECONDS
            except asyncio.CancelledError:
                raise
            except _AuthFailed as exc:
                # 4001：token / 设备绑定不对，快速重试只会反复被拒。
                self.last_error = f"认证失败: {exc}"
                log.error("中心 WS 认证失败（退避到上限再试）: %s", exc)
                delay = RECONNECT_MAX_SECONDS
            except _ProtocolMismatch as exc:
                self.last_error = str(exc)
                log.error("中心 WS 协议不匹配，回落到 HTTP 上行: %s", exc)
                delay = RECONNECT_MAX_SECONDS
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"连接断开: {exc}"
                # 退避只对"压根连不上"累加。握上手之后又断，是网络抖动 ——
                # 必须回到最小间隔。原来只在 _session() **正常返回**时重置，
                # 而它只要断开就抛异常，所以那个重置永远跑不到：每抖一次间隔翻倍，
                # 几轮之后就固定 60 秒重连一次（实测日志里的 8→16→32→60s 就是这个）。
                if self._attempt_established:
                    delay = RECONNECT_MIN_SECONDS
                log.warning("中心 WS 连接断开（%.0fs 后重连）: %s", delay, exc)
            with self._state_lock:
                self._connected = False
                self._ws = None
            if self._stop.is_set():
                break
            await asyncio.sleep(delay)
            delay = min(RECONNECT_MAX_SECONDS, max(RECONNECT_MIN_SECONDS, delay * 2.0))

    async def _session(self) -> None:
        import websockets
        from websockets.exceptions import ConnectionClosed

        headers = {
            # 注意：WS 用 Bearer，与 HTTP 通道的 X-Agent-Token 不同。
            "Authorization": f"Bearer {self.token}",
            "X-Agent-Id": self.agent_id,
            "X-Device-Id": self.device_id or self.agent_id,
            "X-Platform": self.platform,
            "User-Agent": f"PddBridgeAgent/{__version__}",
        }
        kwargs: Dict[str, Any] = {
            "max_size": MAX_FRAME_BYTES,
            "ping_interval": PING_INTERVAL,
            "ping_timeout": PING_TIMEOUT,
        }
        kwargs[_connect_header_kwarg()] = headers

        async with websockets.connect(self.ws_url, **kwargs) as ws:
            try:
                ready = json.loads(await asyncio.wait_for(ws.recv(), timeout=READY_TIMEOUT_SECONDS))
            except ConnectionClosed as exc:
                # 服务端认证失败是 close(4001, reason)，握手本身是成功的。
                if getattr(exc, "code", None) == 4001 or \
                        getattr(getattr(exc, "rcvd", None), "code", None) == 4001:
                    reason = getattr(getattr(exc, "rcvd", None), "reason", "") or ""
                    raise _AuthFailed(reason or "服务端 4001") from exc
                raise
            if not isinstance(ready, dict) or ready.get("type") != "ready":
                raise _ProtocolMismatch(f"首帧不是 ready: {str(ready)[:120]}")
            version = ready.get("protocol_version")
            if version != PROTOCOL_VERSION:
                raise _ProtocolMismatch(f"protocol_version={version} 需要 {PROTOCOL_VERSION}")

            self.max_inflight = int(ready.get("max_inflight") or 0)
            with self._state_lock:
                self._connected = True
                self._ws = ws
                self.connected_at = time.time()
                self._attempt_established = True
                self.last_error = ""
            log.info("中心 WS 已连接 connection_id=%s max_inflight=%s",
                     ready.get("connection_id"), self.max_inflight)

            sender = asyncio.create_task(self._sender(ws))
            try:
                async for raw in ws:
                    self._on_frame(raw)
            finally:
                sender.cancel()
                with self._state_lock:
                    self._connected = False
                    self._ws = None

    async def _sender(self, ws) -> None:
        outbound = self._outbound
        if outbound is None:
            return
        while True:
            encoded = await outbound.get()
            await ws.send(encoded)

    # ------------------------------------------------------------ 下行帧分发
    def _on_frame(self, raw) -> None:
        try:
            frame = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(frame, dict):
            return
        kind = frame.get("type")
        if kind == "ack":
            self._on_ack(frame)
        elif kind == "result":
            self._on_result(frame)
        # pong 无需处理：连接存活由协议级 ping/pong 与读循环本身保证。

    def _on_ack(self, frame: dict) -> None:
        event_id = str(frame.get("event_id") or "")
        if not event_id:
            return
        self.acks_received += 1
        with self._owner_lock:
            waiter = self._owner.get(event_id)
        if waiter is None:
            return
        event = waiter.events.get(event_id) or {}
        row = ack_row_for(event, str(frame.get("status") or ""),
                          str(frame.get("error_code") or ""))
        if row is not None:
            waiter.on_ack(event_id, row)

    def _on_result(self, frame: dict) -> None:
        event_id = str(frame.get("event_id") or "")
        if not event_id:
            return
        self.results_received += 1
        row = {
            "event_id": event_id,
            "status": str(frame.get("status") or ""),
            "error_code": str(frame.get("error_code") or ""),
            "processed_at_ms": int(frame.get("server_processed_at_ms") or 0),
        }
        with self._results_lock:
            # 服务端不补投 result，这里只做有界缓存，防止长时间无人取走时涨内存。
            if len(self._results) < _RESULT_BACKLOG_LIMIT:
                self._results.append(row)
