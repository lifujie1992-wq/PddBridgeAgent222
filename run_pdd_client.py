# -*- coding: utf-8 -*-
"""PDD bridge entry with durable local-first delivery."""
from __future__ import annotations

import atexit
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlparse

import psutil


from bridge import __build_hash__ as BUILD_HASH
from bridge import __version__ as VERSION
from bridge.message_timing import TIMING_FIELDS, stamp_message, queue_metrics
from bridge.event_queue import append_event
log = logging.getLogger("pdd.local_first")
_GATEWAY_PROCESS: subprocess.Popen | None = None
_GATEWAY_PID: int | None = None
_GATEWAY_EXE: Path | None = None
_GATEWAY_CONFIG: Path | None = None
_GATEWAY_PORT: int | None = None
_GATEWAY_STOP_REGISTERED = False
_GATEWAY_STOP_LOCK = threading.Lock()
_ADSORB_PROCESS_PID: int | None = None
_ADSORB_STOP_LOCK = threading.Lock()


class LocalDeliveryQueue:
    """Persistent at-least-once queue from the bridge to the loopback gateway."""

    def __init__(
        self,
        *,
        endpoint: str,
        token: str,
        agent_id: str,
        device_id: str = "",
        config_path: str | Path | None = None,
        path: str | Path,
        stop_event: threading.Event,
        ledger=None,
    ) -> None:
        self.endpoint = str(endpoint or "http://127.0.0.1:18767").rstrip("/")
        self.token = str(token or "").strip()
        self.agent_id = str(agent_id or "").strip()
        self.device_id = str(device_id or "").strip()
        self.config_path = Path(config_path).resolve() if config_path else None
        self._config_mtime_ns = -1
        self.path = Path(path)
        self.stop_event = stop_event
        self.ledger = ledger          # 投递台账（可为 None）
        self.lock = threading.RLock()
        self.wakeup = threading.Event()
        self.pending: OrderedDict[str, dict] = OrderedDict()
        self.last_error = ""
        self.last_success_at = 0.0
        self._load()
        self.thread = threading.Thread(
            target=self._run,
            name="bridge-local-delivery",
            daemon=True,
        )
        self.thread.start()
        if self.pending:
            self.wakeup.set()

    @staticmethod
    def _event_id(event: dict) -> str:
        event_id = str(event.get("event_id") or event.get("idempotency_key") or "").strip()
        if event_id:
            return event_id
        identity = "\x00".join(str(event.get(key) or "") for key in (
            "platform", "account", "buyer_id", "msg_id", "role", "content", "ts",
        ))
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    event_id = self._event_id(event)
                    event["event_id"] = event_id
                    event["idempotency_key"] = event_id
                    self.pending[event_id] = event
        except OSError as exc:
            self.last_error = f"load: {exc}"

    def _rewrite_locked(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                for event in self.pending.values():
                    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            temporary.replace(self.path)
        except OSError as exc:
            self.last_error = f"persist: {exc}"

    def enqueue(self, event: dict, *, wake: bool = True) -> None:
        normalized = dict(event)
        event_id = self._event_id(normalized)
        normalized["event_id"] = event_id
        normalized["idempotency_key"] = event_id
        with self.lock:
            if event_id not in self.pending:
                append_event(self.path, normalized)
                self.pending[event_id] = normalized
        if wake:
            self.wakeup.set()

    def _refresh_identity(self, *, force: bool = False) -> bool:
        if self.config_path is None:
            return False
        try:
            mtime_ns = int(self.config_path.stat().st_mtime_ns)
        except OSError:
            return False
        if not force and mtime_ns == self._config_mtime_ns:
            return False
        raw = _load_raw_config(self.config_path)
        if not raw:
            return False
        token = str(raw.get("agent_token") or "").strip()
        agent_id = str(raw.get("agent_id") or "").strip()
        if not token or not agent_id:
            return False
        endpoint = str(
            raw.get("local_workbench_url")
            or raw.get("local_seat_url")
            or self.endpoint
        ).rstrip("/")
        with self.lock:
            self.token = token
            self.agent_id = agent_id
            self.device_id = str(raw.get("device_id") or agent_id).strip()
            self.endpoint = endpoint
            self._config_mtime_ns = mtime_ns
        return True

    def _request_gateway_reload(self) -> None:
        try:
            req = urlrequest.Request(
                self.endpoint + "/api/local-seat/v1/status?reload=1",
                headers={"Accept": "application/json"},
            )
            with urlrequest.urlopen(req, timeout=0.75) as response:
                response.read()
        except Exception:
            pass

    def _post_once(self, batch: list[dict]) -> set[str]:
        self._refresh_identity()
        # 批量大时给网关更多时间：突发时一次 POST 最多 100 条，网关要逐条落盘，
        # 0.75s 固定超时会把“还没写完的 ack”当失败，反而拖慢排空。
        request_timeout = max(0.75, min(10.0, 0.05 * len(batch)))
        body = json.dumps(
            {"agent_id": self.agent_id, "events": batch},
            ensure_ascii=False,
        ).encode("utf-8")
        req = urlrequest.Request(
            self.endpoint + "/api/bridge/v1/events",
            data=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "X-Agent-Token": self.token,
                "X-Agent-Id": self.agent_id,
                "X-Device-Id": self.device_id,
            },
            method="POST",
        )
        try:
            with urlrequest.urlopen(req, timeout=request_timeout) as response:
                payload = json.loads(response.read().decode("utf-8", "replace") or "{}")
        except urlerror.HTTPError as exc:
            if exc.code != 400:
                raise
            payload = json.loads(exc.read().decode("utf-8", "replace") or "{}")
        if not isinstance(payload, dict):
            raise RuntimeError("local gateway returned an invalid acknowledgement")
        acknowledgements = payload.get("event_acks")
        if isinstance(acknowledgements, list):
            return {
                str(row.get("event_id") or "")
                for row in acknowledgements
                if isinstance(row, dict) and row.get("committed") is True
            }
        if payload.get("ok") is True and int(payload.get("local_ingested") or 0) >= len(batch):
            return {self._event_id(event) for event in batch}
        raise RuntimeError("local gateway returned an incomplete acknowledgement")

    def _post(self, batch: list[dict]) -> set[str]:
        try:
            return self._post_once(batch)
        except urlerror.HTTPError as exc:
            if int(exc.code) != 403:
                raise
            self._refresh_identity(force=True)
            self._request_gateway_reload()
            return self._post_once(batch)

    def _run(self) -> None:
        retry_delay = 0.2
        while not self.stop_event.is_set():
            with self.lock:
                batch = list(self.pending.values())[:100]
            if not batch:
                self.wakeup.wait(1.0)
                self.wakeup.clear()
                continue
            try:
                committed = self._post(batch).intersection(self._event_id(e) for e in batch)
                if not committed:
                    raise RuntimeError("local gateway acknowledged no events")
                if self.ledger is not None:
                    for event_id in committed:
                        self.ledger.record(event_id, "local_ok", source="local_first")
                with self.lock:
                    for event_id in committed:
                        self.pending.pop(event_id, None)
                    self._rewrite_locked()
                    self.last_error = ""
                    self.last_success_at = time.time()
                retry_delay = 0.2
            except (OSError, ValueError, RuntimeError, urlerror.URLError) as exc:
                with self.lock:
                    self.last_error = str(exc)[:300]
                self.stop_event.wait(retry_delay)
                retry_delay = min(5.0, retry_delay * 2.0)

    def status(self) -> dict:
        with self.lock:
            return {
                "url": self.endpoint,
                **queue_metrics(self.pending.values()),
                "last_error": self.last_error,
                "last_success_at": self.last_success_at,
                "worker_alive": self.thread.is_alive(),
                "agent_id": self.agent_id,
                "device_id": self.device_id,
                "config_path": str(self.config_path or ""),
            }

    def pending_ids(self) -> set[str]:
        with self.lock:
            return set(self.pending)


