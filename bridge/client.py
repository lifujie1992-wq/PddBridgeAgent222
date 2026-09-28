# -*- coding: utf-8 -*-
"""HTTP client talking to the center brain /api/bridge/v1/*."""
from __future__ import annotations

import json
import time
from email.utils import parsedate_to_datetime
import urllib.error
import urllib.parse
import urllib.request
import queue
from typing import Any, Dict, List, Optional

from . import __version__


class BridgeClientError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, payload: Optional[dict] = None):
        super().__init__(message)
        self.status = status
        self.payload = payload or {}


class BridgeClient:
    def __init__(
        self,
        server_url: str,
        agent_token: str,
        agent_id: str,
        agent_name: str = "",
        device_id: str = "",
        *,
        websocket_enabled: bool = False,
        websocket_url: str = "",
        websocket_path: str = "/api/bridge/v1/ws",
        websocket_max_inflight: int = 64,
    ):
        self.server_url = str(server_url or "").rstrip("/")
        self.agent_token = str(agent_token or "")
        self.agent_id = str(agent_id or "")
        self.agent_name = str(agent_name or "")
        self.device_id = str(device_id or "")
        # 中心 /events 实测要 9–15s；旧值 15s 正好卡在超时线上，会反复超时重发。
        self.timeout = 30.0
        self._server_clock = None
        self._websocket = None
        self._ws_commands = queue.Queue(maxsize=2000)
        if websocket_enabled:
            from .ws_transport import WebSocketEventTransport
            ws_url = str(websocket_url or self.server_url)
            if websocket_url:
                full_ws_url = ws_url.rstrip("/")
            else:
                full_ws_url = ws_url
            if full_ws_url.startswith("https://"):
                full_ws_url = "wss://" + full_ws_url[8:]
            elif full_ws_url.startswith("http://"):
                full_ws_url = "ws://" + full_ws_url[7:]
            if websocket_url:
                target = full_ws_url
            else:
                target = full_ws_url.rstrip("/") + "/" + str(websocket_path).lstrip("/")
            self._websocket = WebSocketEventTransport(
                target,
                self.agent_token, self.agent_id, self.device_id,
                timeout=self.timeout, max_inflight=websocket_max_inflight,
                on_command=self._queue_ws_command,
            )

    def _queue_ws_command(self, command: dict) -> None:
        try:
            self._ws_commands.put_nowait(dict(command))
        except queue.Full:
            log = __import__("logging").getLogger("pdd.bridge")
            log.error("websocket command queue full; command dropped id=%s",
                      command.get("id") or command.get("command_id"))

    def drain_ws_commands(self) -> List[dict]:
        commands = []
        while True:
            try:
                commands.append(self._ws_commands.get_nowait())
            except queue.Empty:
                return commands

    def server_now(self) -> float:
        if self._server_clock is None:
            return time.time()
        timestamp, observed = self._server_clock
        return timestamp + 1.0 + time.monotonic() - observed

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        *,
        timeout: Optional[float] = None,
    ) -> dict:
        if not self.server_url:
            raise BridgeClientError("server_url is empty")
        if not self.agent_token:
            raise BridgeClientError("agent_token is empty")
        url = f"{self.server_url}{path}"
        data = None
        headers = {
            "X-Agent-Token": self.agent_token,
            "X-Agent-Id": self.agent_id,
            "Accept": "application/json",
        }
        if self.device_id:
            headers["X-Device-Id"] = self.device_id
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=float(timeout or self.timeout)) as resp:
                observed = time.monotonic()
                raw = resp.read().decode("utf-8", "replace")
                try:
                    server_time = parsedate_to_datetime(getattr(resp, "headers", {}).get("Date", "")).timestamp()
                    self._server_clock = (server_time, observed)
                except (ValueError, TypeError, OverflowError):
                    pass
                if not raw.strip():
                    return {"ok": True}
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    return {"ok": True, "data": payload}
                return payload
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                payload = json.loads(raw) if raw else {}
            except Exception:
                payload = {"error": raw or str(exc)}
            if not isinstance(payload, dict):
                payload = {"error": str(payload)}
            raise BridgeClientError(
                str(payload.get("error") or payload.get("error_user") or exc.reason or str(exc)),
                status=int(exc.code),
                payload=payload,
            ) from exc
        except Exception as exc:
            raise BridgeClientError(str(exc)) from exc

    def register(self) -> dict:
        return self._request(
            "POST",
            "/api/bridge/v1/register",
            {
                "agent_id": self.agent_id,
                "agent_name": self.agent_name,
                "version": __version__,
            },
        )

    def heartbeat(self, status: dict) -> dict:
        return self._request(
            "POST",
            "/api/bridge/v1/heartbeat",
            {
                "agent_id": self.agent_id,
                "agent_name": self.agent_name,
                "status": status,
            },
        )

    def upload_events(self, events: List[dict]) -> dict:
        if self._websocket is not None:
            try:
                return self._websocket.send_events(events)
            except BridgeClientError as exc:
                # Keep the existing HTTP endpoint as a live fallback while the
                # center rolls out the WebSocket endpoint.
                log = __import__("logging").getLogger("pdd.bridge")
                log.warning("websocket upload failed, falling back to HTTP: %s", exc)
        return self._request(
            "POST",
            "/api/bridge/v1/events",
            {
                "agent_id": self.agent_id,
                "events": events,
            },
        )

    def connect_websocket(self) -> None:
        if self._websocket is not None:
            self._websocket.connect()

    def transport_status(self) -> dict:
        if self._websocket is None:
            return {"enabled": False, "connected": False, "transport": "http"}
        return self._websocket.status()

    def close(self) -> None:
        if self._websocket is not None:
            self._websocket.close()

    def pull_commands(self, *, wait_seconds: float = 0.0) -> List[dict]:
        wait_seconds = max(0.0, min(float(wait_seconds or 0.0), 25.0))
        q = urllib.parse.urlencode({
            "agent_id": self.agent_id,
            "wait_seconds": wait_seconds,
        })
        payload = self._request(
            "GET",
            f"/api/bridge/v1/commands?{q}",
            timeout=max(self.timeout, wait_seconds + 5.0),
        )
        commands = payload.get("commands") if isinstance(payload, dict) else None
        if not isinstance(commands, list):
            return []
        return [c for c in commands if isinstance(c, dict)]

    def report_command_result(self, command_id: str, result: dict) -> dict:
        return self._request(
            "POST",
            f"/api/bridge/v1/commands/{urllib.parse.quote(str(command_id))}/result",
            {
                "agent_id": self.agent_id,
                "result": result,
            },
        )
