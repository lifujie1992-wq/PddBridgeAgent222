# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .platforms import get_platform
from .message_timing import stamp_message

EventCallback = Callable[[dict], None]
AttachSpec = Tuple[str, str, bool]

_PRODUCT_CONTEXT_FIELDS = (
    "goods_id",
    "goods_name",
    "goods_url",
    "goods_thumb_url",
    "goods_price",
    "goods_spec",
)
_PRODUCT_CONTEXT_TTL_SECONDS = 30 * 60
_MULTILINE_MAX_BYTES = 256 * 1024
_MULTILINE_TIMEOUT_SECONDS = 2.0
# Log reading: a single 256 KiB step per poll let a busy log fall minutes
# behind. Read in a bounded loop so a lagging cursor catches up in one poll.
_READ_STEP_BYTES = 256 * 1024
_READ_BUDGET_BYTES = 8 * 1024 * 1024
_READ_BUDGET_SECONDS = 0.5
# Never stop reading the log because the coalescing buffer is busy: stopping
# is what turned a small backlog into a minutes-long capture delay. Only a
# pathological buffer (far above normal) pauses reading as a last resort.
_PENDING_READ_LIMIT = 20000
_PENDING_FLUSH_BATCH = 500


class LogWatcher:
    """Tail Tanyu log files and emit parsed message dicts (platform-aware)."""

    def __init__(
        self,
        log_dir: str,
        on_event: EventCallback,
        *,
        poll_interval_ms: int = 400,
        platform: str = "pdd",
        parse_line=None,
        attach_specs: Optional[List[AttachSpec]] = None,
        checkpoint_path: Optional[Path] = None,
    ) -> None:
        self.log_dir = Path(log_dir)
        self.on_event = on_event
        self.poll_interval = max(0.2, float(poll_interval_ms) / 1000.0)
        self.platform = get_platform(platform)
        self._parse_line = parse_line or self.platform.parse_line
        self.attach_specs = attach_specs or self.platform.log_attach_specs()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.checkpoint_path = Path(checkpoint_path) if checkpoint_path else None
        try:
            self._checkpoints = json.loads(self.checkpoint_path.read_text(encoding="utf-8")) if self.checkpoint_path else {}
        except (OSError, ValueError):
            self._checkpoints = {}
        if not isinstance(self._checkpoints, dict):
            self._checkpoints = {}
        self._restored_sources: set[str] = set()
        self._known_paths: dict = {}
        self._backlog_paths: dict = {}
        self._identities: dict = {}
        self._history_until: dict = {}
        self._reading_history: dict = {}
        self._read_at: dict = {}
        self._handles: Dict[str, any] = {}
        self._paths: Dict[str, str] = {}
        self._buffers: Dict[str, bytes] = {}
        self.attached: Dict[str, str] = {}
        self.last_error = ""
        self.seen_keys: set[str] = set()
        self._seen_order: List[str] = []
        self._seen_at: Dict[str, float] = {}
        self._seller_semantic_seen: Dict[str, float] = {}
        self._seller_history: List[dict] = []
        self._pending_events: Dict[str, dict] = {}
        self._pending_lock = threading.RLock()
        self._product_context_by_message: Dict[Tuple[str, str, str], dict] = {}
        self._stats_lock = threading.Lock()
        self._stats: Dict[str, Dict[str, int]] = {}
        self._unmatched_samples: Dict[str, List[str]] = {}
        self._multiline_candidates: Dict[str, Tuple[str, float]] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="bridge-log-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._flush_pending(force=True)
        for handle in list(self._handles.values()):
            try:
                handle.close()
            except Exception:
                pass
        self._handles.clear()
        self._buffers.clear()
        self._multiline_candidates.clear()

    def _pick(self, pattern: str, prefer_nonempty: bool = False) -> Optional[Path]:
        if not self.log_dir.is_dir():
            return None
        files = []
        for path in self.log_dir.glob(pattern):
            try:
                st = path.stat()
                files.append((st.st_mtime, st.st_size, path))
            except OSError:
                continue
        files.sort(key=lambda item: item[0], reverse=True)
        if prefer_nonempty:
            nonempty = [item for item in files if item[1] > 0]
            if nonempty:
                return nonempty[0][2]
        return files[0][2] if files else None

    def _attach(self, name: str, pattern: str, prefer_nonempty: bool = False) -> None:
        path = self._pick(pattern, prefer_nonempty=prefer_nonempty)
        if not path:
            return
        current = self._handles.get(name)
        files = []
        for candidate in self.log_dir.glob(pattern):
            try:
                files.append((candidate.stat().st_mtime_ns, str(candidate)))
            except OSError:
                continue
        files.sort()
        saved = self._checkpoints.get(name) or {}
        backlog = self._backlog_paths.setdefault(name, [])
        if name not in self._known_paths:
            if saved:
                pending_paths = list(saved.get("remaining_paths") or [])
                if "known_paths" in saved:
                    pending_paths += [p for _mtime, p in files if p not in saved["known_paths"]]
                else:
                    pending_paths += [p for mtime, p in files if mtime >= saved.get("mtime_ns", 0)]
                backlog.extend(dict.fromkeys(pending_paths))
        else:
            backlog.extend(p for _mtime, p in files if p not in self._known_paths[name] and p not in backlog)
        self._known_paths[name] = {p for _mtime, p in files}
        while backlog and not Path(backlog[0]).is_file():
            backlog.pop(0)
        if backlog:
            path = Path(backlog[0])
        if current is None and name not in self._restored_sources:
            # A rename during downtime can leave unread bytes in the archived file.
            candidates = [Path(saved["path"])] if saved.get("path") else []
            candidates.extend(self.log_dir.glob(pattern))
            for candidate in candidates:
                try:
                    stat = candidate.stat()
                    if [stat.st_dev, stat.st_ino] == saved.get("identity"):
                        path = candidate
                        break
                except OSError:
                    continue
            self._restored_sources.add(name)
        sp = str(path)
        previous = self._paths.get(name) or ""
        rotate_mode = "none"
        if previous == sp and current is not None:
            try:
                stat = path.stat()
                if [stat.st_dev, stat.st_ino] != self._identities.get(name):
                    if os.fstat(current.fileno()).st_size > current.tell():
                        return
                    self._flush_pending(force=True)
                    current.close()
                    rotate_mode = "path_change"
                elif stat.st_size >= current.tell():
                    return
                else:
                    current.close()
                    rotate_mode = "truncate"
            except Exception:
                try:
                    current.close()
                except Exception:
                    pass
                rotate_mode = "truncate"
        elif current is not None:
            # Drain the old file in bounded rounds before switching paths.
            try:
                if os.fstat(current.fileno()).st_size > current.tell():
                    return
            except OSError:
                pass
            self._flush_pending(force=True)
            try:
                current.close()
            except Exception:
                pass
            rotate_mode = "path_change"
        else:
            rotate_mode = "first"
        try:
            handle = open(sp, "rb")
            stat = os.fstat(handle.fileno())
            size = stat.st_size
            identity = [stat.st_dev, stat.st_ino]
            saved = self._checkpoints.get(name) or {}
            saved_position = saved.get("position")
            recoverable = (
                saved.get("identity") == identity
                and isinstance(saved_position, int) and 0 <= saved_position <= size
            )
            if rotate_mode == "first" and recoverable:
                handle.seek(saved_position)
            elif rotate_mode == "truncate":
                handle.seek(0)
            elif rotate_mode == "first" and saved:
                handle.seek(0)
            elif rotate_mode == "first":
                # A live bridge must not replay the existing log on startup. Historical
                # chats use the explicit import path, which persists context without
                # enqueueing a shadow reply for every old buyer message.
                handle.seek(0, os.SEEK_END)
            elif rotate_mode == "path_change":
                handle.seek(0)
            else:
                handle.seek(0, os.SEEK_END)
            self._history_until[name] = size if rotate_mode != "first" or saved else 0
            self._identities[name] = identity
            if sp in backlog:
                backlog.remove(sp)
            self._handles[name] = handle
            self._paths[name] = sp
            self._buffers[name] = b""
            self.attached[name] = path.name
        except Exception as exc:
            self.last_error = f"attach {name}: {exc}"

    def _save_checkpoints(self) -> None:
        # A cursor is committed only after every parsed event reached its durable queue.
        if not self.checkpoint_path or self._pending_events or self._multiline_candidates:
            return
        checkpoints = dict(self._checkpoints)
        for name, handle in self._handles.items():
            checkpoints[name] = {
                "identity": self._identities[name], "path": self._paths[name],
                "position": handle.tell() - len(self._buffers.get(name, b"")),
                "mtime_ns": os.fstat(handle.fileno()).st_mtime_ns,
                "remaining_paths": list(self._backlog_paths.get(name, [])),
                "known_paths": sorted(self._known_paths.get(name, set())),
            }
        if checkpoints != self._checkpoints:
            self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.checkpoint_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(checkpoints), encoding="utf-8")
            temporary.replace(self.checkpoint_path)
            self._checkpoints = checkpoints

    @staticmethod
    def _decode_line(raw: bytes) -> str:
        for encoding in ("utf-8", "gb18030", "gbk"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", "replace")

    def _read_new(self, name: str) -> List[str]:
        handle = self._handles.get(name)
        if not handle:
            return []
        try:
            self._reading_history[name.lower()] = handle.tell() < self._history_until.get(name, 0)
            self._read_at[name.lower()] = time.time()
            lines: List[str] = []
            remaining = _READ_BUDGET_BYTES
            deadline = time.monotonic() + _READ_BUDGET_SECONDS
            while remaining > 0 and time.monotonic() < deadline:
                step = min(_READ_STEP_BYTES, remaining)
                if self._reading_history[name.lower()]:
                    step = min(step, self._history_until[name] - handle.tell())
                if step <= 0:
                    break
                data = handle.read(step)
                if not data:
                    break
                remaining -= len(data)
                data = self._buffers.get(name, b"") + data
                chunks = data.split(b"\n")
                self._buffers[name] = chunks.pop() if chunks else data
                if len(self._buffers[name]) > 8 * 1024 * 1024:
                    chunks.append(self._buffers[name])
                    self._buffers[name] = b""
                lines.extend(self._decode_line(c.rstrip(b"\r")) for c in chunks if c.strip())
            return lines
        except Exception as exc:
            self.last_error = f"read {name}: {exc}"
            try:
                handle.close()
            except Exception:
                pass
            self._handles.pop(name, None)
            self._paths.pop(name, None)
            self._buffers.pop(name, None)
            return []

    def _remember(self, key: str, now: Optional[float] = None) -> bool:
        if key in self.seen_keys:
            return False
        now = float(now or time.monotonic())
        self.seen_keys.add(key)
        self._seen_at[key] = now
        self._seen_order.append(key)
        if len(self._seen_order) > 20000:
            old = self._seen_order[:5000]
            self._seen_order = self._seen_order[5000:]
            for item in old:
                self.seen_keys.discard(item)
                self._seen_at.pop(item, None)
        return True

    @staticmethod
    def _normalized_content(value: object) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    @staticmethod
    def _hash_key(*parts: object) -> str:
        raw = "\x00".join(str(part or "") for part in parts)
        return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()

    def _event_keys(self, message: dict) -> tuple[str, str]:
        platform_id = str(message.get("platform_message_id") or "").strip()
        if platform_id:
            exact = "platform:" + self._hash_key(self.platform.name, platform_id)
        else:
            exact = "fallback:" + self._hash_key(
                self.platform.name,
                message.get("account"),
                message.get("buyer_id"),
                message.get("role"),
                self._normalized_content(message.get("content")),
                message.get("platform_ts_key") or message.get("ts"),
            )
        semantic = ""
        if str(message.get("role") or "").strip().lower() != "user":
            semantic = "seller:" + self._hash_key(
                self.platform.name,
                message.get("account"),
                message.get("buyer_id"),
                message.get("role"),
                self._normalized_content(message.get("content")),
            )
        return exact, semantic

    def _seller_scope(self, message: dict) -> str:
        return self._hash_key(
            self.platform.name,
            message.get("account"),
            message.get("buyer_id"),
            message.get("role"),
        )

    def _same_seller_content(self, left: object, right: object) -> bool:
        first = self._normalized_content(left)
        second = self._normalized_content(right)
        if first == second:
            return True
        shorter, longer = sorted((first, second), key=len)
        prefix = shorter.rstrip(".…")
        return len(prefix) >= 8 and longer.startswith(prefix)

    @staticmethod
    def _product_scope(message: dict) -> Tuple[str, str]:
        return (
            str(message.get("account") or "").strip(),
            str(message.get("buyer_id") or "").strip(),
        )

    @staticmethod
    def _message_timestamp(message: dict) -> float:
        try:
            return float(message.get("ts") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _has_product_context(fields: dict) -> bool:
        goods_id = str(fields.get("goods_id") or "").strip()
        goods_url = str(fields.get("goods_url") or "").strip().lower()
        return bool(goods_id or "goods_id=" in goods_url)

    def _with_product_context(self, message: dict, source: str, now: float) -> dict:
        """Carry an explicit PDD product through its pre_msg_id conversation chain."""
        row = dict(message)
        account, buyer_id = self._product_scope(row)
        if not account or not buyer_id:
            return row

        explicit = {
            key: row.get(key)
            for key in _PRODUCT_CONTEXT_FIELDS
            if row.get(key) not in (None, "")
        }
        context = None
        if self._has_product_context(explicit):
            context = {
                "fields": explicit,
                "origin_ts": self._message_timestamp(row),
                "seen_at": now,
                "quality": self._quality(row, source),
            }
        else:
            parent_id = str(
                row.get("pre_msg_id") or row.get("parent_msg_id") or ""
            ).strip()
            cached = self._product_context_by_message.get((account, buyer_id, parent_id))
            if cached:
                origin_ts = float(cached.get("origin_ts") or 0.0)
                message_ts = self._message_timestamp(row)
                elapsed = now - float(cached.get("seen_at") or now)
                platform_age = message_ts - origin_ts if message_ts and origin_ts else 0.0
                valid_time = (
                    elapsed <= _PRODUCT_CONTEXT_TTL_SECONDS
                    and platform_age >= -1.0
                    and platform_age <= _PRODUCT_CONTEXT_TTL_SECONDS
                )
                if valid_time:
                    context = cached
                    if str(row.get("role") or "").strip().lower() == "user":
                        for key, value in (cached.get("fields") or {}).items():
                            if row.get(key) in (None, ""):
                                row[key] = value
                        row["goods_context_source"] = "pdd_pre_msg_chain"

        if context:
            own_ids = {
                str(row.get("platform_message_id") or "").strip(),
                str(row.get("msg_id") or "").strip(),
            }
            for message_id in own_ids:
                if not message_id:
                    continue
                key = (account, buyer_id, message_id)
                current = self._product_context_by_message.get(key)
                if current is None or int(context.get("quality") or 0) >= int(current.get("quality") or 0):
                    self._product_context_by_message[key] = context

        if len(self._product_context_by_message) > 20000:
            cutoff = now - _PRODUCT_CONTEXT_TTL_SECONDS
            self._product_context_by_message = {
                key: value
                for key, value in self._product_context_by_message.items()
                if float(value.get("seen_at") or 0.0) >= cutoff
            }
        return row

    @staticmethod
    def _quality(message: dict, source: str) -> int:
        source = str(source or "").lower()
        msg_id = str(message.get("msg_id") or "").lower()
        status = str(message.get("delivery_status") or "").lower()
        if msg_id.startswith("callback-") or status == "confirmed" and source in {"inside", "callback"}:
            score = 700
        else:
            score = {
                "inside": 600,
                "callback": 590,
                "inject": 500,
                "logrus": 420,
                "plugin": 200,
            }.get(source, 300)
        content = str(message.get("content") or "")
        score += min(len(content), 300)
        score -= content.count("\ufffd") * 200
        score -= sum(content.count(marker) for marker in ("脙", "脗", "氓", "盲", "忙")) * 25
        score -= sum(1 for char in content if ord(char) < 32 and char not in "\r\n\t") * 50
        if content.endswith(("...", "…")):
            score -= 40
        return score

    @staticmethod
    def _redact_sample(line: str) -> str:
        text = str(line or "")
        text = re.sub(
            r"(?i)(agent[_-]?token|authorization|x-agent-token|bridge[_-]?token)(\s*[=:]\s*)[^\s,;\]}]+",
            r"\1\2***",
            text,
        )
        text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "***PHONE***", text)
        text = re.sub(r"(?<!\d)\d{12,}(?!\d)", "***LONG_ID***", text)
        return text[:512]

    def _record_unmatched_sample(self, source: str, line: str) -> None:
        sample = self._redact_sample(line)
        if not sample:
            return
        with self._stats_lock:
            rows = self._unmatched_samples.setdefault(source, [])
            if sample not in rows and len(rows) < 3:
                rows.append(sample)

    def _is_history_duplicate(
        self,
        exact: str,
        semantic: str,
        now: float,
        message: Optional[dict] = None,
    ) -> bool:
        if exact in self.seen_keys:
            return True
        if semantic:
            previous = float(self._seller_semantic_seen.get(semantic) or 0.0)
            if previous and now - previous <= 5.0:
                return True
            if message is not None:
                scope = self._seller_scope(message)
                for previous_event in reversed(self._seller_history):
                    if now - float(previous_event.get("at") or 0.0) > 5.0:
                        break
                    if previous_event.get("scope") == scope and self._same_seller_content(
                        previous_event.get("content"), message.get("content")
                    ):
                        return True
        return False

    def _queue_message(self, message: dict, source: str) -> None:
        now = time.monotonic()
        message = self._with_product_context(message, source, now)
        exact, semantic = self._event_keys(message)
        if self._is_history_duplicate(exact, semantic, now, message):
            self._bump(source, "duplicate_events")
            return
        group = semantic or exact
        hold = 0.6 if semantic else 0.15
        quality = self._quality(message, source)
        with self._pending_lock:
            if semantic:
                scope = self._seller_scope(message)
                for pending_key, pending in self._pending_events.items():
                    pending_message = pending.get("message") or {}
                    if (
                        pending.get("semantic")
                        and self._seller_scope(pending_message) == scope
                        and self._same_seller_content(pending_message.get("content"), message.get("content"))
                    ):
                        group = pending_key
                        break
            existing = self._pending_events.get(group)
            if existing is None:
                self._pending_events[group] = {
                    "message": message,
                    "source": source,
                    "exact": exact,
                    "semantic": semantic,
                    "quality": quality,
                    "first_seen": now,
                    "due": now + hold,
                }
                return
            self._bump(source, "duplicate_events")
            if quality > int(existing.get("quality") or 0):
                for field in ("captured_at", "captured_at_ms"):
                    if field in existing["message"]:
                        message[field] = existing["message"][field]
                existing.update({
                    "message": message,
                    "source": source,
                    "exact": exact,
                    "semantic": semantic,
                    "quality": quality,
                })
                self._bump(source, "quality_replacements")

    def _flush_pending(self, *, force: bool = False) -> None:
        now = time.monotonic()
        with self._pending_lock:
            ready = [
                (key, value)
                for key, value in self._pending_events.items()
                if force or float(value.get("due") or 0.0) <= now
            ]
            ready.sort(key=lambda item: float(item[1].get("first_seen") or 0.0))
            if not force:
                ready = ready[:_PENDING_FLUSH_BATCH]
        for _key, item in ready:
            message = item["message"]
            source = str(item.get("source") or "")
            exact = str(item.get("exact") or "")
            semantic = str(item.get("semantic") or "")
            if self._is_history_duplicate(exact, semantic, now, message):
                self._bump(source, "duplicate_events")
                with self._pending_lock:
                    self._pending_events.pop(_key, None)
                continue
            self.on_event(message)
            with self._pending_lock:
                self._pending_events.pop(_key, None)
            self._remember(exact, now)
            if semantic:
                self._seller_semantic_seen[semantic] = now
                self._seller_history.append({
                    "scope": self._seller_scope(message),
                    "content": self._normalized_content(message.get("content")),
                    "at": now,
                })
            self._bump(source, "emitted_events")
        cutoff = now - 10.0
        for key, seen_at in list(self._seller_semantic_seen.items()):
            if seen_at < cutoff:
                self._seller_semantic_seen.pop(key, None)
        self._seller_history = [row for row in self._seller_history if float(row.get("at") or 0.0) >= cutoff]

    def _bump(self, source: str, field: str, amount: int = 1) -> None:
        with self._stats_lock:
            stats = self._stats.setdefault(
                source,
                {
                    "lines_read": 0,
                    "candidate_lines": 0,
                    "unmatched_candidates": 0,
                    "parsed_events": 0,
                    "emitted_events": 0,
                    "duplicate_events": 0,
                    "quality_replacements": 0,
                    "parse_errors": 0,
                },
            )
            stats[field] = int(stats.get(field) or 0) + amount

    def diagnostics(self) -> Dict[str, object]:
        with self._stats_lock:
            sources = {name: dict(values) for name, values in self._stats.items()}
            samples = {name: list(values) for name, values in self._unmatched_samples.items()}
        files: Dict[str, Dict[str, object]] = {}
        for name, path in list(self._paths.items()):
            handle = self._handles.get(name)
            try:
                position = int(handle.tell()) if handle else 0
            except Exception:
                position = 0
            try:
                size = int(os.path.getsize(path))
            except OSError:
                size = 0
            files[name.lower()] = {
                "name": Path(path).name,
                "position": position,
                "size": size,
                "lag_bytes": max(0, size - position),
            }
        totals: Dict[str, int] = {}
        for values in sources.values():
            for field, value in values.items():
                totals[field] = totals.get(field, 0) + int(value or 0)
        return {
            "sources": sources,
            "files": files,
            "totals": totals,
            "unmatched_candidate_samples": samples,
            "parsed_pending": len(self._pending_events),
            "oldest_parsed_wait_seconds": max(
                (time.monotonic() - row["first_seen"] for row in list(self._pending_events.values())),
                default=0.0,
            ),
        }

    def _expire_multiline_candidates(self) -> None:
        cutoff = time.monotonic() - _MULTILINE_TIMEOUT_SECONDS
        for source, (_text, started_at) in list(self._multiline_candidates.items()):
            if started_at <= cutoff:
                self._multiline_candidates.pop(source, None)

    def _emit_line(self, line: str, source: str) -> None:
        self._bump(source, "lines_read")
        candidate_fn = getattr(self.platform, "is_candidate_line", None)
        candidate = bool(candidate_fn(line)) if callable(candidate_fn) else False
        if candidate:
            self._bump(source, "candidate_lines")
        try:
            failure_fn = getattr(self.platform, "is_failure_line", None)
            failure = bool(failure_fn(line)) if callable(failure_fn) else False
            pending = self._multiline_candidates.get(source)
            if failure:
                self._multiline_candidates.pop(source, None)
                messages = []
            elif pending:
                combined = pending[0] + "\n" + line
                messages = []
                if (
                    len(combined.encode("utf-8", "replace")) <= _MULTILINE_MAX_BYTES
                    and time.monotonic() - pending[1] <= _MULTILINE_TIMEOUT_SECONDS
                ):
                    messages = list(self._parse_line(combined, source))
                if messages:
                    self._multiline_candidates.pop(source, None)
                else:
                    current_messages = list(self._parse_line(line, source))
                    if current_messages:
                        self._multiline_candidates.pop(source, None)
                        messages = current_messages
                    elif len(combined.encode("utf-8", "replace")) <= _MULTILINE_MAX_BYTES:
                        self._multiline_candidates[source] = (combined, pending[1])
                        return
                    else:
                        self._multiline_candidates.pop(source, None)
            else:
                messages = list(self._parse_line(line, source))
            if messages and not candidate:
                candidate = True
                self._bump(source, "candidate_lines")
            if candidate and not messages:
                self._bump(source, "unmatched_candidates")
                self._record_unmatched_sample(source, line)
                if (
                    not failure
                    and any(marker in line for marker in ("business_message", "originData", "SendContent"))
                    and len(line.encode("utf-8", "replace")) <= _MULTILINE_MAX_BYTES
                ):
                    self._multiline_candidates[source] = (line, time.monotonic())
            self._bump(source, "parsed_events", len(messages))
            for msg in messages:
                msg.setdefault("captured_at", self._read_at.get(source, time.time()))
                msg = stamp_message(msg, historical=self._reading_history.get(source, False))
                msg["log_file"] = self.attached.get(source.upper(), "")
                self._queue_message(msg, source)
        except Exception as exc:
            self._bump(source, "parse_errors")
            self.last_error = f"parse {source}: {exc}"

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                for name, pattern, prefer_nonempty in self.attach_specs:
                    self._attach(name, pattern, prefer_nonempty=prefer_nonempty)
                self._save_checkpoints()
                if len(self._pending_events) < _PENDING_READ_LIMIT:
                    for name in list(self._handles):
                        for line in self._read_new(name):
                            self._emit_line(line, name.lower())
                self._flush_pending()
                self._expire_multiline_candidates()
                self._save_checkpoints()
            except Exception as exc:
                self.last_error = f"watcher: {exc}"
            self._stop.wait(self.poll_interval)
