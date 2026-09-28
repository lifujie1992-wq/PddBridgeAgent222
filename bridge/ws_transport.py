"""Persistent WebSocket event transport with per-event ACK tracking."""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Dict, List, Optional

from .client import BridgeClientError

log = logging.getLogger("pdd.bridge.ws")


class WebSocketEventTransport:
    def __init__(self, url: str, token: str, agent_id: str, device_id: str = "",
                 *, timeout: float = 30.0, max_inflight: int = 64,
                 on_command: Optional[Callable[[dict], None]] = None) -> None:
        self.url = str(url).rstrip("/")
        self.token = str(token or "")
        self.agent_id = str(agent_id or "")
        self.device_id = str(device_id or "")
        self.timeout = max(5.0, float(timeout))
        self.max_inflight = max(1, int(max_inflight))
        self._ws = None
        self._connect_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: Dict[str, dict] = {}
        self._reader = None
        self._closed = False
        self._on_command = on_command

    def _connect(self):
        try:
            import websocket  # type: ignore
        except ImportError as exc:
            raise BridgeClientError("websocket-client is not installed") from exc
        with self._connect_lock:
            if self._ws is not None:
                return self._ws
            try:
                self._ws = websocket.create_connection(
                    self.url,
                    timeout=self.timeout,
                    header=[
                        f"Authorization: Bearer {self.token}",
                        f"X-Agent-Token: {self.token}",
                        f"X-Agent-Id: {self.agent_id}",
                        f"X-Device-Id: {self.device_id}",
                    ],
                )
                self._closed = False
                self._reader = threading.Thread(
                    target=self._read_loop, name="bridge-ws-reader", daemon=True
                )
                self._reader.start()
                log.info("websocket connected url=%s", self.url)
                return self._ws
            except Exception as exc:
                self._ws = None
                raise BridgeClientError(f"websocket connect failed: {exc}") from exc

    def connect(self) -> None:
        """Establish the socket early so the center can observe agent readiness."""
        self._connect()

    def status(self) -> dict:
        """Return a small, serialization-safe transport snapshot for heartbeats/UI."""
        connected = self._ws is not None and not self._closed
        return {"enabled": True, "connected": connected,
                "transport": "websocket" if connected else "http"}

    def _read_loop(self) -> None:
        while not self._closed:
            ws = self._ws
            if ws is None:
                return
            try:
                raw = ws.recv()
                if not raw:
                    raise ConnectionError("websocket closed")
                message = json.loads(raw)
                if not isinstance(message, dict):
                    continue
                event_id = str(message.get("event_id") or "")
                if event_id:
                    with self._pending_lock:
                        item = self._pending.get(event_id)
                        if item is not None:
                            item["messages"].append(message)
                            status = str(message.get("status") or "")
                            if message.get("type") == "result" or status in {
                                "processed", "rejected", "ignored", "retry"
                            }:
                                item["done"] = True
                            item["condition"].notify_all()
                elif message.get("type") in {"command", "command_push"} or message.get("command_id"):
                    command = message.get("command")
                    if not isinstance(command, dict):
                        command = dict(message)
                    payload = message.get("payload")
                    if isinstance(payload, dict):
                        command = {**payload, **command}
                    command.setdefault("id", message.get("command_id"))
                    callback = self._on_command
                    if callback is not None:
                        try:
                            callback(command)
                        except Exception:
                            log.exception("websocket command callback failed")
                    command_id = str(command.get("id") or command.get("command_id") or "")
                    if command_id:
                        with self._send_lock:
                            ws.send(json.dumps({
                                "type": "command_ack",
                                "command_id": command_id,
                                "agent_id": self.agent_id,
                                "status": "received",
                            }, ensure_ascii=False))
                elif message.get("type") == "ping":
                    with self._send_lock:
                        ws.send(json.dumps({"type": "pong", "ts": time.time()}))
            except Exception as exc:
                log.warning("websocket reader stopped: %s", exc)
                self._disconnect(exc)
                return

    def _disconnect(self, error: Exception) -> None:
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        with self._pending_lock:
            for item in self._pending.values():
                item["error"] = error
                item["done"] = True
                item["condition"].notify_all()

    def send_events(self, events: List[dict]) -> dict:
        if not events:
            return {"ack_version": 1, "event_acks": []}
        ws = self._connect()
        entries = {}
        with self._pending_lock:
            for event in events:
                event_id = str(event.get("event_id") or event.get("idempotency_key") or "")
                if not event_id:
                    continue
                entries[event_id] = {
                    "messages": [], "done": False, "error": None,
                    "condition": threading.Condition(self._pending_lock),
                }
                self._pending[event_id] = entries[event_id]
        try:
            with self._send_lock:
                for event in events:
                    ws.send(json.dumps({
                        "type": "event",
                        "event_id": str(event.get("event_id") or event.get("idempotency_key") or ""),
                        "idempotency_key": str(event.get("idempotency_key") or event.get("event_id") or ""),
                        "agent_id": self.agent_id,
                        "payload": event,
                    }, ensure_ascii=False))
            deadline = time.monotonic() + self.timeout
            acknowledgements = []
            with self._pending_lock:
                while entries and time.monotonic() < deadline:
                    unfinished = [item for item in entries.values() if not item["done"]]
                    if not unfinished:
                        break
                    unfinished[0]["condition"].wait(timeout=max(0.05, deadline - time.monotonic()))
                for event_id, item in entries.items():
                    if item["error"] is not None:
                        raise BridgeClientError(str(item["error"]))
                    final = item["messages"][-1] if item["messages"] else {}
                    status = str(final.get("status") or "retry")
                    acknowledgements.append({
                        "event_id": event_id,
                        "status": "accepted" if status == "processed" else status,
                        "committed": status == "processed",
                        "retryable": status == "retry" or not item["done"],
                        "reason": final.get("error_code") or final.get("reason") or "",
                    })
            return {"ack_version": 1, "event_acks": acknowledgements}
        finally:
            with self._pending_lock:
                for event_id in entries:
                    self._pending.pop(event_id, None)

    def close(self) -> None:
        self._closed = True
        self._disconnect(ConnectionError("transport closed"))
