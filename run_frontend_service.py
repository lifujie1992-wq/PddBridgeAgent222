#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Static staff frontend with reverse-proxy to backend API/static.

Stability goals:
- Customer-service browsers only talk to the frontend port.
- /api/* and /static/* are proxied to the backend (no CORS pain).
- Survives backend restarts: HTML still loads; API returns 502 until backend is up.

Usage:
  python run_frontend_service.py
  python run_frontend_service.py --port 18767 --backend http://127.0.0.1:18765
"""
from __future__ import annotations

import argparse
import hmac
import json
import mimetypes
import os
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import parse_qs, quote, unquote, urlparse, urlsplit

BASE = Path(__file__).resolve().parent
WEB = BASE / "web"
DEFAULT_BACKEND = "http://47.107.138.228:18765"
DEFAULT_PORT = 18767
LOCAL_GATEWAY_VERSION = "0.6.2.3"
_SENSITIVE_PROXY_HEADERS = {
    "host",
    "content-length",
    "x-agent-token",
    "x-agent-id",
    "x-device-id",
    "x-bridge-token",
    "authorization",
    "cookie",
}
_MESSAGE_CONTEXT_FIELDS = (
    "order_context",
    "order_id",
    "order_info",
    "local_context_lookup",
    "goods_id",
    "goods_name",
    "goods_url",
    "goods_thumb_url",
    "goods_price",
    "goods_spec",
    "goods_context_source",
    "template_name",
    "raw_type",
    "message_type",
)


def _has_message_value(value) -> bool:
    return value not in (None, "", [], {})


def _message_context_score(message: dict) -> int:
    return sum(1 for key in _MESSAGE_CONTEXT_FIELDS if _has_message_value(message.get(key)))


def _merge_message_copies(current: dict, incoming: dict) -> tuple[dict, bool]:
    """Merge duplicate copies without losing richer local product/order context."""
    merged = dict(current)
    changed = False
    old_score = _message_context_score(current)
    new_score = _message_context_score(incoming)
    for key, value in incoming.items():
        if key in {"captured_at", "captured_at_ms", "enqueued_at"}:
            if current.get(key) and (not value or current[key] <= value):
                continue
        if key in _MESSAGE_CONTEXT_FIELDS:
            if key in {"order_info", "local_context_lookup"} and isinstance(value, dict):
                current_value = merged.get(key) if isinstance(merged.get(key), dict) else {}
                combined = {**current_value, **value}
                if combined != current_value:
                    merged[key] = combined
                    changed = True
                continue
            if _has_message_value(value) and not _has_message_value(merged.get(key)):
                merged[key] = value
                changed = True
            continue
        if key != "content" and _has_message_value(value) and merged.get(key) != value:
            merged[key] = value
            changed = True
    incoming_content = str(incoming.get("content") or "")
    current_content = str(current.get("content") or "")
    if incoming_content and (
        not current_content
        or new_score > old_score
        or (new_score == old_score and len(incoming_content) > len(current_content))
    ):
        if incoming_content != current_content:
            merged["content"] = incoming_content
            changed = True
    return merged, changed


def _usable_shop_name(value, shop_id: str = "") -> str:
    """Return a real display name, excluding ID-based UI fallbacks."""
    text = str(value or "").strip()
    sid = str(shop_id or "").strip()
    if not text or text == sid or text.startswith("mall_"):
        return ""
    numeric = sid[5:] if sid.startswith("mall_") else ""
    compact = "".join(text.split()).lower()
    if numeric and compact in {
        numeric,
        f"shop{numeric}",
        f"pddshop{numeric}",
        f"\u5e97\u94fa{numeric}",
        f"\u62fc\u591a\u591a\u5e97\u94fa{numeric}",
    }:
        return ""
    return text


def _preferred_shop_name(local_value, remote_value, shop_id: str = "") -> str:
    # The center is authoritative when it has a real configured display name.
    return (
        _usable_shop_name(remote_value, shop_id)
        or _usable_shop_name(local_value, shop_id)
        or ""
    )


def load_seat_identity(config_path: str = "") -> dict[str, str]:
    """Load the same identity used by the bridge process on this machine."""
    explicit = str(config_path or "").strip()
    runtime_root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else BASE
    candidates: list[Path] = [Path(explicit).resolve()] if explicit else []
    if not explicit:
        candidates.extend(
            [
                runtime_root / "bridge_config.json",
                runtime_root / "bridge_config.taobao.json",
            ]
        )
    cfg: dict = {}
    for path in candidates:
        try:
            candidate = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if not isinstance(candidate, dict):
            continue
        token = str(candidate.get("agent_token") or "").strip().lower()
        cfg = {**candidate, "config_path": str(path)}
        if token and not token.startswith(("change-me", "replace-me")) and token not in {"example", "changeme"}:
            break
    if not cfg:
        return {
            "agent_token": "", "agent_id": "", "device_id": "",
            "server_url": "", "platform": "pdd", "config_path": explicit,
        }
    token = str(cfg.get("agent_token") or "").strip()
    if token.lower().startswith(("change-me", "replace-me")) or token.lower() in {"example", "changeme"}:
        token = ""
    return {
        "agent_token": token,
        "agent_id": str(cfg.get("agent_id") or "").strip(),
        "device_id": str(cfg.get("device_id") or cfg.get("agent_id") or "").strip(),
        "server_url": str(cfg.get("server_url") or "").strip().rstrip("/"),
        "platform": str(cfg.get("platform") or "pdd").strip().lower(),
        "config_path": str(cfg.get("config_path") or ""),
    }


class SeatIdentityProvider:
    """Mtime-cached bridge identity bound to one exact config file."""

    def __init__(self, config_path: str | Path):
        self.config_path = Path(config_path).resolve()
        self.lock = threading.RLock()
        self.mtime_ns = -1
        self.size = -1
        self.identity: dict[str, str] = {
            "agent_token": "",
            "agent_id": "",
            "device_id": "",
            "server_url": "",
            "platform": "pdd",
            "config_path": str(self.config_path),
        }
        self.last_error = ""
        self.reload_count = 0
        self.snapshot(force=True)

    def snapshot(self, *, force: bool = False) -> dict[str, str]:
        try:
            stat = self.config_path.stat()
            signature = (int(stat.st_mtime_ns), int(stat.st_size))
        except OSError as exc:
            with self.lock:
                self.last_error = str(exc)[:300]
                return dict(self.identity)
        with self.lock:
            if not force and signature == (self.mtime_ns, self.size):
                return dict(self.identity)
        candidate = load_seat_identity(str(self.config_path))
        token = str(candidate.get("agent_token") or "").strip()
        agent_id = str(candidate.get("agent_id") or "").strip()
        if not token or not agent_id:
            with self.lock:
                self.last_error = "bridge config has no usable agent_token/agent_id"
                return dict(self.identity)
        candidate["device_id"] = str(candidate.get("device_id") or agent_id).strip()
        candidate["platform"] = str(candidate.get("platform") or "pdd").strip().lower()
        candidate["config_path"] = str(self.config_path)
        with self.lock:
            self.identity = {str(key): str(value or "") for key, value in candidate.items()}
            self.mtime_ns, self.size = signature
            self.last_error = ""
            self.reload_count += 1
            return dict(self.identity)

    def status(self) -> dict:
        with self.lock:
            return {
                "config_path": str(self.config_path),
                "config_mtime_ns": self.mtime_ns,
                "reload_count": self.reload_count,
                "last_error": self.last_error,
            }


def proxy_request_headers(
    headers,
    *,
    ui_role: str,
    agent_token: str,
    agent_id: str,
    device_id: str = "",
) -> dict[str, str]:
    """Strip caller-controlled credentials and inject the local seat principal."""
    seat = str(ui_role or "").strip().lower() == "seat"
    blocked = set(_SENSITIVE_PROXY_HEADERS)
    if seat:
        blocked.add("x-user-token")
    out = {str(k): str(v) for k, v in headers.items() if str(k).lower() not in blocked}
    if seat and agent_token:
        out["X-Agent-Token"] = agent_token
        if agent_id:
            out["X-Agent-Id"] = agent_id
        if device_id:
            out["X-Device-Id"] = device_id
    return out


def load_frontend_host() -> str:
    host = "127.0.0.1"
    try:
        cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
        host = str(cfg.get("frontend_host") or cfg.get("http_host") or host).strip() or host
    except Exception:
        pass
    return str(os.environ.get("KEFU_FRONTEND_HOST") or host).strip() or host


def load_ports() -> tuple[int, str]:
    fe_port = DEFAULT_PORT
    backend = DEFAULT_BACKEND
    try:
        cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
        fe_port = int(cfg.get("frontend_port") or DEFAULT_PORT)
        # Prefer explicit brain server URL (LAN split).
        for key in ("brain_server_url", "public_base_url", "backend_url"):
            val = str(cfg.get(key) or "").strip().rstrip("/")
            if val.startswith("http://") or val.startswith("https://"):
                backend = val
                break
        else:
            host = str(cfg.get("http_host") or "127.0.0.1")
            if host in {"0.0.0.0", "::"}:
                host = "127.0.0.1"
            be_port = int(cfg.get("http_port") or 18765)
            backend = f"http://{host}:{be_port}"
    except Exception:
        pass
    # Env override for seat machines without editing shared config.json
    env_backend = str(os.environ.get("KEFU_BRAIN_URL") or os.environ.get("BRAIN_SERVER_URL") or "").strip().rstrip("/")
    if env_backend.startswith("http://") or env_backend.startswith("https://"):
        backend = env_backend
    env_port = str(os.environ.get("KEFU_FRONTEND_PORT") or "").strip()
    if env_port.isdigit():
        fe_port = int(env_port)
    return fe_port, backend


def _shop_id_from_account(account: str, platform: str = "") -> str:
    account = str(account or "").strip()
    platform = str(platform or "").strip().lower()
    if account.startswith("cs_"):
        mall = account[3:].split(":", 1)[0].split("_", 1)[0]
        return f"mall_{mall}" if mall.isdigit() else ""
    if account.startswith(("mall_", "tb_")):
        return account
    if account and platform == "taobao":
        store = account.split(":", 1)[0].split("：", 1)[0].strip()
        return f"tb_nick_{store}" if store else ""
    return ""


def _event_timestamp(value) -> float:
    from bridge.message_timing import epoch_seconds
    return epoch_seconds(value)


def _is_pdd_system_event(value: dict) -> bool:
    if not isinstance(value, dict):
        return False
    event = value.get("event") if isinstance(value.get("event"), dict) else value
    template = str(event.get("template_name") or event.get("template") or "").strip().lower()
    if template == "mall_robot_man_intervention_and_restart":
        return True
    try:
        message_type = int(event.get("raw_type", event.get("message_type", event.get("type", -1))))
    except (TypeError, ValueError):
        message_type = -1
    content = " ".join(str(event.get("content") or "").split())
    if message_type == 31 and (
        (bool(event.get("no_unreply_hint")) and bool(event.get("conv_silent")))
        or ("还没有配置消费者问到的常见问题回答" in content and "立即配置" in content)
    ):
        return True
    return "机器人已暂停接待" in content and "立即恢复接待" in content


def _filter_replayed_callback_batches(messages: list[dict]) -> list[dict]:
    """Drop later callback batches repeated by PDD history snapshots."""
    groups: dict[int, list[int]] = {}
    for index, message in enumerate(messages):
        if str(message.get("role") or "") != "mall_cs":
            continue
        if not str(message.get("msg_id") or "").startswith("callback-"):
            continue
        try:
            timestamp = float(message.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if timestamp > 100_000_000_000:
            timestamp /= 1000.0
        groups.setdefault(int(timestamp), []).append(index)

    seen: set[tuple[str, ...]] = set()
    suppressed: set[int] = set()
    for timestamp in sorted(groups):
        indices = groups[timestamp]
        fingerprint = tuple(sorted({
            " ".join(str(messages[index].get("content") or "").split()).casefold()
            for index in indices
            if str(messages[index].get("content") or "").strip()
        }))
        # A single repeated short reply is valid. Snapshot replay is a repeated
        # multi-message batch, so require at least three distinct texts.
        if len(indices) < 3 or len(fingerprint) < 3:
            continue
        if fingerprint in seen:
            suppressed.update(indices)
        else:
            seen.add(fingerprint)
    if not suppressed:
        return messages
    return [message for index, message in enumerate(messages) if index not in suppressed]


class LocalSeatState:
    """Durable local-first conversation cache for a Windows customer-service seat."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.sessions: dict[str, dict] = {}
        self.active_shop_ids: set[str] = set()
        self.observed_shop_ids: set[str] = set()
        self.shop_names: dict[str, str] = {}
        self.shop_accounts: dict[str, str] = {}
        self.seat_accounts: dict[str, str] = {}
        self.shop_platforms: dict[str, str] = {}
        self.shop_ai_takeover: dict[str, bool] = {}
        self.auth_user: dict = {}
        self.remote_cache: dict[str, dict] = {}
        self.inflight: set[str] = set()
        self.backend_base = ""
        self.agent_token = ""
        self.agent_id = ""
        self.device_id = ""
        self.platform = "pdd"
        self.config_path = ""
        self.identity_provider: SeatIdentityProvider | None = None
        self.last_local_event_at = 0.0
        self.last_remote_sync_at = 0.0
        self.last_remote_error = ""
        self.event_version = 0
        self.event_condition = threading.Condition(self.lock)
        self._load()

    @staticmethod
    def _key(account: str, buyer_id: str) -> str:
        return f"{str(account or '').strip()}\x00{str(buyer_id or '').strip()}"

    @staticmethod
    def _clone(value):
        return json.loads(json.dumps(value, ensure_ascii=False))

    @staticmethod
    def _bootstrap_shop_ai_enabled(row: dict) -> bool:
        explicit = row.get("shop_ai_takeover_enabled")
        if isinstance(explicit, bool):
            return explicit
        return bool(
            str(row.get("status") or "").strip().lower() == "active"
            and str(row.get("brain_mode") or "").strip().lower() == "own"
            and row.get("brain_policy_configured") is True
            and str(row.get("send_policy") or "").strip().lower() == "shop"
        )

    @staticmethod
    def _apply_ai_takeover_state(row: dict, shop_policies: dict[str, bool]) -> dict:
        result = dict(row)
        shop_id = str(result.get("shop_id") or _shop_id_from_account(result.get("account") or "", self.platform))
        if shop_id in shop_policies:
            shop_enabled = bool(shop_policies[shop_id])
        else:
            shop_enabled = bool(result.get("shop_ai_takeover_enabled", False))
        result["shop_ai_takeover_enabled"] = shop_enabled
        if not shop_enabled:
            result["ai_takeover_enabled"] = False
            result["ai_takeover_state"] = "shadow"
            return result

        # 大脑下发的“转人工”优先于本地 AI 开关：一个按钮只应该有一个状态。
        # 否则会出现“AI接待中”和“需人工”同时挂在一行上的矛盾（现场就是这么冒出来的：
        # 历史手工重开 AI 留下的 ai_takeover_override=true 把 handoff 顶掉了）。
        if result.get("handoff") is True:
            result["ai_takeover_enabled"] = False
            result["ai_takeover_state"] = "handoff"
            return result

        override = result.get("ai_takeover_override")
        if isinstance(override, bool):
            result["ai_takeover_enabled"] = override
            result["ai_takeover_state"] = "active" if override else "paused"
            return result

        state = str(result.get("ai_takeover_state") or "").strip().lower()
        if state:
            result["ai_takeover_enabled"] = bool(
                result.get("ai_takeover_enabled", state == "active")
            )
            return result

        enabled = result.get("ai_takeover_enabled")
        if isinstance(enabled, bool):
            result["ai_takeover_state"] = "active" if enabled else "paused"
            return result

        result["ai_takeover_enabled"] = True
        result["ai_takeover_state"] = "active"
        return result

    def _learn_remote_shop_names_locked(self, payload: dict) -> bool:
        """Persist authoritative center names for local-only session rendering."""
        if not isinstance(payload, dict):
            return False
        candidates: list[tuple[str, str]] = []
        for collection_key in ("shops", "sessions"):
            for row in payload.get(collection_key) or []:
                if not isinstance(row, dict):
                    continue
                shop_id = str(
                    row.get("shop_id") or _shop_id_from_account(row.get("account") or "", self.platform)
                ).strip()
                candidates.append((shop_id, str(row.get("shop_name") or "")))
        payload_shop_id = str(
            payload.get("shop_id") or _shop_id_from_account(payload.get("account") or "", self.platform)
        ).strip()
        if payload_shop_id:
            candidates.append((payload_shop_id, str(payload.get("shop_name") or "")))

        name_maps = [payload.get("shop_name_map")]
        config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
        name_maps.append(config.get("shop_name_map"))
        for name_map in name_maps:
            if not isinstance(name_map, dict):
                continue
            for raw_key, raw_name in name_map.items():
                key = str(raw_key or "").strip()
                shop_id = key if key.startswith("mall_") else _shop_id_from_account(key, self.platform)
                candidates.append((shop_id, str(raw_name or "")))

        changed = False
        for shop_id, remote_name in candidates:
            if not shop_id:
                continue
            resolved = _preferred_shop_name(
                self.shop_names.get(shop_id),
                remote_name,
                shop_id,
            )
            if not resolved or self.shop_names.get(shop_id) == resolved:
                continue
            self.shop_names[shop_id] = resolved
            changed = True
            for session in self.sessions.values():
                if not isinstance(session, dict) or str(session.get("shop_id") or "") != shop_id:
                    continue
                if str(session.get("shop_name") or "") != resolved:
                    session["shop_name"] = resolved
        return changed

    @staticmethod
    def _message_sort_key(message: dict) -> tuple[float, float, int]:
        try:
            timestamp = float(message.get("ts") or 0)
        except (TypeError, ValueError):
            timestamp = 0.0
        if timestamp > 100_000_000_000:
            timestamp /= 1000.0
        try:
            precise_timestamp = float(message.get("platform_ts_key") or timestamp)
            if precise_timestamp > 100_000_000_000:
                precise_timestamp /= 1000.0
            elif precise_timestamp < 1_000_000_000:
                precise_timestamp = timestamp
        except (TypeError, ValueError):
            precise_timestamp = timestamp
        try:
            sequence = int(message.get("_sequence") or message.get("sequence") or 0)
        except (TypeError, ValueError):
            sequence = 0
        return precise_timestamp, timestamp, sequence

    def _repair_session_locked(self, session: dict) -> bool:
        original = session.get("messages") if isinstance(session.get("messages"), list) else []
        messages = [message for message in original if isinstance(message, dict)]
        messages = _filter_replayed_callback_batches(messages)
        messages.sort(key=self._message_sort_key)
        messages = messages[-500:]
        changed = messages != original
        if not messages:
            return changed
        last = messages[-1]
        summary = {
            "messages": messages,
            "msg_count": len(messages),
            "last_content": str(last.get("content") or ""),
            "last_role": str(last.get("role") or ""),
            "last_ts": self._message_sort_key(last)[0],
        }
        for key, value in summary.items():
            if session.get(key) != value:
                session[key] = value
                changed = True
        return changed

    def configure(
        self,
        *,
        backend: str,
        agent_token: str,
        agent_id: str,
        device_id: str = "",
        platform: str = "pdd",
        config_path: str = "",
        identity_provider: SeatIdentityProvider | None = None,
    ) -> None:
        self.backend_base = str(backend or "").rstrip("/")
        self.agent_token = str(agent_token or "").strip()
        self.agent_id = str(agent_id or "").strip()
        self.device_id = str(device_id or agent_id or "").strip()
        self.platform = str(platform or "pdd").strip().lower()
        self.config_path = str(config_path or "").strip()
        if identity_provider is not None:
            self.identity_provider = identity_provider

    def refresh_identity(self, *, force: bool = False) -> dict[str, str]:
        provider = self.identity_provider
        if provider is None:
            return {
                "agent_token": self.agent_token,
                "agent_id": self.agent_id,
                "device_id": self.device_id,
                "server_url": self.backend_base,
                "platform": self.platform,
                "config_path": self.config_path,
            }
        identity = provider.snapshot(force=force)
        self.configure(
            backend=identity.get("server_url") or self.backend_base,
            agent_token=identity.get("agent_token") or "",
            agent_id=identity.get("agent_id") or "",
            device_id=identity.get("device_id") or "",
            platform=identity.get("platform") or "pdd",
            config_path=identity.get("config_path") or "",
        )
        return identity

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, dict):
            return
        sessions = data.get("sessions")
        if isinstance(sessions, dict):
            self.sessions = {
                str(key): dict(value)
                for key, value in sessions.items() if isinstance(value, dict)
            }
        self.active_shop_ids = {
            str(value or "").strip()
            for value in (data.get("active_shop_ids") or [])
            if str(value or "").strip()
        }
        self.observed_shop_ids = {
            str(value or "").strip()
            for value in (data.get("observed_shop_ids") or [])
            if str(value or "").strip()
        }
        names = data.get("shop_names")
        if isinstance(names, dict):
            self.shop_names = {str(key): str(value) for key, value in names.items()}
        accounts = data.get("shop_accounts")
        if isinstance(accounts, dict):
            self.shop_accounts = {str(key): str(value) for key, value in accounts.items()}
        seat_accounts = data.get("seat_accounts")
        if isinstance(seat_accounts, dict):
            self.seat_accounts = {str(key): str(value) for key, value in seat_accounts.items()}
        platforms = data.get("shop_platforms")
        if isinstance(platforms, dict):
            self.shop_platforms = {str(key): str(value) for key, value in platforms.items()}
        shop_ai_takeover = data.get("shop_ai_takeover")
        if isinstance(shop_ai_takeover, dict):
            self.shop_ai_takeover = {
                str(key): bool(value) for key, value in shop_ai_takeover.items()
            }
        auth_user = data.get("auth_user")
        if isinstance(auth_user, dict):
            self.auth_user = dict(auth_user)
        # Upgrade existing state: infer one seat account only when the local
        # cache proves there is exactly one account for a shop.
        accounts_by_shop: dict[str, set[str]] = {}
        for session in self.sessions.values():
            if not isinstance(session, dict):
                continue
            account = str(session.get("account") or "").strip()
            shop_id = str(session.get("shop_id") or "").strip()
            if account and shop_id and not account.startswith("local_"):
                accounts_by_shop.setdefault(shop_id, set()).add(account)
        for shop_id, values in accounts_by_shop.items():
            if len(values) == 1:
                self.seat_accounts.setdefault(shop_id, next(iter(values)))
        changed = self._purge_system_messages_locked()
        for session in self.sessions.values():
            if isinstance(session, dict):
                changed = self._repair_session_locked(session) or changed
        if changed:
            self._save_locked()

    def _purge_system_messages_locked(self) -> bool:
        changed = False
        for key in list(self.sessions):
            session = self.sessions.get(key)
            if not isinstance(session, dict):
                continue
            messages = session.get("messages") if isinstance(session.get("messages"), list) else []
            kept = [message for message in messages if not _is_pdd_system_event(message)]
            if len(kept) == len(messages):
                continue
            changed = True
            if not kept:
                self.sessions.pop(key, None)
                continue
            session["messages"] = kept
            self._repair_session_locked(session)
        return changed

    def _save_locked(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "active_shop_ids": sorted(self.active_shop_ids),
                        "observed_shop_ids": sorted(self.observed_shop_ids),
                        "shop_names": self.shop_names,
                        "shop_accounts": self.shop_accounts,
                        "seat_accounts": self.seat_accounts,
                        "shop_platforms": self.shop_platforms,
                        "shop_ai_takeover": self.shop_ai_takeover,
                        "auth_user": self.auth_user,
                        "sessions": self.sessions,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            temporary.replace(self.path)
        except OSError:
            pass

    def dock_control(self) -> dict:
        defaults = {"pin": True, "adsorb": True}
        path = self.path.parent / "pdd_adsorb_control.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return {
            "ok": True,
            "pin": payload.get("pin") if isinstance(payload.get("pin"), bool) else defaults["pin"],
            "adsorb": payload.get("adsorb") if isinstance(payload.get("adsorb"), bool) else defaults["adsorb"],
        }

    def set_dock_control(self, payload: dict) -> dict:
        current = self.dock_control()
        for key in ("pin", "adsorb"):
            if key in payload:
                current[key] = bool(payload[key])
        path = self.path.parent / "pdd_adsorb_control.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"pin": current["pin"], "adsorb": current["adsorb"]}, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(path)
        return current

    def publish(self, raw: dict) -> dict:
        event = raw.get("event") if isinstance(raw.get("event"), dict) else raw
        if str(event.get("type") or "").strip().lower() == "shop_metadata":
            platform = str(event.get("platform") or "pdd").strip().lower() or "pdd"
            shop_id = str(event.get("shop_id") or "").strip()
            shop_name = str(event.get("shop_name") or "").strip()
            account = str(event.get("account") or "").strip()
            if platform != "pdd" or not shop_id.startswith("mall_") or not shop_name:
                raise ValueError("valid PDD shop_id and shop_name are required")
            with self.lock:
                changed = bool(
                    self.shop_names.get(shop_id) != shop_name
                    or (account and self.shop_accounts.get(shop_id) != account)
                    or self.shop_platforms.get(shop_id) != platform
                )
                self.shop_names[shop_id] = shop_name
                if account:
                    self.shop_accounts.setdefault(shop_id, account)
                self.shop_platforms[shop_id] = platform
                self.observed_shop_ids.add(shop_id)
                self._save_locked()
                if changed:
                    self.event_version += 1
                    self.event_condition.notify_all()
                version = self.event_version
            return {
                "accepted": changed,
                "committed": True,
                "visible": True,
                "shop_id": shop_id,
                "event_id": str(event.get("event_id") or f"shop-metadata-{shop_id}"),
                "version": version,
            }
        if _is_pdd_system_event(event) or bool(event.get("is_diagnostic")):
            return {
                "accepted": False,
                "committed": True,
                "visible": False,
                "filtered": True,
                "filter_reason": "pdd_system_message",
                "event_id": str(event.get("event_id") or event.get("idempotency_key") or event.get("msg_id") or ""),
                "version": self.event_version,
            }
        account = str(event.get("account") or "").strip()
        buyer_id = str(event.get("buyer_id") or "").strip()
        content = str(event.get("content") or "").strip()
        if not buyer_id or not content:
            raise ValueError("buyer_id and content are required")
        platform = str(event.get("platform") or "pdd").strip().lower() or "pdd"
        shop_id = str(event.get("shop_id") or _shop_id_from_account(account, platform)).strip()
        shop_name = str(
            event.get("shop_name") or event.get("mall_name") or event.get("mallName") or ""
        ).strip()
        with self.lock:
            if not shop_id:
                candidates = [
                    value for value in sorted(self.active_shop_ids | self.observed_shop_ids)
                    if self.shop_platforms.get(value, "").lower() == platform
                    or (platform == "pdd" and value.startswith("mall_"))
                    or (platform == "taobao" and value.startswith("tb_"))
                ]
                if len(candidates) == 1:
                    shop_id = candidates[0]
            if shop_id and (not account or (platform == "pdd" and not account.startswith("cs_"))):
                canonical = str(
                    self.seat_accounts.get(shop_id)
                    or self.shop_accounts.get(shop_id)
                    or ""
                ).strip()
                if canonical:
                    account = canonical
        if platform == "pdd" and shop_id.startswith("mall_") and not account.startswith("cs_"):
            account = str(self.seat_accounts.get(shop_id) or self.shop_accounts.get(shop_id) or "").strip()
        if not account:
            account = f"local_{platform}:{self.agent_id or 'seat'}"
        timestamp = _event_timestamp(event.get("ts"))
        msg_id = str(
            event.get("msg_id")
            or event.get("event_id")
            or event.get("idempotency_key")
            or f"local-{account}-{buyer_id}-{timestamp}-{content[:32]}"
        )
        role = str(event.get("role") or "user").strip() or "user"
        explicit_nickname = str(event.get("buyer_nick") or event.get("nickname") or "").strip()
        message = {
            "msg_id": msg_id,
            "buyer_id": buyer_id,
            "role": role,
            "content": content,
            "ts": timestamp,
            "platform_ts_key": str(event.get("platform_ts_key") or ""),
            "nickname": explicit_nickname or buyer_id,
            "account": account,
            "source": "local_bridge",
            "delivery_status": str(event.get("delivery_status") or ""),
            "parent_msg_id": str(event.get("parent_msg_id") or ""),
            "local_first": True,
            "bridge_source": str(event.get("source") or ""),
        }
        from bridge.message_timing import TIMING_FIELDS
        for field in (*_MESSAGE_CONTEXT_FIELDS, *TIMING_FIELDS):
            value = event.get(field)
            if _has_message_value(value):
                message[field] = self._clone(value)
        key = self._key(account, buyer_id)
        with self.lock:
            metadata_changed = False
            # A later, richer copy of the same platform message repairs an earlier
            # placeholder local_pdd session instead of leaving an invisible duplicate.
            if not account.startswith("local_"):
                for old_key in list(self.sessions):
                    if old_key == key:
                        continue
                    old_session = self.sessions.get(old_key)
                    if not isinstance(old_session, dict):
                        continue
                    old_account = str(old_session.get("account") or "")
                    if not old_account.startswith("local_") or str(old_session.get("buyer_id") or "") != buyer_id:
                        continue
                    old_messages = old_session.get("messages") if isinstance(old_session.get("messages"), list) else []
                    removed = [
                        item for item in old_messages
                        if isinstance(item, dict) and str(item.get("msg_id") or "") == msg_id
                    ]
                    if not removed:
                        continue
                    metadata_changed = True
                    remaining = [item for item in old_messages if item not in removed]
                    if not remaining:
                        self.sessions.pop(old_key, None)
                        continue
                    old_session["messages"] = remaining
                    old_session["msg_count"] = len(remaining)
                    old_session["unread"] = max(
                        0,
                        int(old_session.get("unread") or 0)
                        - sum(1 for item in removed if str(item.get("role") or "") == "user"),
                    )
                    self._repair_session_locked(old_session)
            session = self.sessions.get(key)
            if not isinstance(session, dict):
                session = {
                    "buyer_id": buyer_id,
                    "account": account,
                    "nickname": message["nickname"],
                    "shop_id": shop_id,
                    "shop_name": shop_name,
                    "platform": platform,
                    "unread": 0,
                    "messages": [],
                }
            previous_metadata = (
                str(session.get("nickname") or ""),
                str(session.get("shop_id") or ""),
                str(session.get("shop_name") or ""),
            )
            messages = session.get("messages") if isinstance(session.get("messages"), list) else []
            duplicate_index = next(
                (
                    index
                    for index, item in enumerate(messages)
                    if isinstance(item, dict) and str(item.get("msg_id") or "") == msg_id
                ),
                -1,
            )
            duplicate = duplicate_index >= 0
            if not duplicate:
                messages.append(message)
                session["messages"] = messages
                if role == "user":
                    session["unread"] = int(session.get("unread") or 0) + 1
            else:
                merged_message, message_changed = _merge_message_copies(
                    messages[duplicate_index],
                    message,
                )
                if message_changed:
                    messages[duplicate_index] = merged_message
                    session["messages"] = messages
                    metadata_changed = True
            session.update({
                "nickname": explicit_nickname or session.get("nickname") or buyer_id,
                "shop_id": shop_id,
                "shop_name": shop_name or session.get("shop_name") or "",
            })
            self._repair_session_locked(session)
            metadata_changed = metadata_changed or previous_metadata != (
                str(session.get("nickname") or ""),
                str(session.get("shop_id") or ""),
                str(session.get("shop_name") or ""),
            )
            self.sessions[key] = session
            self.last_local_event_at = time.time()
            if shop_id:
                self.observed_shop_ids.add(shop_id)
                if shop_name:
                    metadata_changed = metadata_changed or self.shop_names.get(shop_id) != shop_name
                    self.shop_names[shop_id] = shop_name
                if account and not account.startswith("local_"):
                    # The account on a local message is the account currently
                    # logged into this seat; remote shop metadata is aggregate.
                    if not self.seat_accounts.get(shop_id):
                        metadata_changed = True
                        self.seat_accounts[shop_id] = account
                    if not self.shop_accounts.get(shop_id):
                        self.shop_accounts[shop_id] = account
                metadata_changed = metadata_changed or self.shop_platforms.get(shop_id) != platform
                self.shop_platforms[shop_id] = platform
            self._save_locked()
            accepted = not duplicate or metadata_changed
            if accepted:
                self.event_version += 1
                self.event_condition.notify_all()
            visible = bool(shop_id and shop_id in (self.active_shop_ids | self.observed_shop_ids))
            version = self.event_version
        return {
            "accepted": accepted,
            "committed": True,
            "visible": visible,
            "shop_id": shop_id,
            "event_id": str(event.get("event_id") or event.get("idempotency_key") or msg_id),
            "version": version,
        }

    def wait_for_event(self, after: int, timeout: float = 20.0) -> int:
        with self.event_condition:
            if self.event_version <= after:
                self.event_condition.wait(timeout=max(0.1, timeout))
            return self.event_version

    def seat_status(self) -> dict:
        self.refresh_identity()
        with self.lock:
            messages = sum(len(row.get("messages") or []) for row in self.sessions.values())
            sessions = len(self.sessions)
            version = self.event_version
        return {
            "ok": True,
            "service": "pdd-local-seat-gateway",
            "gateway_version": LOCAL_GATEWAY_VERSION,
            "platform": self.platform,
            "agent_id": self.agent_id,
            "device_id": self.device_id,
            "agent_token_set": bool(self.agent_token),
            "config_path": self.config_path,
            "mode": "local_first",
            "ui_role": "seat",
            "capabilities_enabled": False,
            "pid": os.getpid(),
            "brain": self.backend_base,
            "rbac_required": False,
            "event_version": version,
            "store": {
                "messages": messages,
                "sessions": sessions,
                "last_local_at": self.last_local_event_at,
                "path": str(self.path),
            },
            "identity": self.identity_provider.status() if self.identity_provider else {},
        }

    def clear_unread(self, account: str, buyer_id: str) -> None:
        with self.lock:
            session = self.sessions.get(self._key(account, buyer_id))
            if isinstance(session, dict):
                session["unread"] = 0
                self._save_locked()

    def set_handoff(self, account: str, buyer_id: str, enabled: bool, reason: str = "") -> None:
        """Update one local session before asynchronously syncing the brain."""
        account = str(account or "").strip()
        buyer_id = str(buyer_id or "").strip()
        if not account or not buyer_id:
            return
        with self.lock:
            session = self.sessions.get(self._key(account, buyer_id))
            if not isinstance(session, dict):
                return
            session["handoff"] = bool(enabled)
            session["handoff_reason"] = str(reason or "") if enabled else ""
            self._save_locked()
            self.event_version += 1
            self.event_condition.notify_all()

    def set_ai_takeover(
        self,
        account: str,
        buyer_id: str,
        enabled: bool,
        reason: str = "",
        confirmed: dict | None = None,
    ) -> None:
        """Apply a per-conversation AI state only after the center confirms it."""
        account = str(account or "").strip()
        buyer_id = str(buyer_id or "").strip()
        if not account or not buyer_id:
            return
        confirmed = confirmed if isinstance(confirmed, dict) else {}
        with self.lock:
            session = self.sessions.get(self._key(account, buyer_id))
            if not isinstance(session, dict):
                return
            session["ai_takeover_override"] = bool(enabled)
            session["ai_takeover_enabled"] = bool(confirmed.get("ai_takeover_enabled", enabled))
            session["shop_ai_takeover_enabled"] = bool(
                confirmed.get("shop_ai_takeover_enabled", True)
            )
            session["ai_takeover_state"] = str(
                confirmed.get("ai_takeover_state") or ("active" if enabled else "paused")
            )
            session["ai_takeover_updated_at"] = str(confirmed.get("ai_takeover_updated_at") or "")
            session["ai_takeover_reason"] = str(confirmed.get("ai_takeover_reason") or reason or "")
            if "handoff" in confirmed:
                session["handoff"] = bool(confirmed.get("handoff"))
                session["handoff_reason"] = str(confirmed.get("handoff_reason") or "")
            elif enabled:
                session["handoff"] = False
                session["handoff_reason"] = ""
            self._save_locked()
            self.event_version += 1
            self.event_condition.notify_all()

    def _is_misidentified_pdd_staff_session_locked(self, session: dict) -> bool:
        account = str(session.get("account") or "")
        if str(session.get("platform") or "").lower() != "pdd" or account.startswith("cs_"):
            return False
        staff_ids = {
            str(value).rsplit(":", 1)[-1]
            for value in self.shop_accounts.values()
            if str(value).startswith("cs_") and ":" in str(value)
        }
        return str(session.get("buyer_id") or "") in staff_ids

    def _visible_sessions_locked(self) -> list[dict]:
        rows = []
        visible_shop_ids = self.active_shop_ids | self.observed_shop_ids
        for session in self.sessions.values():
            shop_id = str(session.get("shop_id") or "")
            if shop_id not in visible_shop_ids:
                continue
            # A seat is normally logged into one CS account per shop.  The
            # center reports all accounts under the shop, so keep the local
            # list scoped to the account discovered for this seat.
            seat_account = str(self.seat_accounts.get(shop_id) or "").strip()
            if not seat_account or str(session.get("account") or "").strip() != seat_account:
                continue
            if self._is_misidentified_pdd_staff_session_locked(session):
                continue
            row = {key: value for key, value in session.items() if key != "messages"}
            row["msg_count"] = len(session.get("messages") or [])
            shop_id = str(row.get("shop_id") or "")
            row["shop_name"] = _preferred_shop_name(
                row.get("shop_name"),
                self.shop_names.get(shop_id),
                shop_id,
            )
            rows.append(row)
        return rows

    def merge_sessions(self, path: str) -> dict:
        query = parse_qs(urlsplit(path).query)
        with self.lock:
            remote = self._clone(self.remote_cache.get(path) or {})
            names_changed = self._learn_remote_shop_names_locked(remote)
            if names_changed:
                self._save_locked()
            visible_local_rows = self._clone(self._visible_sessions_locked())
            local_rows = list(visible_local_rows)
            shop_policies = dict(self.shop_ai_takeover)
            active_shop_ids = sorted(self.active_shop_ids | self.observed_shop_ids)
            detail_summaries: dict[str, dict] = {}
            for cached_path, payload in self.remote_cache.items():
                if not cached_path.startswith("/api/session/") or not isinstance(payload, dict):
                    continue
                parsed_detail = urlsplit(cached_path)
                detail_buyer = unquote(parsed_detail.path.split("/api/session/", 1)[-1])
                detail_query = parse_qs(parsed_detail.query)
                detail_account = str((detail_query.get("account") or [""])[0]).strip()
                detail_messages = [
                    message
                    for message in (payload.get("messages") or [])
                    if isinstance(message, dict) and not _is_pdd_system_event(message)
                ]
                detail_messages = _filter_replayed_callback_batches(detail_messages)
                if not detail_account or not detail_buyer or not detail_messages:
                    continue
                latest = max(detail_messages, key=self._message_sort_key)
                summary = {
                    "last_content": str(latest.get("content") or ""),
                    "last_role": str(latest.get("role") or ""),
                    "last_ts": self._message_sort_key(latest)[0],
                    "msg_count": len(detail_messages),
                }
                # 会话状态（转人工 / AI 开关）也在明细里。中心的会话列表若未包含本工位
                # （归属/作用域问题），列表行就永远是旧值；这里从明细补齐。
                for state_key in (
                    "handoff", "handoff_reason", "ai_takeover_enabled", "ai_takeover_override",
                    "ai_takeover_state", "ai_takeover_updated_at", "ai_takeover_reason",
                ):
                    value = payload.get(state_key)
                    if value is not None:
                        summary[state_key] = value
                detail_summaries[self._key(detail_account, detail_buyer)] = summary
        search = str((query.get("search") or [""])[0]).strip().casefold()
        requested_shop = str((query.get("shop_id") or [""])[0]).strip()
        scope = str((query.get("scope") or ["active"])[0]).strip().lower()
        if requested_shop:
            local_rows = [row for row in local_rows if row.get("shop_id") == requested_shop]
        if search:
            local_rows = [
                row for row in local_rows
                if search in "\n".join(str(row.get(key) or "") for key in (
                    "buyer_id", "account", "nickname", "last_content",
                )).casefold()
            ]
        local_keys = {
            self._key(row.get("account"), row.get("buyer_id"))
            for row in local_rows
        }
        remote_rows = remote.get("sessions") if isinstance(remote.get("sessions"), list) else []
        remote_rows = [
            row for row in remote_rows
            if isinstance(row, dict)
            and self._key(row.get("account"), row.get("buyer_id")) in local_keys
            and not _is_pdd_system_event({
                **row,
                "content": row.get("last_content") or row.get("content") or "",
            })
        ]
        # 列表里没有、本地有的会话：补拉一次明细（异步、去重），下一轮就能从明细
        # 拿到转人工/AI 状态，而不是一直显示旧的“AI接待中”。
        remote_keys = {
            self._key(row.get("account"), row.get("buyer_id")) for row in remote_rows
        }
        for row in local_rows[:5]:
            if self._key(row.get("account"), row.get("buyer_id")) in remote_keys:
                continue
            account = str(row.get("account") or "").strip()
            buyer_id = str(row.get("buyer_id") or "").strip()
            if account and buyer_id:
                self.schedule_refresh(
                    f"/api/session/{quote(buyer_id, safe='')}?account={quote(account, safe='')}"
                )
        merged = {
            self._key(row.get("account"), row.get("buyer_id")): dict(row)
            for row in remote_rows
        }
        for row in local_rows:
            key = self._key(row.get("account"), row.get("buyer_id"))
            current = merged.get(key)
            if current is None:
                merged[key] = row
            elif float(row.get("last_ts") or 0) >= float(current.get("last_ts") or 0):
                merged_row = {**current, **row, "msg_count": max(
                    int(current.get("msg_count") or 0), int(row.get("msg_count") or 0)
                )}
                for state_key in (
                    "shop_ai_takeover_enabled", "ai_takeover_enabled", "ai_takeover_override",
                    "ai_takeover_state", "ai_takeover_updated_at", "ai_takeover_reason",
                    "ai_takeover_policy_reason",
                ):
                    if state_key in current and state_key not in row:
                        merged_row[state_key] = current[state_key]
                # A brain-issued handoff must not disappear merely because the
                # local message cache has a newer preview with handoff=false.
                if current.get("handoff") is True and row.get("handoff") is not True:
                    merged_row["handoff"] = True
                    merged_row["handoff_reason"] = str(current.get("handoff_reason") or "")
                shop_id = str(merged_row.get("shop_id") or "")
                merged_row["shop_name"] = _preferred_shop_name(
                    row.get("shop_name"),
                    current.get("shop_name"),
                    shop_id,
                )
                merged[key] = merged_row
        for key, summary in detail_summaries.items():
            current = merged.get(key)
            if current is None:
                continue
            if float(summary.get("last_ts") or 0) >= float(current.get("last_ts") or 0):
                merged[key] = {
                    **current,
                    **summary,
                    "msg_count": max(
                        int(current.get("msg_count") or 0),
                        int(summary.get("msg_count") or 0),
                    ),
                }
        rows = [
            self._apply_ai_takeover_state(row, shop_policies)
            for row in merged.values()
            if not self._is_misidentified_pdd_staff_session_locked(row)
        ]
        all_rows = list(rows)
        if scope == "unread":
            rows = [row for row in rows if int(row.get("unread") or 0) > 0]
        elif scope == "handoff":
            rows = [row for row in rows if bool(row.get("handoff"))]
        for row in rows:
            if row.get("last_content") is not None:
                row["display_last_content"] = row["last_content"]
        rows.sort(key=lambda row: float(row.get("last_ts") or 0), reverse=True)
        try:
            limit = max(1, min(int((query.get("limit") or [50])[0]), 100))
            page = max(1, int((query.get("page") or [1])[0]))
        except (TypeError, ValueError):
            limit, page = 50, 1
        total = len(rows)
        pages = max(1, (total + limit - 1) // limit)
        rows = rows[(page - 1) * limit:page * limit]
        counts = {
            "all": len(all_rows),
            "active": len(all_rows),
            "history": 0,
            "unread": sum(1 for row in all_rows if int(row.get("unread") or 0) > 0),
            "handoff": sum(1 for row in all_rows if bool(row.get("handoff"))),
            "handoff_unread": sum(
                int(row.get("unread") or 0) for row in all_rows if bool(row.get("handoff"))
            ),
        }
        shop_counts: dict[str, int] = {}
        for row in all_rows:
            shop_id = str(row.get("shop_id") or "")
            if shop_id:
                shop_counts[shop_id] = int(shop_counts.get(shop_id) or 0) + 1
        auth = dict(remote.get("auth") or {})
        auth.setdefault("role", "agent")
        auth.setdefault("role_label", "本机工位")
        auth["shop_ids"] = active_shop_ids
        return {
            **remote,
            "ok": True,
            "sessions": rows,
            "total": total,
            "page": page,
            "limit": limit,
            "pages": pages,
            "counts": counts,
            "shop_counts": shop_counts,
            "auth": auth,
            "local_first": True,
        }

    def merge_queue(self, path: str) -> dict:
        query = parse_qs(urlsplit(path).query)
        only_handoff = str((query.get("handoff") or ["1"])[0]).strip().lower() not in {
            "0", "false", "no",
        }
        try:
            limit = max(1, min(int((query.get("limit") or [50])[0]), 200))
        except (TypeError, ValueError):
            limit = 50
        with self.lock:
            remote = self._clone(self.remote_cache.get(path) or {})
            local_rows = self._clone(self._visible_sessions_locked())
            shop_policies = dict(self.shop_ai_takeover)
        remote_rows = remote.get("items") if isinstance(remote.get("items"), list) else []
        local_keys = {
            self._key(row.get("account"), row.get("buyer_id"))
            for row in local_rows
        }
        merged = {
            self._key(row.get("account"), row.get("buyer_id")): dict(row)
            for row in remote_rows
            if isinstance(row, dict)
            and self._key(row.get("account"), row.get("buyer_id")) in local_keys
        }
        for row in local_rows:
            key = self._key(row.get("account"), row.get("buyer_id"))
            current = merged.get(key) or {}
            local_item = {
                **current,
                **row,
                "last_text": row.get("last_content") or current.get("last_text") or "",
            }
            merged[key] = local_item
        rows = [
            self._apply_ai_takeover_state(row, shop_policies)
            for row in merged.values()
            if not only_handoff or bool(row.get("handoff"))
        ]
        rows.sort(key=lambda row: (0 if row.get("handoff") else 1, -float(row.get("last_ts") or 0)))
        rows = rows[:limit]
        return {
            **remote,
            "ok": True,
            "count": len(rows),
            "items": rows,
            "local_first": True,
        }

    def merge_detail(self, path: str) -> dict | None:
        parsed = urlsplit(path)
        buyer_id = unquote(parsed.path.split("/api/session/", 1)[-1])
        query = parse_qs(parsed.query)
        account = str((query.get("account") or [""])[0]).strip()
        with self.lock:
            remote = self._clone(self.remote_cache.get(path) or {})
            requested_shop = str(
                (remote.get("shop_id") if isinstance(remote, dict) else "")
                or _shop_id_from_account(account, self.platform)
                or ""
            ).strip()
            seat_account = str(self.seat_accounts.get(requested_shop) or "").strip()
            if not seat_account or account != seat_account:
                return None
            names_changed = self._learn_remote_shop_names_locked(remote)
            if names_changed:
                self._save_locked()
            local = self._clone(self.sessions.get(self._key(account, buyer_id)))
            active = self.active_shop_ids | self.observed_shop_ids
            shop_policies = dict(self.shop_ai_takeover)
        remote_messages = remote.get("messages") if isinstance(remote.get("messages"), list) else []
        remote_messages = [
            message for message in remote_messages
            if isinstance(message, dict) and not _is_pdd_system_event(message)
        ]
        remote_messages = _filter_replayed_callback_batches(remote_messages)
        if remote:
            remote["messages"] = remote_messages
            remote["msg_count"] = len(remote_messages)
        if local and str(local.get("shop_id") or "") not in active:
            local = None
        if local is None:
            return None
        local_messages = local.get("messages") if isinstance(local.get("messages"), list) else []
        local_messages = [
            message for message in local_messages
            if isinstance(message, dict) and not _is_pdd_system_event(message)
        ]
        messages = {}
        for index, message in enumerate([*local_messages, *remote_messages]):
            if not isinstance(message, dict):
                continue
            key = str(message.get("msg_id") or message.get("idempotency_key") or f"{message.get('ts')}-{index}")
            if key in messages:
                messages[key], _changed = _merge_message_copies(messages[key], message)
            else:
                messages[key] = dict(message)
        ordered = sorted(
            messages.values(),
            key=self._message_sort_key,
        )
        ordered = _filter_replayed_callback_batches(ordered)
        merged_detail = {
            **local,
            **remote,
            "ok": True,
            "messages": ordered,
            "msg_count": len(ordered),
            "message_total": max(int(remote.get("message_total") or 0), len(ordered)),
            "local_first": True,
        }
        shop_id = str(merged_detail.get("shop_id") or local.get("shop_id") or "")
        merged_detail["shop_name"] = _preferred_shop_name(
            local.get("shop_name"),
            remote.get("shop_name"),
            shop_id,
        )
        return self._apply_ai_takeover_state(merged_detail, shop_policies)

    def merged_status(self, path: str = "/api/status") -> dict:
        with self.lock:
            remote = self._clone(self.remote_cache.get(path) or {})
            active = sorted(self.active_shop_ids | self.observed_shop_ids)
            local_count = len(self._visible_sessions_locked())
            shop_name_map = dict(self.shop_names)
        remote_config = remote.get("config") if isinstance(remote.get("config"), dict) else {}
        configured_shop_names = remote_config.get("shop_name_map")
        if isinstance(configured_shop_names, dict):
            shop_name_map.update(configured_shop_names)
        remote_shop_names = remote.get("shop_name_map")
        if isinstance(remote_shop_names, dict):
            shop_name_map.update(remote_shop_names)
        return {
            "ok": True,
            "watching": True,
            "send_mode": "observe",
            "brain_mode": "tanyu_shadow",
            "shop_brains": [],
            **remote,
            "shop_name_map": shop_name_map,
            "session_count": local_count,
            "local_first": True,
            "local_session_count": local_count,
            "seat_active_shop_ids": active,
            "local_last_event_at": self.last_local_event_at,
            "local_last_remote_sync_at": self.last_remote_sync_at,
            "local_remote_error": self.last_remote_error,
            "gateway_version": LOCAL_GATEWAY_VERSION,
            "platform": self.platform,
            "agent_id": self.agent_id,
            "device_id": self.device_id,
        }

    def runtime_config(self) -> dict:
        return {
            "ok": True,
            "service": "pdd-local-seat-gateway",
            "api_base": "",
            "rbac_enabled": False,
            "roles": [],
            "expose_bridge_token_to_ui": False,
            "ui_role": "seat",
            "seat_mode": True,
            "seat_agent_id": self.agent_id,
            "seat_device_id": self.device_id,
            "platform": self.platform,
            "capabilities_enabled": False,
            "deploy_mode": "seat-local-first",
            "local_first": True,
            "gateway_version": LOCAL_GATEWAY_VERSION,
        }

    def auth_me(self) -> dict:
        with self.lock:
            user = self._clone(self.auth_user)
            shop_ids = sorted(self.active_shop_ids | self.observed_shop_ids)
        if not user:
            user = {
                "username": f"seat:{self.agent_id}",
                "display_name": self.agent_id or "本机客服工位",
                "role": "agent",
                "role_label": "本机工位",
                "enabled": True,
                "system": False,
                "perms": ["workbench.view", "workbench.reply", "workbench.export"],
            }
        user["shop_ids"] = shop_ids
        user["seat_agent_id"] = self.agent_id
        return {
            "ok": True,
            "authenticated": True,
            "rbac_enabled": False,
            "user": user,
            "roles": [],
            "local_first": True,
        }

    def _remote_json(self, path: str, *, timeout: float = 20.0) -> dict:
        for attempt in range(2):
            self.refresh_identity(force=attempt > 0)
            req = urlrequest.Request(
                self.backend_base + path,
                headers={
                    "Accept": "application/json",
                    "X-Agent-Token": self.agent_token,
                    "X-Agent-Id": self.agent_id,
                    "X-Device-Id": self.device_id,
                },
                method="GET",
            )
            try:
                with urlrequest.urlopen(req, timeout=timeout) as response:
                    payload = json.loads(response.read().decode("utf-8", "replace"))
                return payload if isinstance(payload, dict) else {}
            except urlerror.HTTPError as exc:
                if int(exc.code) != 403 or attempt:
                    raise
        return {}

    def post_remote_json(self, path: str, payload: dict, *, timeout: float = 20.0) -> dict:
        """Submit an operator action and wait for the brain to confirm it."""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        for attempt in range(2):
            self.refresh_identity(force=attempt > 0)
            if not self.backend_base or not self.agent_token:
                raise RuntimeError("center brain identity is not configured")
            req = urlrequest.Request(
                self.backend_base + path,
                data=body,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json; charset=utf-8",
                    "X-Agent-Token": self.agent_token,
                    "X-Agent-Id": self.agent_id,
                    "X-Device-Id": self.device_id,
                },
                method="POST",
            )
            try:
                with urlrequest.urlopen(req, timeout=timeout) as response:
                    result = json.loads(response.read().decode("utf-8", "replace") or "{}")
                return result if isinstance(result, dict) else {}
            except urlerror.HTTPError as exc:
                if int(exc.code) != 403 or attempt:
                    raise
        return {}

    def schedule_refresh(self, path: str) -> None:
        self.refresh_identity()
        if not self.backend_base or not self.agent_token:
            return
        key = f"GET {path}"
        with self.lock:
            if key in self.inflight:
                return
            self.inflight.add(key)

        def refresh() -> None:
            try:
                payload = self._remote_json(path)
                with self.lock:
                    previous = self.remote_cache.get(path)
                    changed = previous != payload
                    self.remote_cache[path] = payload
                    self.last_remote_sync_at = time.time()
                    self.last_remote_error = ""
                    names_changed = self._learn_remote_shop_names_locked(payload)
                    if path == "/api/seat/v1/bootstrap":
                        self.active_shop_ids = {
                            str(value or "").strip()
                            for value in (payload.get("active_shop_ids") or [])
                            if str(value or "").strip()
                        }
                        shop_ai_takeover: dict[str, bool] = {}
                        for row in (payload.get("shops") or []):
                            if not isinstance(row, dict):
                                continue
                            shop_id = str(row.get("shop_id") or "").strip()
                            if not shop_id:
                                continue
                            accounts = row.get("accounts") if isinstance(row.get("accounts"), list) else []
                            account = str(row.get("account") or (accounts[0] if accounts else "")).strip()
                            # A shop can have several CS accounts. Keep the
                            # account discovered locally for this seat.
                            if account and not self.shop_accounts.get(shop_id):
                                self.shop_accounts[shop_id] = account
                            platform = str(row.get("platform") or "").strip().lower()
                            if platform:
                                self.shop_platforms[shop_id] = platform
                            shop_ai_takeover[shop_id] = self._bootstrap_shop_ai_enabled(row)
                        self.shop_ai_takeover = shop_ai_takeover
                        if isinstance(payload.get("user"), dict):
                            self.auth_user = dict(payload["user"])
                        self._save_locked()
                    elif names_changed:
                        self._save_locked()
                    if names_changed or (changed and (
                        path == "/api/seat/v1/bootstrap"
                        or path.startswith("/api/sessions")
                        or path.startswith("/api/session/")
                    )):
                        self.event_version += 1
                        self.event_condition.notify_all()
            except Exception as exc:
                with self.lock:
                    self.last_remote_error = str(exc)[:300]
            finally:
                with self.lock:
                    self.inflight.discard(key)

        threading.Thread(target=refresh, name="seat-local-remote-refresh", daemon=True).start()

    def post_async(self, path: str, payload: dict) -> None:
        self.refresh_identity()
        if not self.backend_base or not self.agent_token:
            return

        def send() -> None:
            try:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                for attempt in range(2):
                    self.refresh_identity(force=attempt > 0)
                    req = urlrequest.Request(
                        self.backend_base + path,
                        data=body,
                        headers={
                            "Content-Type": "application/json; charset=utf-8",
                            "X-Agent-Token": self.agent_token,
                            "X-Agent-Id": self.agent_id,
                            "X-Device-Id": self.device_id,
                        },
                        method="POST",
                    )
                    try:
                        with urlrequest.urlopen(req, timeout=15) as response:
                            response.read()
                        break
                    except urlerror.HTTPError as exc:
                        if int(exc.code) != 403 or attempt:
                            raise
            except Exception as exc:
                with self.lock:
                    self.last_remote_error = str(exc)[:300]

        threading.Thread(target=send, name="seat-local-remote-post", daemon=True).start()


LOCAL_SEAT_STATE = LocalSeatState(
    ((Path(sys.executable).resolve().parent / "data") if getattr(sys, "frozen", False) else BASE / "runtime")
    / "seat_local_state.json"
)


class FrontHandler(BaseHTTPRequestHandler):
    backend_base = DEFAULT_BACKEND
    web_root = WEB
    ui_role = "brain"
    seat_agent_token = ""
    seat_agent_id = ""
    seat_device_id = ""
    identity_provider: SeatIdentityProvider | None = None
    local_state = LOCAL_SEAT_STATE

    def log_message(self, fmt: str, *args) -> None:
        stream = getattr(sys, "stderr", None)
        if stream is not None:
            stream.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, body: bytes, content_type: str = "text/plain; charset=utf-8",
              extra_headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _proxy(self) -> None:
        path = self.path.split("?", 1)[0]
        self._sync_identity()
        if self.ui_role == "seat" and path.startswith("/api/capabilities"):
            body = json.dumps({"ok": False, "error": "capability center is only available on the brain service"}, ensure_ascii=False).encode("utf-8")
            self._send(403, body, "application/json; charset=utf-8")
            return
        if (
            self.ui_role == "seat"
            and path.startswith("/api/")
            and not self.seat_agent_token
        ):
            body = json.dumps(
                {
                    "ok": False,
                    "error": "seat_identity_missing",
                    "error_user": "本机工位未绑定桥接身份，请检查 bridge_config.json 后重启本地服务",
                },
                ensure_ascii=False,
            ).encode("utf-8")
            self._send(503, body, "application/json; charset=utf-8")
            return
        length = int(self.headers.get("Content-Length") or 0)
        payload = self.rfile.read(length) if length > 0 else None
        for attempt in range(2):
            self._sync_identity(force=attempt > 0)
            target = self.backend_base.rstrip("/") + self.path
            headers = proxy_request_headers(
                self.headers,
                ui_role=self.ui_role if path.startswith("/api/") else "brain",
                agent_token=self.seat_agent_token,
                agent_id=self.seat_agent_id,
                device_id=self.seat_device_id,
            )
            req = urlrequest.Request(target, data=payload, headers=headers, method=self.command)
            try:
                with urlrequest.urlopen(req, timeout=120) as resp:
                    data = resp.read()
                    out_headers = {
                        "Content-Type": resp.headers.get("Content-Type") or "application/octet-stream",
                    }
                    for key in ("X-Request-Id", "Retry-After"):
                        if resp.headers.get(key):
                            out_headers[key] = resp.headers.get(key)
                    self._send(getattr(resp, "status", 200), data, out_headers["Content-Type"], out_headers)
                    return
            except urlerror.HTTPError as exc:
                if int(exc.code) == 403 and attempt == 0:
                    try:
                        exc.read()
                    except Exception:
                        pass
                    continue
                data = exc.read() if hasattr(exc, "read") else str(exc).encode("utf-8")
                ctype = exc.headers.get("Content-Type") if exc.headers else "application/json"
                self._send(int(exc.code), data or b"{}", ctype or "application/json")
                return
            except Exception as exc:
                body = json.dumps({
                    "ok": False,
                    "error": f"backend_unreachable: {exc}",
                    "backend": self.backend_base,
                    "hint": "请确认后端 run_backend_service / app.py 已启动",
                }, ensure_ascii=False).encode("utf-8")
                self._send(502, body, "application/json; charset=utf-8")
                return

    def _json_body(self) -> dict:
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 2_000_000)
            raw = self.rfile.read(length) if length > 0 else b"{}"
            payload = json.loads(raw.decode("utf-8", "replace"))
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _send_json(self, payload: dict, status: int = 200) -> None:
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _sync_identity(self, *, force: bool = False) -> dict[str, str]:
        provider = self.identity_provider
        if provider is None:
            return {
                "agent_token": self.seat_agent_token,
                "agent_id": self.seat_agent_id,
                "device_id": self.seat_device_id,
                "server_url": self.backend_base,
            }
        identity = provider.snapshot(force=force)
        self.seat_agent_token = str(identity.get("agent_token") or "")
        self.seat_agent_id = str(identity.get("agent_id") or "")
        self.seat_device_id = str(identity.get("device_id") or self.seat_agent_id)
        if identity.get("server_url"):
            self.backend_base = str(identity["server_url"]).rstrip("/")
        self.local_state.refresh_identity(force=force)
        return identity

    def _local_bridge_authorized(self) -> bool:
        supplied = str(self.headers.get("X-Agent-Token") or "")
        identity = self._sync_identity()
        expected = str(identity.get("agent_token") or "")
        if expected and supplied and hmac.compare_digest(expected, supplied):
            return True
        identity = self._sync_identity(force=True)
        expected = str(identity.get("agent_token") or "")
        return bool(expected and supplied and hmac.compare_digest(expected, supplied))

    def _local_first_get(self) -> bool:
        if self.ui_role != "seat":
            return False
        path = self.path.split("?", 1)[0]
        if path in {"/api/local-seat/v1/status", "/api/seat/status"}:
            query = parse_qs(urlsplit(self.path).query)
            self._sync_identity(force=str((query.get("reload") or [""])[0]) == "1")
            self._send_json(self.local_state.seat_status())
            return True
        if path == "/api/runtime-config":
            self._send_json(self.local_state.runtime_config())
            return True
        if path == "/api/dock/control":
            self._send_json(self.local_state.dock_control())
            return True
        if path == "/api/auth/me":
            self._send_json(self.local_state.auth_me())
            return True
        if path == "/api/seat/stream":
            self._seat_stream()
            return True
        if path == "/api/status":
            self._send_json(self.local_state.merged_status(self.path))
            return True
        if path == "/api/sessions":
            self.local_state.schedule_refresh(self.path)
            self._send_json(self.local_state.merge_sessions(self.path))
            return True
        if path == "/api/queue/handoff":
            self.local_state.schedule_refresh(self.path)
            self._send_json(self.local_state.merge_queue(self.path))
            return True
        # `/api/session/message-detail` is a separate white-box endpoint.  It
        # carries the real buyer/account in the query string, so treating the
        # literal path segment as a buyer id makes every request look non-local.
        # Only ordinary `/api/session/<buyer_id>` detail requests belong to the
        # local-session filter; functional endpoints must continue to proxy.
        if path.startswith("/api/session/") and path != "/api/session/message-detail":
            self.local_state.schedule_refresh(self.path)
            payload = self.local_state.merge_detail(self.path)
            if payload is None:
                self._send_json({"ok": False, "error": "session_not_local"}, status=404)
            else:
                self._send_json(payload)
            return True
        return False

    def _seat_stream(self) -> None:
        query = parse_qs(urlsplit(self.path).query)
        try:
            after = int((query.get("after") or [self.headers.get("Last-Event-ID") or 0])[0])
        except (TypeError, ValueError):
            after = 0
        version = self.local_state.wait_for_event(after, timeout=20.0)
        if version > after:
            payload = json.dumps({"version": version}, separators=(",", ":"))
            body = f"id: {version}\nevent: seat-message\ndata: {payload}\n\n".encode("utf-8")
        else:
            body = b": keepalive\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
        self.close_connection = True

    def _static_file(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in {"", "/"}:
            rel = "index.html"
        else:
            rel = path.lstrip("/").replace("\\", "/")
            if ".." in rel.split("/"):
                self._send(400, b"bad path")
                return
            # aliases
            if rel in {"adsorb", "adsorb/"}:
                rel = "adsorb.html"
        file_path = (self.web_root / rel).resolve()
        root = self.web_root.resolve()
        try:
            file_path.relative_to(root)
        except ValueError:
            self._send(400, b"bad path")
            return
        if not file_path.is_file():
            # Only SPA-fallback index for unknown app routes — not for missing known files
            if rel.endswith(".html") or rel.endswith(".js") or rel.endswith(".css"):
                self._send(404, f"missing file: {rel}".encode("utf-8"))
                return
            file_path = root / "index.html"
            if not file_path.is_file():
                self._send(404, b"frontend package missing: run tools/sync_web_frontend.py")
                return
        data = file_path.read_bytes()
        ctype = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript") or ctype == "application/json":
            ctype = ctype + "; charset=utf-8"
        self._send(200, data, ctype)

    def do_OPTIONS(self) -> None:  # noqa: N802
        if self.path.startswith("/api/") or self.path.startswith("/static/"):
            self._proxy()
        else:
            self._send(204, b"")

    def do_GET(self) -> None:  # noqa: N802
        if self._local_first_get():
            return
        if self.ui_role == "seat" and self.path.startswith("/static/"):
            self._static_file()
            return
        if self.path.startswith("/api/") or self.path.startswith("/static/"):
            self._proxy()
        else:
            self._static_file()

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if self.ui_role == "seat" and path in {
            "/api/local-seat/v1/events",
            "/api/bridge/v1/events",
        }:
            if not self._local_bridge_authorized():
                self._send_json({"ok": False, "error": "forbidden"}, 403)
                return
            payload = self._json_body()
            events = payload.get("events") if isinstance(payload.get("events"), list) else [payload]
            accepted = 0
            visible = 0
            errors = []
            event_acks = []
            for event in events[:100]:
                if not isinstance(event, dict):
                    continue
                try:
                    result = self.local_state.publish(event)
                    accepted += int(bool(result.get("accepted")))
                    visible += int(bool(result.get("visible")))
                    acknowledgement = {
                        "event_id": result.get("event_id"),
                        "committed": True,
                        "accepted": bool(result.get("accepted")),
                    }
                    if result.get("filtered"):
                        acknowledgement.update({
                            "filtered": True,
                            "filter_reason": str(result.get("filter_reason") or ""),
                        })
                    event_acks.append(acknowledgement)
                except ValueError as exc:
                    errors.append(str(exc))
            self._send_json({
                "ok": not errors,
                "accepted": accepted,
                "local_ingested": len(event_acks),
                "visible": visible,
                "errors": errors,
                "event_acks": event_acks,
                "mode": "local_first",
            }, 202 if not errors else 400)
            return
        if self.ui_role == "seat" and path == "/api/clear_unread":
            payload = self._json_body()
            self.local_state.clear_unread(payload.get("account"), payload.get("buyer_id"))
            self.local_state.post_async(self.path, payload)
            self._send_json({"ok": True, "local_first": True})
            return
        if self.ui_role == "seat" and path == "/api/dock/control":
            payload = self._json_body()
            supplied = [key for key in ("pin", "adsorb") if key in payload]
            if not supplied or any(not isinstance(payload.get(key), bool) for key in supplied):
                self._send_json({"ok": False, "error": "pin or adsorb must be boolean"}, 400)
                return
            try:
                self._send_json({**self.local_state.set_dock_control(payload), "local_first": True})
            except OSError as exc:
                self._send_json({"ok": False, "error": str(exc)}, 500)
            return
        if self.ui_role == "seat" and path == "/api/session/state":
            payload = self._json_body()
            account = str(payload.get("account") or "").strip()
            buyer_id = str(payload.get("buyer_id") or "").strip()
            if not account or not buyer_id:
                self._send_json({"ok": False, "error": "account and buyer_id required"}, 400)
                return
            reason = str(payload.get("reason") or "")
            is_ai_toggle = "ai_takeover_enabled" in payload
            if is_ai_toggle and not isinstance(payload.get("ai_takeover_enabled"), bool):
                self._send_json({"ok": False, "error": "ai_takeover_enabled must be boolean"}, 400)
                return
            remote_payload = {"account": account, "buyer_id": buyer_id, "reason": reason}
            if is_ai_toggle:
                remote_payload["ai_takeover_enabled"] = bool(payload["ai_takeover_enabled"])
            else:
                remote_payload["handoff"] = bool(payload.get("handoff"))
            try:
                remote_result = self.local_state.post_remote_json(self.path, remote_payload)
            except urlerror.HTTPError as exc:
                raw = exc.read() if hasattr(exc, "read") else b""
                try:
                    error_payload = json.loads(raw.decode("utf-8", "replace"))
                except Exception:
                    error_payload = {"ok": False, "error": str(exc)}
                self._send_json(error_payload if isinstance(error_payload, dict) else {
                    "ok": False, "error": str(exc),
                }, int(exc.code))
                return
            except Exception as exc:
                self._send_json({
                    "ok": False,
                    "error": "brain_unreachable",
                    "error_user": f"中心大脑未确认 AI 恢复：{exc}",
                }, 502)
                return
            if remote_result.get("ok") is False:
                self._send_json(remote_result, 409)
                return
            if is_ai_toggle:
                self.local_state.set_ai_takeover(
                    account,
                    buyer_id,
                    bool(payload["ai_takeover_enabled"]),
                    reason,
                    remote_result,
                )
            else:
                self.local_state.set_handoff(account, buyer_id, bool(payload.get("handoff")), reason)
            response_payload = {
                **remote_result,
                "ok": True,
                "local_first": True,
            }
            if not is_ai_toggle:
                response_payload["handoff"] = bool(payload.get("handoff"))
            self._send_json(response_payload)
            return
        if self.path.startswith("/api/"):
            self._proxy()
        else:
            self._send(404, b"not found")

    def do_PUT(self) -> None:  # noqa: N802
        self.do_POST()

    def do_PATCH(self) -> None:  # noqa: N802
        self.do_POST()

    def do_DELETE(self) -> None:  # noqa: N802
        self.do_POST()


def main() -> int:
    fe_default, be_default = load_ports()
    host_default = load_frontend_host()
    parser = argparse.ArgumentParser(description="Staff frontend (static + API proxy)")
    parser.add_argument("--host", default=host_default)
    parser.add_argument("--port", type=int, default=fe_default)
    parser.add_argument("--backend", default="", help="Backend origin, e.g. http://47.107.138.228:18765")
    parser.add_argument("--web-dir", default=str(WEB))
    parser.add_argument("--bridge-config", default="", help="Local bridge_config.json used to bind this seat")
    parser.add_argument("--ui-role", choices=("brain", "seat"), default="")
    args = parser.parse_args()

    runtime_root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else BASE
    exact_config_path = Path(args.bridge_config or runtime_root / "bridge_config.json").resolve()
    identity_provider = SeatIdentityProvider(exact_config_path)
    identity = identity_provider.snapshot()
    backend = str(args.backend or be_default).rstrip("/")
    if not args.backend and backend == DEFAULT_BACKEND and identity.get("server_url"):
        backend = identity["server_url"]

    web_root = Path(args.web_dir)
    if not web_root.is_dir():
        print(f"web dir missing: {web_root}", file=sys.stderr)
        print("Run: python tools/sync_web_frontend.py", file=sys.stderr)
        return 2

    # Ensure index exists
    if not (web_root / "index.html").is_file():
        print("web/index.html missing — syncing from templates...", flush=True)
        sync = BASE / "tools" / "sync_web_frontend.py"
        if sync.is_file():
            os.system(f'"{sys.executable}" "{sync}"')
        if not (web_root / "index.html").is_file():
            print("failed to build web/index.html", file=sys.stderr)
            return 2

    FrontHandler.backend_base = backend
    FrontHandler.web_root = web_root
    configured_role = ""
    try:
        cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
        configured_role = str(cfg.get("ui_role") or "").strip().lower()
    except Exception:
        configured_role = ""
    env_role = str(os.environ.get("KEFU_UI_ROLE") or "").strip().lower()
    try:
        backend_host = str(urlparse(backend).hostname or "").strip().lower()
    except Exception:
        backend_host = ""
    remote_backend = backend_host not in {"", "127.0.0.1", "localhost", "::1"}
    FrontHandler.ui_role = args.ui_role or env_role or (
        "seat" if identity.get("agent_token") or remote_backend else (configured_role or "brain")
    )
    FrontHandler.seat_agent_token = identity.get("agent_token") or ""
    FrontHandler.seat_agent_id = identity.get("agent_id") or ""
    FrontHandler.seat_device_id = identity.get("device_id") or FrontHandler.seat_agent_id
    FrontHandler.identity_provider = identity_provider
    FrontHandler.local_state.configure(
        backend=backend,
        agent_token=FrontHandler.seat_agent_token,
        agent_id=FrontHandler.seat_agent_id,
        device_id=FrontHandler.seat_device_id,
        platform=identity.get("platform") or "pdd",
        config_path=str(exact_config_path),
        identity_provider=identity_provider,
    )
    if FrontHandler.ui_role == "seat" and str(args.host).strip().lower() not in {
        "127.0.0.1", "localhost", "::1",
    }:
        print("local seat gateway must bind to loopback", file=sys.stderr)
        return 2
    if FrontHandler.ui_role == "seat":
        def sync_local_seat() -> None:
            paths = (
                "/api/seat/v1/bootstrap",
                "/api/sessions?paged=1&scope=active&page=1&limit=50",
            )
            while True:
                for path in paths:
                    FrontHandler.local_state.schedule_refresh(path)
                time.sleep(1.0)

        threading.Thread(target=sync_local_seat, name="seat-local-sync", daemon=True).start()

    def _ensure_jump_helper(runtime_root: Path) -> None:
        """幂等拉起本地秒跳助手(18769), 浮窗点击直连探域 DLL 毫秒跳。"""
        helper = runtime_root / "PddJumpHelper.exe"
        if not helper.is_file():
            return
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq PddJumpHelper.exe", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10,
            )
            if "pddjumphelper" in (out.stdout or "").lower():
                return
        except Exception:
            pass
        try:
            subprocess.Popen(
                [str(helper)], cwd=str(runtime_root),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass

    _ensure_jump_helper(runtime_root)

    server = ThreadingHTTPServer((args.host, int(args.port)), FrontHandler)
    role_line = f"role: {FrontHandler.ui_role}"
    if FrontHandler.ui_role == "seat":
        role_line += f" / agent: {FrontHandler.seat_agent_id} / device: {FrontHandler.seat_device_id}"
    print(
        f"frontend running\n"
        f"UI:  http://127.0.0.1:{args.port}/\n"
        f"API proxy -> {FrontHandler.backend_base}\n"
        f"{role_line}\n"
        f"web: {web_root}\n"
        f"pid: {os.getpid()}\n",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("frontend stopped", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
