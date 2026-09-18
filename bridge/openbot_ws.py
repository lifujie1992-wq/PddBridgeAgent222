# -*- coding: utf-8 -*-
"""Openbot-compatible local WebSocket bridge for 千牛 imsdk.

千牛 recent.html 注入脚本连接 ws://127.0.0.1:41010 ，收到:
  {"method":"execute","expression":"imsdk.invoke(...)"}
回:
  {"type":"execute","response":"<json string>"}
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("pdd.bridge.openbot_ws")

DEFAULT_WS_PORT = 41010
DEFAULT_HTTP_PORT = 41011

_STATE_LOCK = threading.RLock()
_SERVER_THREAD: Optional[threading.Thread] = None
_LOOP: Optional[asyncio.AbstractEventLoop] = None
_CONNECTED: Dict[str, Any] = {}  # session_id -> websocket
_PENDING: Dict[str, "asyncio.Future"] = {}
_LAST_CONNECT_TS = 0.0
_STARTED = False


def is_client_connected() -> bool:
    with _STATE_LOCK:
        return bool(_CONNECTED)


def connection_count() -> int:
    with _STATE_LOCK:
        return len(_CONNECTED)


def last_connect_age_sec() -> Optional[float]:
    with _STATE_LOCK:
        if not _LAST_CONNECT_TS:
            return None
        return max(0.0, time.time() - _LAST_CONNECT_TS)


def status_snapshot() -> dict:
    return {
        "openbot_ws_port": DEFAULT_WS_PORT,
        "openbot_http_port": DEFAULT_HTTP_PORT,
        "openbot_connected": is_client_connected(),
        "openbot_clients": connection_count(),
        "openbot_last_connect_age_sec": last_connect_age_sec(),
        "openbot_started": _STARTED,
    }


def _assets_bridge_js() -> str:
    import sys

    candidates = [
        Path(__file__).resolve().parent / "assets" / "qn_imsdk_bridge.js",
    ]
    if getattr(sys, "frozen", False):
        meipass = Path(getattr(sys, "_MEIPASS", "") or "")
        exe_dir = Path(sys.executable).resolve().parent
        candidates = [
            meipass / "bridge" / "assets" / "qn_imsdk_bridge.js",
            meipass / "assets" / "qn_imsdk_bridge.js",
            exe_dir / "bridge" / "assets" / "qn_imsdk_bridge.js",
            *candidates,
        ]
    for p in candidates:
        try:
            if p.is_file():
                return p.read_text(encoding="utf-8")
        except Exception:
            continue
    return "console.error('qn_imsdk_bridge.js missing');"


async def _handle_ws(websocket):
    global _LAST_CONNECT_TS
    sid = uuid.uuid4().hex[:12]
    with _STATE_LOCK:
        _CONNECTED[sid] = websocket
        _LAST_CONNECT_TS = time.time()
    log.info("openbot ws client connected id=%s peers=%s", sid, len(_CONNECTED))
    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            mtype = str(msg.get("type") or "")
            if mtype == "hi":
                continue
            if mtype == "execute":
                # response for the oldest pending invoke (FIFO)
                resp = msg.get("response")
                fut: Optional[asyncio.Future] = None
                with _STATE_LOCK:
                    # pick first pending future
                    if _PENDING:
                        key = next(iter(_PENDING))
                        fut = _PENDING.pop(key, None)
                if fut is not None and not fut.done():
                    fut.set_result(resp)
            # other event types (receiveNewMsg etc.) ignored for send path
    except Exception as exc:
        log.debug("openbot ws session end id=%s: %s", sid, exc)
    finally:
        with _STATE_LOCK:
            _CONNECTED.pop(sid, None)
            # fail pending if no clients left
            if not _CONNECTED:
                for k, fut in list(_PENDING.items()):
                    if not fut.done():
                        fut.set_exception(ConnectionError("千牛桥接 WebSocket 已断开"))
                _PENDING.clear()
        log.info("openbot ws client disconnected id=%s peers=%s", sid, len(_CONNECTED))


async def _invoke_on_loop(expression: str, timeout: float = 12.0) -> Any:
    if not _CONNECTED:
        raise ConnectionError("千牛未连接 openbot 桥（ws://127.0.0.1:41010）。请确认已注入并打开接待台聊天页")
    # use any connected client
    with _STATE_LOCK:
        ws = next(iter(_CONNECTED.values()))
    req_id = uuid.uuid4().hex
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    with _STATE_LOCK:
        _PENDING[req_id] = fut
    try:
        await ws.send(json.dumps({"method": "execute", "expression": expression}, ensure_ascii=False))
        raw = await asyncio.wait_for(fut, timeout=timeout)
    except Exception:
        with _STATE_LOCK:
            _PENDING.pop(req_id, None)
        raise
    if raw is None or raw == "":
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return raw


def invoke_imsdk(api: str, param: Any = None, *, timeout: float = 12.0) -> Any:
    """imsdk.invoke(api, param) via connected 千牛 page (openbot protocol)."""
    if param is None:
        param = {}
    expr = f"imsdk.invoke({json.dumps(api)}, {json.dumps(param, ensure_ascii=False)})"
    return eval_expression(expr, timeout=timeout)


def eval_expression(expression: str, *, timeout: float = 12.0) -> Any:
    if not _STARTED or _LOOP is None:
        raise RuntimeError("openbot WebSocket 服务未启动")
    if not is_client_connected():
        raise ConnectionError("千牛页面未连上 Agent（请打开接待台聊天窗口，并确认已注入桥接脚本）")

    async def _run():
        return await _invoke_on_loop(expression, timeout=timeout)

    fut = asyncio.run_coroutine_threadsafe(_run(), _LOOP)
    return fut.result(timeout=timeout + 2.0)


def start_openbot_bridge(*, ws_port: int = DEFAULT_WS_PORT, http_port: int = DEFAULT_HTTP_PORT) -> None:
    """Start WS+HTTP servers in a daemon thread (idempotent)."""
    global _SERVER_THREAD, _STARTED, _LOOP
    with _STATE_LOCK:
        if _STARTED and _SERVER_THREAD and _SERVER_THREAD.is_alive():
            return
        _STARTED = True

    def _runner() -> None:
        global _LOOP
        try:
            import websockets
            from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        except Exception as exc:
            log.error("openbot bridge deps missing: %s", exc)
            return

        js_body = _assets_bridge_js().encode("utf-8")

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # quiet
                return

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path in ("/qn_imsdk_bridge.js", "/bridge.js", "/"):
                    data = js_body if path != "/" else b"qn openbot bridge ok\n"
                    ctype = "application/javascript; charset=utf-8" if path.endswith(".js") or path == "/qn_imsdk_bridge.js" else "text/plain; charset=utf-8"
                    if path == "/":
                        ctype = "text/plain; charset=utf-8"
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self.send_response(404)
                    self.end_headers()

        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", http_port), Handler)
            threading.Thread(target=httpd.serve_forever, name="openbot-http", daemon=True).start()
            log.info("openbot HTTP bridge js on http://127.0.0.1:%s/qn_imsdk_bridge.js", http_port)
        except OSError as exc:
            log.warning("openbot HTTP port %s busy: %s", http_port, exc)

        async def _main():
            global _LOOP
            _LOOP = asyncio.get_running_loop()
            async with websockets.serve(
                _handle_ws,
                "127.0.0.1",
                ws_port,
                max_size=8 * 1024 * 1024,
                ping_interval=20,
                ping_timeout=20,
            ):
                log.info("openbot WS listening ws://127.0.0.1:%s", ws_port)
                await asyncio.Future()  # run forever

        try:
            asyncio.run(_main())
        except OSError as exc:
            log.error("openbot WS port %s failed: %s", ws_port, exc)
        except Exception as exc:
            log.exception("openbot bridge crashed: %s", exc)

    _SERVER_THREAD = threading.Thread(target=_runner, name="openbot-ws-server", daemon=True)
    _SERVER_THREAD.start()
    # brief wait for bind
    time.sleep(0.25)


def stop_openbot_bridge() -> None:
    # daemon threads die with process; no hard stop needed for EXE lifecycle
    pass