def _discover_pdd_shop_metadata(log_dir: str | Path) -> list[dict]:
    """Read only structured shop identity fields from recent local inside logs."""
    root = Path(log_dir)
    if not root.is_dir():
        return []
    from bridge.parser import parse_line
    from bridge.watcher import LogWatcher

    paths = []
    for path in root.glob("inside*.log*"):
        try:
            paths.append((path.stat().st_mtime_ns, path))
        except OSError:
            continue
    shops: dict[str, dict] = {}
    for _mtime, path in sorted(paths)[-8:]:
        try:
            with path.open("rb") as handle:
                size = path.stat().st_size
                if size > 2 * 1024 * 1024:
                    handle.seek(size - 2 * 1024 * 1024)
                    handle.readline()
                raw_lines = handle.read().splitlines()
        except OSError:
            continue
        for raw_line in raw_lines:
            line = LogWatcher._decode_line(raw_line)
            if "mallName" not in line and "shopName" not in line and "mall_name" not in line:
                continue
            for row in parse_line(line, "inside"):
                account = str(row.get("account") or "").strip()
                shop_name = str(row.get("shop_name") or "").strip()
                if not account.startswith("cs_") or ":" not in account or not shop_name:
                    continue
                mall_id = account[3:].split(":", 1)[0]
                if not mall_id.isdigit():
                    continue
                shop_id = f"mall_{mall_id}"
                shops[shop_id] = {
                    "type": "shop_metadata",
                    "platform": "pdd",
                    "shop_id": shop_id,
                    "shop_name": shop_name,
                    "account": account,
                    "source": "local_inside_metadata",
                    "event_id": "shop-metadata-" + hashlib.sha256(
                        f"{shop_id}\0{shop_name}\0{account}".encode("utf-8")
                    ).hexdigest()[:24],
                }
    return list(shops.values())


