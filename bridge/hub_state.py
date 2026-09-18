# -*- coding: utf-8 -*-
"""Bridge agent presence plus durable command coordination."""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import deque
from collections.abc import Mapping
from typing import Any, Deque, Dict, List, Optional


class BridgeHub:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.command_ready = threading.Condition(self.lock)
        self.agents: Dict[str, dict] = {}
        self.commands: Dict[str, dict] = {}
        self.command_queue: Dict[str, Deque[str]] = {}
        self.events: Deque[dict] = deque(maxlen=5000)
        self.tokens: set[str] = set()
        self.token_principals: Dict[str, str] = {}
        self._legacy_tokens: set[str] = set()
        self._legacy_token_principals: Dict[str, str] = {}
        self._legacy_token_channels: Dict[str, Dict[str, str]] = {}
        self.command_store: Any = None
        self._last_command_cleanup_at = 0.0
        self._command_cleanup_lock = threading.Lock()

    def configure_command_store(self, store: Any) -> None:
        required = (
            "enqueue_bridge_command",
            "claim_bridge_commands",
            "complete_bridge_command",
        )
        selected = store if store is not None and all(hasattr(store, name) for name in required) else None
        with self.lock:
            self.command_store = selected

    def maybe_cleanup_commands(
        self,
        *,
        retention_days: int = 30,
        interval_seconds: float = 3600.0,
    ) -> int:
        """Delete old terminal commands at most once per process interval."""
        now = time.monotonic()
        if now - self._last_command_cleanup_at < max(60.0, interval_seconds):
            return 0
        if not self._command_cleanup_lock.acquire(blocking=False):
            return 0
        try:
            now = time.monotonic()
            if now - self._last_command_cleanup_at < max(60.0, interval_seconds):
                return 0
            with self.lock:
                store = self.command_store
            cleanup = getattr(store, "cleanup_bridge_commands", None)
            if not callable(cleanup):
                self._last_command_cleanup_at = now
                return 0
            # Throttle failures too; command ACK traffic must not turn a
            # database outage into a cleanup retry storm.
            self._last_command_cleanup_at = now
            removed = int(cleanup(retention_days=retention_days) or 0)
            return max(0, removed)
        finally:
            self._command_cleanup_lock.release()

    def configure_tokens(self, tokens: Any) -> None:
        """Configure accepted agent tokens and their server-side principals.

        Preferred entries are ``{"token": "...", "agent_id": "..."}`` or a
        top-level ``{token: agent_id}`` mapping. Plain string tokens remain
        supported for existing installations: their first authenticated
        registration binds the token to one agent id for the lifetime of this
        hub process.
        """
        configured_tokens: set[str] = set()
        explicit_principals: Dict[str, str] = {}
        legacy_tokens: set[str] = set()

        if isinstance(tokens, Mapping):
            entries = [
                {"token": token, "agent_id": agent_id}
                for token, agent_id in tokens.items()
            ]
        elif isinstance(tokens, (list, tuple, set, frozenset)):
            entries = list(tokens)
        else:
            entries = []

        for raw in entries:
            if isinstance(raw, Mapping):
                token = str(raw.get("token") or raw.get("agent_token") or "").strip()
                agent_id = str(raw.get("agent_id") or raw.get("principal") or "").strip()
                if not token or not agent_id:
                    continue
                configured_tokens.add(token)
                explicit_principals[token] = agent_id
                continue
            token = str(raw or "").strip()
            if token:
                configured_tokens.add(token)
                legacy_tokens.add(token)

        with self.lock:
            learned_principals = {
                token: agent_id
                for token, agent_id in self._legacy_token_principals.items()
                if token in legacy_tokens
            }
            learned_channels = {
                token: dict(channels)
                for token, channels in self._legacy_token_channels.items()
                if token in legacy_tokens
            }
            self.tokens = configured_tokens
            self._legacy_tokens = legacy_tokens
            self._legacy_token_principals = learned_principals
            self._legacy_token_channels = learned_channels
            self.token_principals = {**learned_principals, **explicit_principals}

    def auth(self, token: str) -> bool:
        token = str(token or "").strip()
        if not token:
            return False
        with self.lock:
            if not self.tokens:
                # Fail closed if no tokens configured.
                return False
            return token in self.tokens

    @staticmethod
    def _legacy_principal(token: str) -> str:
        """Derive a stable, non-secret agent id from a unique legacy token."""
        digest = hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()[:16]
        return f"agent-{digest}"

    @staticmethod
    def _normalize_platform(platform: str) -> str:
        value = str(platform or "").strip().lower()
        if value in {"qianniu", "tb"}:
            return "taobao"
        return value if value in {"pdd", "taobao"} else ""

    @classmethod
    def _legacy_channel_principal(cls, token: str, platform: str) -> str:
        platform = cls._normalize_platform(platform)
        if not platform:
            return cls._legacy_principal(token)
        digest = hashlib.sha256(
            f"{str(token or '')}\x00{platform}".encode("utf-8")
        ).hexdigest()[:16]
        return f"{platform}-{digest}"

    @classmethod
    def _legacy_principal_platform(cls, token: str, agent_id: str) -> str:
        claimed = str(agent_id or "").strip()
        for platform in ("pdd", "taobao"):
            if claimed == cls._legacy_channel_principal(token, platform):
                return platform
        return ""

    def resolve_agent_id(
        self,
        token: str,
        claimed_agent_id: str = "",
        *,
        bind_legacy: bool = False,
        platform_hint: str = "",
    ) -> str:
        """Resolve a token to its trusted agent identity.

        ``claimed_agent_id`` is only a compatibility assertion. It never
        overrides an existing principal. Legacy string tokens may bind once,
        during registration, by passing ``bind_legacy=True``.
        """
        token = str(token or "").strip()
        claimed_agent_id = str(claimed_agent_id or "").strip()
        platform_hint = self._normalize_platform(platform_hint)
        with self.lock:
            if not token or token not in self.tokens:
                raise PermissionError("invalid agent token")

            if token not in self._legacy_tokens:
                principal = self.token_principals.get(token)
                if not principal:
                    raise PermissionError("agent token has no agent principal")
                if claimed_agent_id and claimed_agent_id != principal:
                    raise PermissionError("agent token belongs to another agent")
                return principal

            # A legacy token is an authentication credential, not a channel
            # identity. One Windows seat may legitimately run PDD and Taobao
            # bridges with that same credential. Keep the first channel on the
            # historical principal and assign a deterministic principal to any
            # additional platform so their heartbeat and command state cannot
            # overwrite or consume each other.
            principal = self._legacy_principal(token)
            claimed_platform = self._legacy_principal_platform(token, claimed_agent_id)
            platform = platform_hint or claimed_platform
            channels = self._legacy_token_channels.setdefault(token, {})

            if platform:
                bound = channels.get(platform)
                if bound:
                    return bound

                derived = self._legacy_channel_principal(token, platform)
                if claimed_agent_id == derived:
                    channels[platform] = derived
                    return derived

                base_owner = next(
                    (name for name, agent_id in channels.items() if agent_id == principal),
                    "",
                )
                if not base_owner:
                    channels[platform] = principal
                    self._legacy_token_principals[token] = principal
                    self.token_principals[token] = principal
                    return principal

                channels[platform] = derived
                return derived

            # Unlabelled old clients retain their historical behavior. Once a
            # platform-specific principal has been persisted by registration,
            # its ID alone is enough to resolve all later requests.
            self._legacy_token_principals[token] = principal
            self.token_principals[token] = principal
            return principal

    def register(self, agent_id: str, agent_name: str, version: str = "") -> dict:
        agent_id = str(agent_id or "").strip() or f"agent-{secrets.token_hex(6)}"
        with self.lock:
            row = self.agents.get(agent_id) or {}
            row.update(
                {
                    "agent_id": agent_id,
                    "agent_name": agent_name or row.get("agent_name") or agent_id,
                    "version": version or row.get("version") or "",
                    "registered_at": row.get("registered_at") or time.time(),
                    "last_seen": time.time(),
                    "online": True,
                    "status": row.get("status") or {},
                }
            )
            self.agents[agent_id] = row
            self.command_queue.setdefault(agent_id, deque())
            return dict(row)

    def heartbeat(self, agent_id: str, agent_name: str, status: dict) -> dict:
        agent_id = str(agent_id or "").strip()
        if not agent_id:
            raise ValueError("agent_id required")
        with self.lock:
            row = self.agents.get(agent_id) or {
                "agent_id": agent_id,
                "registered_at": time.time(),
            }
            row["agent_name"] = agent_name or row.get("agent_name") or agent_id
            row["last_seen"] = time.time()
            row["online"] = True
            row["status"] = status if isinstance(status, dict) else {}
            self.agents[agent_id] = row
            self.command_queue.setdefault(agent_id, deque())
            return {
                "ok": True,
                "agent_id": agent_id,
                "queued_commands": len(self.command_queue.get(agent_id) or []),
            }

    def push_events(self, agent_id: str, events: List[dict]) -> dict:
        accepted = 0
        with self.lock:
            for raw in events or []:
                if not isinstance(raw, dict):
                    continue
                item = dict(raw)
                item["agent_id"] = agent_id
                item["received_at"] = time.time()
                self.events.append(item)
                accepted += 1
            if agent_id in self.agents:
                self.agents[agent_id]["last_seen"] = time.time()
        return {"ok": True, "accepted": accepted}

    def enqueue_send(
        self,
        *,
        agent_id: str,
        buyer_id: str,
        account: str,
        content: str,
        meta: Optional[dict] = None,
    ) -> dict:
        agent_id = str(agent_id or "").strip()
        buyer_id = str(buyer_id or "").strip()
        account = str(account or "").strip()
        content = str(content or "").strip()
        if not agent_id or not buyer_id or not account or not content:
            raise ValueError("agent_id, buyer_id, account, content required")
        command_id = f"cmd-{int(time.time() * 1000)}-{secrets.token_hex(4)}"
        cmd = {
            "id": command_id,
            "type": "send_text",
            "agent_id": agent_id,
            "buyer_id": buyer_id,
            "account": account,
            "content": content,
            "meta": meta or {},
            "created_at": time.time(),
            "status": "queued",
            "result": None,
        }
        return self._enqueue_command(cmd)

    def enqueue_open_chat(
        self,
        *,
        agent_id: str,
        buyer_id: str,
        account: str,
        meta: Optional[dict] = None,
    ) -> dict:
        """Ask seat agent to focus official client conversation (no send)."""
        agent_id = str(agent_id or "").strip()
        buyer_id = str(buyer_id or "").strip()
        account = str(account or "").strip()
        if not agent_id or not buyer_id or not account:
            raise ValueError("agent_id, buyer_id, account required")
        command_id = f"cmd-jump-{int(time.time() * 1000)}-{secrets.token_hex(4)}"
        cmd = {
            "id": command_id,
            "type": "open_chat",
            "agent_id": agent_id,
            "buyer_id": buyer_id,
            "account": account,
            "content": "",
            "meta": meta or {},
            "created_at": time.time(),
            "status": "queued",
            "result": None,
        }
        return self._enqueue_command(cmd)

    def _enqueue_command(self, cmd: dict) -> dict:
        agent_id = str(cmd.get("agent_id") or "").strip()
        with self.lock:
            if agent_id not in self.agents:
                self.agents[agent_id] = {
                    "agent_id": agent_id,
                    "agent_name": agent_id,
                    "registered_at": time.time(),
                    "last_seen": 0,
                    "online": False,
                    "status": {},
                }
            store = self.command_store
        persisted = store.enqueue_bridge_command(cmd) if store is not None else dict(cmd)
        with self.command_ready:
            self.commands[str(persisted.get("id") or cmd.get("id") or "")] = dict(persisted)
            if store is None:
                self.command_queue.setdefault(agent_id, deque()).append(str(cmd.get("id") or ""))
            self.command_ready.notify_all()
        return dict(persisted)

    def list_online_agents(self, *, max_age_seconds: float = 30.0) -> List[dict]:
        now = time.time()
        with self.lock:
            rows = []
            for row in self.agents.values():
                age = now - float(row.get("last_seen") or 0)
                if age <= max_age_seconds:
                    rows.append(dict(row))
            return rows

    def pull_commands(
        self,
        agent_id: str,
        *,
        limit: int = 10,
        wait_seconds: float = 0.0,
        lease_seconds: int = 120,
    ) -> List[dict]:
        agent_id = str(agent_id or "").strip()
        out: List[dict] = []
        wait_seconds = max(0.0, min(float(wait_seconds or 0.0), 25.0))
        with self.command_ready:
            store = self.command_store
            if store is not None:
                out = store.claim_bridge_commands(
                    agent_id,
                    limit=limit,
                    lease_seconds=lease_seconds,
                )
                if not out and wait_seconds > 0:
                    self.command_ready.wait(timeout=wait_seconds)
                    out = store.claim_bridge_commands(
                        agent_id,
                        limit=limit,
                        lease_seconds=lease_seconds,
                    )
                for cmd in out:
                    self.commands[str(cmd.get("id") or "")] = dict(cmd)
                if agent_id in self.agents:
                    self.agents[agent_id]["last_seen"] = time.time()
                    self.agents[agent_id]["online"] = True
                return [dict(cmd) for cmd in out]
            q = self.command_queue.setdefault(agent_id, deque())
            if not q and wait_seconds > 0:
                self.command_ready.wait(timeout=wait_seconds)
            while q and len(out) < limit:
                command_id = q.popleft()
                cmd = self.commands.get(command_id)
                if not cmd:
                    continue
                if cmd.get("status") not in {"queued", "delivered"}:
                    continue
                cmd["status"] = "delivered"
                cmd["delivered_at"] = time.time()
                out.append(
                    {
                        "id": cmd["id"],
                        "type": cmd.get("type") or "send_text",
                        "buyer_id": cmd["buyer_id"],
                        "account": cmd["account"],
                        "content": cmd.get("content") or "",
                        "meta": cmd.get("meta") or {},
                    }
                )
            if agent_id in self.agents:
                self.agents[agent_id]["last_seen"] = time.time()
                self.agents[agent_id]["online"] = True
        return out

    def pull_commands_authenticated(
        self,
        token: str,
        *,
        claimed_agent_id: str = "",
        limit: int = 10,
        wait_seconds: float = 0.0,
        lease_seconds: int = 120,
    ) -> List[dict]:
        """Pull commands for the server-derived token principal only."""
        agent_id = self.resolve_agent_id(token, claimed_agent_id)
        return self.pull_commands(
            agent_id,
            limit=limit,
            wait_seconds=wait_seconds,
            lease_seconds=lease_seconds,
        )

    def complete_command(self, command_id: str, agent_id: str, result: dict) -> dict:
        command_id = str(command_id or "").strip()
        agent_id = str(agent_id or "").strip()
        with self.lock:
            store = self.command_store
        if store is not None:
            completed = store.complete_bridge_command(command_id, agent_id, result or {})
            if completed is None:
                raise KeyError("command not found")
            with self.lock:
                self.commands[command_id] = dict(completed)
            return dict(completed)
        with self.lock:
            cmd = self.commands.get(command_id)
            if not cmd:
                raise KeyError("command not found")
            if str(cmd.get("agent_id")) != agent_id:
                raise PermissionError("command belongs to another agent")
            ok = bool((result or {}).get("ok"))
            cmd["status"] = "succeeded" if ok else "failed"
            cmd["result"] = result if isinstance(result, dict) else {"raw": result}
            cmd["finished_at"] = time.time()
            return dict(cmd)

    def complete_command_authenticated(
        self,
        command_id: str,
        token: str,
        result: dict,
        *,
        claimed_agent_id: str = "",
    ) -> dict:
        """Complete a command only as the server-derived token principal."""
        agent_id = self.resolve_agent_id(token, claimed_agent_id)
        return self.complete_command(command_id, agent_id, result)

    def list_agents(self) -> List[dict]:
        now = time.time()
        with self.lock:
            rows = []
            for row in self.agents.values():
                item = dict(row)
                last = float(item.get("last_seen") or 0)
                item["online"] = bool(last and now - last < 30)
                rows.append(item)
            return sorted(rows, key=lambda r: str(r.get("agent_name") or r.get("agent_id")))

    def recent_events(self, limit: int = 50) -> List[dict]:
        with self.lock:
            items = list(self.events)[-max(1, min(500, int(limit))) :]
            return list(reversed(items))


HUB = BridgeHub()
