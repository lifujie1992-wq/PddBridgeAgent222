# -*- coding: utf-8 -*-
"""HTTP client talking to the center brain /api/bridge/v1/*."""
from __future__ import annotations

import json
import time
from email.utils import parsedate_to_datetime
import urllib.error
import urllib.parse
import urllib.request
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
    ):
        self.server_url = str(server_url or "").rstrip("/")
        self.agent_token = str(agent_token or "")
        self.agent_id = str(agent_id or "")
        self.agent_name = str(agent_name or "")
        self.device_id = str(device_id or "")
        self.timeout = 15.0
        self._server_clock = None

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
        return self._request(
            "POST",
            "/api/bridge/v1/events",
            {
                "agent_id": self.agent_id,
                "events": events,
            },
        )

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