def _event_from_message(agent, message: dict) -> dict:
    message = stamp_message(message)
    account = str(message.get("account") or "").strip()
    return agent._ensure_event_id({
        "type": "message",
        "platform": message.get("platform") or agent.platform.name,
        "idempotency_key": message.get("idempotency_key") or "",
        "msg_id": message.get("msg_id"),
        "platform_message_id": message.get("platform_message_id") or "",
        "identity_kind": message.get("identity_kind") or "",
        "buyer_id": message.get("buyer_id"),
        "role": message.get("role"),
        "content": message.get("content"),
        "ts": message.get("ts"),
        "platform_ts_key": message.get("platform_ts_key") or "",
        "parent_msg_id": message.get("pre_msg_id") or message.get("parent_msg_id") or "",
        "account": account,
        "shop_id": message.get("shop_id") or "",
        "shop_name": message.get("shop_name") or message.get("mall_name") or "",
        "buyer_nick": message.get("buyer_nick") or message.get("nickname") or "",
        "order_context": message.get("order_context") or {},
        "order_id": message.get("order_id") or "",
        "order_info": message.get("order_info") or {},
        "local_context_lookup": message.get("local_context_lookup") or {},
        "goods_id": message.get("goods_id") or "",
        "goods_name": message.get("goods_name") or "",
        "goods_url": message.get("goods_url") or "",
        "goods_thumb_url": message.get("goods_thumb_url") or "",
        "goods_price": message.get("goods_price") or "",
        "goods_spec": message.get("goods_spec") or "",
        "goods_context_source": message.get("goods_context_source") or "",
        "template_name": message.get("template_name") or "",
        "raw_type": message.get("raw_type", message.get("message_type")),
        "source": message.get("source"),
        "delivery_status": message.get("delivery_status") or "",
        "agent_id": agent.cfg["agent_id"],
        **{key: message[key] for key in TIMING_FIELDS if key in message},
    })


def _message_with_local_context(agent, message: dict) -> dict:
    """Fill missing PDD account from the gateway's persisted seat bootstrap."""
    normalized = dict(message)
    if str(normalized.get("account") or "").strip():
        return normalized
    config_path = Path(str(agent.cfg.get("config_path") or _root() / "bridge_config.json"))
    state_path = config_path.resolve().parent / "data" / "seat_local_state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        state = {}
    active = {
        str(value or "").strip()
        for value in [*(state.get("active_shop_ids") or []), *(state.get("observed_shop_ids") or [])]
        if str(value or "").strip()
    }
    platforms = state.get("shop_platforms") if isinstance(state.get("shop_platforms"), dict) else {}
    platform = str(normalized.get("platform") or agent.platform.name or "pdd").strip().lower()
    candidates = [
        value for value in sorted(active)
        if str(platforms.get(value) or "").strip().lower() == platform
        or (platform == "pdd" and value.startswith("mall_"))
        or (platform == "taobao" and value.startswith("tb_"))
    ]
    if len(candidates) != 1:
        return normalized
    shop_id = candidates[0]
    accounts = state.get("shop_accounts") if isinstance(state.get("shop_accounts"), dict) else {}
    account = str(accounts.get(shop_id) or "").strip()
    if not account and platform == "pdd" and shop_id.startswith("mall_"):
        mall_id = shop_id[5:]
        account = f"cs_{mall_id}:0" if mall_id.isdigit() else ""
    if account:
        normalized["account"] = account
        normalized["shop_id"] = shop_id
    return normalized


def install_local_first() -> None:
    import bridge
    from bridge import agent as agent_module

    agent_class = agent_module.BridgeAgent
    if getattr(agent_class, "_local_first_v0512", False):
        return

    original_init = agent_class.__init__
    original_on_local_event = agent_class._on_local_event
    original_flush_events = agent_class._flush_events
    original_status_payload = agent_class._status_payload

    def needs_order_context(message: dict) -> bool:
        return bool(
            str(message.get("platform") or "pdd").strip().lower() == "pdd"
            and str(message.get("role") or "").strip().lower() == "user"
            and not message.get("is_history")
            and str(message.get("buyer_id") or "").strip()
            and not (
                isinstance(message.get("local_context_lookup"), dict)
                and "ok" in message.get("local_context_lookup", {})
            )
        )

    def context_update_event(event: dict) -> dict:
        updated = dict(event)
        original_id = str(event.get("event_id") or event.get("idempotency_key") or "")
        update_id = "pdd-context-" + hashlib.sha256(original_id.encode("utf-8")).hexdigest()[:32]
        updated["event_id"] = update_id
        updated["idempotency_key"] = update_id
        return updated

    def replace_pending_event(self, event_id: str, enriched_message: dict) -> bool:
        replacement = _event_from_message(self, enriched_message)
        replacement["event_id"] = event_id
        replacement["idempotency_key"] = event_id
        replaced = False
        with self._pending_lock:
            for index, current in enumerate(self._pending):
                current_id = str(current.get("event_id") or current.get("idempotency_key") or "")
                if current_id != event_id:
                    continue
                for field in TIMING_FIELDS:
                    if field in current:
                        replacement[field] = current[field]
                self._pending[index] = replacement
                replaced = True
                break
        # The original event is already durable. Batch ACK compaction persists
        # remaining enrichments; a crash can repeat lookup, not lose the message.
        return replaced

    def ensure_context_worker(self) -> None:
        if getattr(self, "_pdd_context_worker_started", False):
            return
        from bridge.pdd_context import PddOrderContextLookup, merge_lookup_result

        self._pdd_context_worker_started = True
        self._pdd_context_queue: queue.Queue[tuple[str, dict]] = queue.Queue()
        self._pdd_context_gate_lock = threading.RLock()
        self._pdd_context_pending_ids: set[str] = set()
        self._pdd_context_deadlines: dict[str, float] = {}
        # 订单上下文查询：单线程 + 固定 1.8s 时，1 分钟上百条必然堆积，堆积期间事件
        # 被挡在上传外 → 大脑收到消息但没有订单信息。改成并发池 + 会话级结果缓存。
        lookup_budget = max(0.4, min(3.0, float(self.cfg.get("pdd_context_timeout_seconds") or 1.0)))
        # 门控只挡“正在查上下文的那几条”（其他消息照常上传），而大脑的时效窗口是 60s，
        # 所以宁可多等几秒也别丢订单信息。默认 5s。
        self._pdd_context_gate_seconds = max(
            lookup_budget, float(self.cfg.get("pdd_context_gate_seconds") or 5.0)
        )
        context_workers = max(1, min(8, int(self.cfg.get("pdd_context_workers") or 4)))
        self._pdd_context_lookup = PddOrderContextLookup(
            timeout=lookup_budget,
            cache_seconds=float(self.cfg.get("pdd_context_cache_seconds") or 25.0),
        )
        self._pdd_context_stats = {
            "completed": 0,
            "failed": 0,
            "last_error": "",
            "last_completed_at": 0.0,
        }

        def note(outcome: str, error: str) -> None:
            # 多个 worker 共用这份统计：加锁避免计数丢失（现场要靠它算失败率）
            with self._pdd_context_gate_lock:
                self._pdd_context_stats[outcome] += 1
                self._pdd_context_stats["last_error"] = error
                self._pdd_context_stats["last_completed_at"] = time.time()

        def run() -> None:
            while not self._stop.is_set():
                try:
                    event_id, message = self._pdd_context_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                try:
                    with self._pdd_context_gate_lock:
                        deadline = self._pdd_context_deadlines.get(event_id, 0)
                    if time.monotonic() >= deadline:
                        # 到点放弃也要计入 failed，否则现场看到的失败率会严重低估。
                        note("failed", "context_deadline_exceeded")
                        replace_pending_event(self, event_id, {**message, "local_context_lookup": {
                            "ok": False, "source": "pdd_cdp_order_context", "error": "context_deadline_exceeded",
                        }})
                        continue
                    try:
                        result = self._pdd_context_lookup.lookup(message)
                    except Exception:
                        result = {
                            "local_context_lookup": {
                                "ok": False,
                                "source": "pdd_cdp_order_context",
                                "error": "lookup_worker_error",
                            }
                        }
                    if time.monotonic() >= deadline:
                        result = {"local_context_lookup": {
                            "ok": False, "source": "pdd_cdp_order_context", "error": "context_deadline_exceeded",
                        }}
                    enriched = merge_lookup_result(message, result)
                    replaced = replace_pending_event(self, event_id, enriched)
                    lookup = enriched.get("local_context_lookup") or {}
                    if lookup.get("ok"):
                        note("completed", "")
                    else:
                        note("failed", str(lookup.get("error") or "lookup_failed"))
                    if replaced and bool(self.cfg.get("dual_write_local_workbench", True)):
                        rich_event = context_update_event(_event_from_message(self, enriched))
                        self._local_delivery.enqueue(rich_event)
                except Exception:
                    note("failed", "context_commit_error")
                    fallback = dict(message)
                    fallback["local_context_lookup"] = {
                        "ok": False,
                        "source": "pdd_cdp_order_context",
                        "error": "context_commit_error",
                    }
                    try:
                        replace_pending_event(self, event_id, fallback)
                    except Exception:
                        pass
                finally:
                    with self._pdd_context_gate_lock:
                        self._pdd_context_pending_ids.discard(event_id)
                        self._pdd_context_deadlines.pop(event_id, None)
                    self._pdd_context_queue.task_done()
                    ensure_upload_worker(self)
                    self._immediate_upload_event.set()

        for index in range(context_workers):
            threading.Thread(
                target=run,
                name=f"pdd-order-context-{index + 1}",
                daemon=True,
            ).start()

    def ensure_upload_worker(self) -> None:
        if getattr(self, "_immediate_upload_started", False):
            return
        self._immediate_upload_started = True
        self._immediate_upload_event = threading.Event()
        self._immediate_upload_lock = threading.Lock()

        def run() -> None:
            while not self._stop.is_set():
                self._immediate_upload_event.wait(0.2)
                self._immediate_upload_event.clear()
                self._stop.wait(0.02)
                if not self._stop.is_set():
                    self._flush_events()

        threading.Thread(target=run, name="bridge-event-uploader", daemon=True).start()

    def init(self, *args, **kwargs) -> None:
        original_init(self, *args, **kwargs)
        self._pdd_system_filtered_events = 0
        from bridge.parser import configure_parser_profile

        config_path = Path(str(self.cfg.get("config_path") or _root() / "bridge_config.json")).resolve()
        profile_cache = Path(
            str(
                self.cfg.get("parser_profile_cache_path")
                or config_path.with_name("parser_profile_pdd.last_good.json")
            )
        )
        configure_parser_profile(
            self.cfg.get("parser_profile") or self.cfg.get("parser_profile_path"),
            profile_cache,
        )
        ensure_context_worker(self)
        queue_path = Path(self.cfg.get("local_queue_path") or "bridge_queue_pdd.jsonl")
        local_path = queue_path.with_name(f"{queue_path.stem}_local{queue_path.suffix or '.jsonl'}")
        self._local_delivery = LocalDeliveryQueue(
            endpoint=self.cfg.get("local_workbench_url") or "http://127.0.0.1:18767",
            token=self.cfg.get("agent_token") or "",
            agent_id=self.cfg.get("agent_id") or "",
            device_id=self.cfg.get("device_id") or self.cfg.get("agent_id") or "",
            config_path=config_path,
            path=local_path,
            stop_event=self._stop,
            ledger=getattr(self, "_ledger", None),
        )
        self._local_first_gate_lock = threading.RLock()
        self._local_first_deadlines: dict[str, float] = {}
        with self._pending_lock:
            center_pending = {
                str(item.get("event_id") or item.get("idempotency_key") or "")
                for item in self._pending
            }
        for event_id in center_pending.intersection(self._local_delivery.pending_ids()):
            if event_id:
                self._local_first_deadlines[event_id] = time.monotonic() + 1.0

        def seed_shop_metadata() -> None:
            rows = _discover_pdd_shop_metadata(self.cfg.get("tanyu_log_dir") or "")
            for row in rows:
                self._local_delivery.enqueue(row, wake=False)
            if rows:
                self._local_delivery.wakeup.set()

        threading.Thread(
            target=seed_shop_metadata,
            name="pdd-shop-metadata",
            daemon=True,
        ).start()

        # Recovered buyer events may have been persisted while the process was
        # interrupted during CDP lookup. Gate and enrich them before uploading.
        with self._pending_lock:
            recovered = [dict(item) for item in self._pending if needs_order_context(item)]
        for item in recovered:
            event_id = str(item.get("event_id") or item.get("idempotency_key") or "")
            if not event_id:
                continue
            with self._pdd_context_gate_lock:
                self._pdd_context_pending_ids.add(event_id)
                self._pdd_context_deadlines[event_id] = time.monotonic() + self._pdd_context_gate_seconds
            self._pdd_context_queue.put((event_id, item))
        ensure_upload_worker(self)

    def flush_events(self) -> None:
        ensure_upload_worker(self)
        if not self._immediate_upload_lock.acquire(blocking=False):
            return
        try:
            with self._pending_lock:
                center_pending = {
                    str(item.get("event_id") or item.get("idempotency_key") or "")
                    for item in self._pending
                }
            context_gate_lock = getattr(self, "_pdd_context_gate_lock", None)
            if context_gate_lock is not None:
                with context_gate_lock:
                    self._pdd_context_pending_ids.intersection_update(center_pending)
                    now = time.monotonic()
                    blocked_ids = {key for key in self._pdd_context_pending_ids
                                   if self._pdd_context_deadlines.get(key, 0) > now}
                with self._pending_lock:
                    for event in self._pending:
                        if (event.get("event_id") in self._pdd_context_pending_ids
                                and event.get("event_id") not in blocked_ids):
                            event["local_context_lookup"] = {
                                "ok": False, "source": "pdd_cdp_order_context",
                                "error": "context_deadline_exceeded",
                            }
            else:
                blocked_ids = set()
            while not self._stop.is_set():
                with self._pending_lock:
                    center_pending = {
                        str(item.get("event_id") or item.get("idempotency_key") or "")
                        for item in self._pending
                    }
                if not center_pending:
                    break
                local_pending = self._local_delivery.pending_ids()
                now = time.monotonic()
                with self._local_first_gate_lock:
                    blocked_until = [
                        deadline
                        for event_id, deadline in self._local_first_deadlines.items()
                        if event_id in center_pending
                        and event_id in local_pending
                        and deadline > now
                    ]
                if not blocked_until:
                    break
                self._stop.wait(min(0.02, max(0.0, min(blocked_until) - now)))
            original_flush_events(self, blocked_ids=blocked_ids)
            with self._pending_lock:
                remaining = {
                    str(item.get("event_id") or item.get("idempotency_key") or "")
                    for item in self._pending
                }
            with self._local_first_gate_lock:
                self._local_first_deadlines = {
                    event_id: deadline
                    for event_id, deadline in self._local_first_deadlines.items()
                    if event_id in remaining
                }
        finally:
            self._immediate_upload_lock.release()

    def on_local_event(self, message: dict) -> None:
        from bridge.parser import is_pdd_system_message
        if is_pdd_system_message(message):
            self._pdd_system_filtered_events += 1
            return
        message = stamp_message(_message_with_local_context(self, message))
        event = _event_from_message(self, message)
        local_enabled = bool(self.cfg.get("dual_write_local_workbench", True))
        if local_enabled:
            self._local_delivery.enqueue(event, wake=False)
            event_id = str(event.get("event_id") or event.get("idempotency_key") or "")
            if event_id:
                with self._local_first_gate_lock:
                    self._local_first_deadlines[event_id] = time.monotonic() + 1.0
        lookup_required = needs_order_context(message)
        event_id = str(event.get("event_id") or event.get("idempotency_key") or "")
        if lookup_required and event_id:
            ensure_context_worker(self)
            with self._pdd_context_gate_lock:
                if event_id in self._pdd_context_pending_ids:
                    lookup_required = False
                else:
                    self._pdd_context_pending_ids.add(event_id)
                    self._pdd_context_deadlines[event_id] = time.monotonic() + self._pdd_context_gate_seconds
        original_on_local_event(self, message)
        if lookup_required and event_id:
            self._pdd_context_queue.put((event_id, message))
        if local_enabled:
            self._local_delivery.wakeup.set()
        ensure_upload_worker(self)
        self._immediate_upload_event.set()

    def status_payload(self) -> dict:
        from bridge.parser import parser_profile_status

        payload = original_status_payload(self)
        payload["version"] = VERSION
        payload["build_hash"] = BUILD_HASH
        payload["local_first"] = True
        payload["local_delivery"] = self._local_delivery.status()
        payload["pdd_system_filtered_events"] = self._pdd_system_filtered_events
        payload["parser_profile"] = parser_profile_status()
        context_gate_lock = getattr(self, "_pdd_context_gate_lock", None)
        if context_gate_lock is None:
            context_pending = 0
        else:
            with context_gate_lock:
                context_pending = len(self._pdd_context_pending_ids)
        payload["pdd_order_context"] = {
            **getattr(self, "_pdd_context_stats", {}),
            "pending": context_pending,
        }
        return payload

    agent_class.__init__ = init
    agent_class._flush_events = flush_events
    agent_class._on_local_event = on_local_event
    agent_class._status_payload = status_payload
    agent_class._local_first_v0512 = True
    bridge.__version__ = VERSION
    agent_module.__version__ = VERSION


def _root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _adsorb_auto_start_enabled(root: Path | None = None) -> bool:
    config_path = (root or _root()) / "pdd_adsorb_config.json"
    if not config_path.is_file():
        return True
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        log.warning("ignoring invalid adsorb config %s: %s", config_path, exc)
        return True
    if not isinstance(payload, dict):
        return True
    return payload.get("auto_start_with_bridge", True) is not False


def _running_adsorb_pid(root: Path, executable: Path) -> int | None:
    pid_path = root / "state" / "pdd-adsorb.pid"
    try:
        pid = int(pid_path.read_text(encoding="ascii").strip())
        process = psutil.Process(pid)
        if _same_path(process.exe(), executable):
            return pid
    except (OSError, ValueError, psutil.Error):
        pass
    return None


def start_adsorb_window(root: Path | None = None) -> bool:
    """Start the optional PDD dock after the loopback workbench is healthy."""
    global _ADSORB_PROCESS_PID
    bundle_root = (root or _root()).resolve()
    if not _adsorb_auto_start_enabled(bundle_root):
        log.info("PDD adsorb auto-start is disabled")
        return False
    executable = bundle_root / "PddAdsorbWindow.exe"
    if not executable.is_file():
        log.warning("PddAdsorbWindow.exe not found; bridge continues without dock")
        return False
    existing_pid = _running_adsorb_pid(bundle_root, executable)
    if existing_pid:
        log.info("PDD adsorb window already running pid=%s", existing_pid)
        return False
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    process = subprocess.Popen(
        [str(executable)],
        cwd=str(bundle_root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    _ADSORB_PROCESS_PID = int(process.pid)
    log.info("PDD adsorb window started pid=%s", process.pid)
    return True


def stop_started_adsorb_window(root: Path | None = None) -> None:
    """Stop only the dock instance started by this bridge process."""
    global _ADSORB_PROCESS_PID
    with _ADSORB_STOP_LOCK:
        started_pid = _ADSORB_PROCESS_PID
        _ADSORB_PROCESS_PID = None
        if not started_pid:
            return
        bundle_root = (root or _root()).resolve()
        executable = bundle_root / "PddAdsorbWindow.exe"
        deadline = time.monotonic() + 2.0
        running_pid = _running_adsorb_pid(bundle_root, executable)
        while running_pid is None and time.monotonic() < deadline:
            time.sleep(0.05)
            running_pid = _running_adsorb_pid(bundle_root, executable)
        if running_pid != started_pid or not executable.is_file():
            return
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            stopper = subprocess.Popen(
                [str(executable), "--stop"],
                cwd=str(bundle_root),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            stopper.wait(timeout=10.0)
            log.info("PDD adsorb window stopped pid=%s", started_pid)
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("failed to stop PDD adsorb window pid=%s: %s", started_pid, exc)


def _load_raw_config(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _save_raw_config(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _gateway_status(base_url: str, *, force_reload: bool = False) -> dict[str, Any]:
    try:
        suffix = "?reload=1" if force_reload else ""
        req = urlrequest.Request(
            base_url.rstrip("/") + "/api/local-seat/v1/status" + suffix,
            headers={"Accept": "application/json"},
        )
        with urlrequest.urlopen(req, timeout=0.8) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    return {}


def _gateway_status_matches(status: dict, raw: dict, config_path: Path) -> bool:
    if not isinstance(status, dict) or status.get("ok") is not True:
        return False
    expected_agent = str(raw.get("agent_id") or "").strip()
    expected_device = str(raw.get("device_id") or expected_agent).strip()
    return bool(
        status.get("service") == "pdd-local-seat-gateway"
        and status.get("gateway_version") == VERSION
        and str(status.get("platform") or "").lower() == "pdd"
        and str(status.get("agent_id") or "").strip() == expected_agent
        and str(status.get("device_id") or "").strip() == expected_device
        and _same_path(status.get("config_path") or "", config_path)
    )


def _gateway_healthy(base_url: str, raw: dict, config_path: Path) -> bool:
    return _gateway_status_matches(_gateway_status(base_url), raw, config_path)


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=0.25):
            return True
    except OSError:
        return False


def _same_path(left: str | Path, right: str | Path) -> bool:
    try:
        return str(Path(left).resolve()).casefold() == str(Path(right).resolve()).casefold()
    except (OSError, RuntimeError, ValueError):
        return False


def _gateway_process_matches(
    pid: int,
    gateway: Path,
    config_path: Path,
    port: int,
) -> bool:
    """Only adopt a gateway from this bundle, config and loopback port."""
    try:
        process = psutil.Process(int(pid))
        if not _same_path(process.exe(), gateway):
            return False
        args = [str(value) for value in process.cmdline()]
    except (psutil.Error, OSError, ValueError):
        return False

    lowered = [value.casefold() for value in args]

    def argument(flag: str) -> str:
        try:
            index = lowered.index(flag.casefold())
        except ValueError:
            return ""
        return args[index + 1] if index + 1 < len(args) else ""

    try:
        command_port = int(argument("--port"))
    except (TypeError, ValueError):
        return False
    return bool(
        argument("--ui-role").casefold() == "seat"
        and command_port == int(port)
        and _same_path(argument("--bridge-config"), config_path)
    )


def _stop_jump_helpers(root: Path) -> None:
    """Stop helper processes belonging to this installed bundle."""
    target = (root / "PddJumpHelper.exe").resolve()
    processes: dict[int, psutil.Process] = {}
    try:
        for process in psutil.process_iter():
            try:
                if _same_path(process.exe(), target):
                    processes[process.pid] = process
                    for child in process.children(recursive=True):
                        if _same_path(child.exe(), target):
                            processes[child.pid] = child
            except (psutil.Error, OSError):
                continue
    except psutil.Error:
        return
    if not processes:
        return
    for process in processes.values():
        try:
            process.terminate()
        except (psutil.Error, OSError):
            pass
    _, alive = psutil.wait_procs(list(processes.values()), timeout=3.0)
    for process in alive:
        try:
            process.kill()
        except (psutil.Error, OSError):
            pass
    log.info("local jump helper stopped count=%s", len(processes))


def _remember_gateway(
    *,
    pid: int,
    gateway: Path,
    config_path: Path,
    port: int,
    process: subprocess.Popen | None = None,
) -> None:
    global _GATEWAY_PROCESS, _GATEWAY_PID, _GATEWAY_EXE
    global _GATEWAY_CONFIG, _GATEWAY_PORT, _GATEWAY_STOP_REGISTERED
    _GATEWAY_PROCESS = process
    _GATEWAY_PID = int(pid)
    _GATEWAY_EXE = gateway.resolve()
    _GATEWAY_CONFIG = config_path.resolve()
    _GATEWAY_PORT = int(port)
    if not _GATEWAY_STOP_REGISTERED:
        atexit.register(stop_local_gateway)
        _GATEWAY_STOP_REGISTERED = True


def stop_local_gateway() -> None:
    global _GATEWAY_PROCESS, _GATEWAY_PID, _GATEWAY_EXE
    global _GATEWAY_CONFIG, _GATEWAY_PORT
    with _GATEWAY_STOP_LOCK:
        helper_root = _GATEWAY_EXE.parent if _GATEWAY_EXE is not None else _root()
        _stop_jump_helpers(helper_root)
        pid = _GATEWAY_PID
        gateway = _GATEWAY_EXE
        config_path = _GATEWAY_CONFIG
        port = _GATEWAY_PORT
        _GATEWAY_PROCESS = None
        _GATEWAY_PID = None
        _GATEWAY_EXE = None
        _GATEWAY_CONFIG = None
        _GATEWAY_PORT = None

        if not pid or gateway is None or config_path is None or port is None:
            return
        if not _gateway_process_matches(pid, gateway, config_path, port):
            log.warning("refusing to stop unverified local gateway pid=%s", pid)
            return
        try:
            process = psutil.Process(pid)
            process.terminate()
            try:
                process.wait(timeout=5.0)
            except psutil.TimeoutExpired:
                process.kill()
                process.wait(timeout=3.0)
            log.info("local gateway stopped pid=%s", pid)
        except psutil.NoSuchProcess:
            pass
        except (psutil.Error, OSError) as exc:
            log.warning("failed to stop local gateway pid=%s: %s", pid, exc)


def start_local_gateway() -> None:
    global _GATEWAY_PROCESS
    root = _root()
    config_path = Path(os.environ.get("PDD_BRIDGE_CONFIG") or root / "bridge_config.json").resolve()
    raw = _load_raw_config(config_path)
    if not raw:
        raise RuntimeError(f"bridge config is missing or invalid JSON: {config_path}")
    if raw.get("manage_local_workbench", True) is False:
        return
    base_url = str(
        raw.get("local_workbench_url")
        or raw.get("local_seat_url")
        or os.environ.get("KEFU_LOCAL_SEAT_URL")
        or "http://127.0.0.1:18767"
    ).rstrip("/")
    parsed = urlparse(base_url)
    host = str(parsed.hostname or "127.0.0.1")
    requested_port = int(parsed.port or 18767)
    if host.lower() not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("local_workbench_url must use loopback")

    configured = str(raw.get("local_gateway_exe") or "").strip()
    gateway = Path(configured) if configured else root / "LocalSeatGateway.exe"
    if not gateway.is_absolute():
        gateway = root / gateway
    if not gateway.is_file():
        raise RuntimeError(f"LocalSeatGateway.exe not found: {gateway}")

    # Port 18766 remains reserved for Qianniu on dual-client machines. Legacy
    # PDD configs are migrated to a dedicated loopback port without touching
    # the bridge token, device id, durable queues, or shop settings.
    first_port = 18767 if requested_port == 18766 else requested_port
    candidate_ports = [first_port, *[value for value in range(18767, 18777) if value != first_port]]
    port = 0
    adopted: tuple[int, int] | None = None
    for candidate_port in candidate_ports:
        candidate_url = f"http://{host}:{candidate_port}"
        status = _gateway_status(candidate_url)
        if _gateway_status_matches(status, raw, config_path):
            try:
                existing_pid = int(status.get("pid") or 0)
            except (TypeError, ValueError):
                existing_pid = 0
            if existing_pid and _gateway_process_matches(
                existing_pid,
                gateway,
                config_path,
                candidate_port,
            ):
                adopted = (candidate_port, existing_pid)
                port = candidate_port
                break
        if not _port_open(host, candidate_port):
            port = candidate_port
            break
    if not port:
        raise RuntimeError("no dedicated loopback port is available for the PDD local gateway")

    base_url = f"http://{host}:{port}"
    configured_url = str(raw.get("local_workbench_url") or "").rstrip("/")
    if configured_url != base_url:
        raw["local_workbench_url"] = base_url
        _save_raw_config(config_path, raw)

    if adopted:
        existing_pid = adopted[1]
        _remember_gateway(
            pid=existing_pid,
            gateway=gateway,
            config_path=config_path,
            port=port,
        )
        log.info("adopted existing local gateway pid=%s", existing_pid)
        return

    status = _gateway_status(base_url)
    if status or _port_open(host, port):
        try:
            existing_pid = int(status.get("pid") or 0)
        except (TypeError, ValueError):
            existing_pid = 0
        raise RuntimeError(
            f"loopback port {port} is occupied by an incompatible service"
        )

    command = [
        str(gateway),
        "--ui-role", "seat",
        "--bridge-config", str(config_path),
        "--host", host,
        "--port", str(port),
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    _GATEWAY_PROCESS = subprocess.Popen(
        command,
        cwd=str(root),
        creationflags=creationflags,
    )
    _remember_gateway(
        pid=_GATEWAY_PROCESS.pid,
        gateway=gateway,
        config_path=config_path,
        port=port,
        process=_GATEWAY_PROCESS,
    )
    deadline = time.time() + 60.0  # 真实机器杀软扫描冻结 exe, 首启常超 20s
    while time.time() < deadline:
        if _GATEWAY_PROCESS.poll() is not None:
            raise RuntimeError(f"LocalSeatGateway exited with code {_GATEWAY_PROCESS.returncode}")
        status = _gateway_status(base_url, force_reload=True)
        if _gateway_status_matches(status, raw, config_path):
            try:
                status_pid = int(status.get("pid") or 0)
            except (TypeError, ValueError):
                status_pid = 0
            if status_pid == _GATEWAY_PROCESS.pid and _gateway_process_matches(
                status_pid,
                gateway,
                config_path,
                port,
            ):
                return
        time.sleep(0.2)
    stop_local_gateway()
    raise RuntimeError(f"LocalSeatGateway did not become healthy: {base_url}")


def _gateway_config_ready(config_path: str | Path | None = None) -> bool:
    """Return true once first-run setup has produced a usable bridge identity."""
    path = Path(
        config_path
        or os.environ.get("PDD_BRIDGE_CONFIG")
        or _root() / "bridge_config.json"
    ).resolve()
    raw = _load_raw_config(path)
    if not raw or raw.get("manage_local_workbench", True) is False:
        return False
    token = str(raw.get("agent_token") or "").strip().lower()
    agent_id = str(raw.get("agent_id") or "").strip()
    if not token or not agent_id:
        return False
    return not token.startswith(("change-me", "replace-me", "center-issued-token"))


def _retry_local_gateway_after_setup(stop_event: threading.Event) -> None:
    """Start the gateway when the GUI saves a valid first-run configuration."""
    last_error = ""
    while not stop_event.is_set():
        if not _gateway_config_ready():
            stop_event.wait(0.5)
            continue
        try:
            start_local_gateway()
            if not stop_event.is_set():
                start_adsorb_window()
            return
        except Exception as exc:
            error = str(exc)
            if error != last_error:
                log.error("local gateway startup retry failed (将持续自动重试): %s", error)
                last_error = error
            stop_event.wait(1.0)


def main(argv: list[str] | None = None) -> int:
    log_dir = _root() / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(log_dir / "bridge-pipeline.log", maxBytes=5 * 1024 * 1024,
                                      backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)
    except OSError:
        log.warning("pipeline log file unavailable")
    install_local_first()
    args = list(sys.argv[1:] if argv is None else argv)
    manage_gateway = not any(flag in args for flag in ("--status", "--init-config"))
    gateway_retry_stop = threading.Event()
    gateway_retry_thread: threading.Thread | None = None
    if manage_gateway:
        if _gateway_config_ready():
            try:
                start_local_gateway()
                start_adsorb_window()
            except Exception as exc:
                log.error("local gateway startup failed: %s", exc)
        if _GATEWAY_PID is None:
            gateway_retry_thread = threading.Thread(
                target=_retry_local_gateway_after_setup,
                args=(gateway_retry_stop,),
                name="pdd-local-gateway-first-run",
                daemon=True,
            )
            gateway_retry_thread.start()
    from bridge.run import main as bridge_main
    try:
        return bridge_main(["--platform", "pdd", *args])
    finally:
        if manage_gateway:
            gateway_retry_stop.set()
            if gateway_retry_thread is not None:
                gateway_retry_thread.join(timeout=1.0)
            stop_started_adsorb_window()
            stop_local_gateway()


if __name__ == "__main__":
    raise SystemExit(main())
