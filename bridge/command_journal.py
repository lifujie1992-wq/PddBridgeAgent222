"""Durable Agent-side journal for command execution and result ACK retries."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any


class CommandJournal:
    VERSION = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._records: dict[str, dict[str, Any]] = {}
        self.last_error = ""
        self._load_ok = True
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        changed = False
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            raw_records = payload.get("records") if isinstance(payload, dict) else {}
            if not isinstance(raw_records, dict):
                raise ValueError("command journal records must be an object")
            for command_id, raw in raw_records.items():
                if not isinstance(raw, dict) or not str(command_id).strip():
                    continue
                record = dict(raw)
                if record.get("state") == "executing":
                    record["state"] = "result_pending"
                    record["result"] = {
                        "ok": False,
                        "status": "indeterminate",
                        "error": "agent restarted during command execution",
                        "error_user": "桥接在发送过程中重启，结果未知；为避免重复发送，请人工核对会话",
                        "real_send": None,
                        "via": "command_recovery",
                    }
                    record["recovered_at"] = time.time()
                    changed = True
                self._records[str(command_id)] = record
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.last_error = f"load_command_journal: {exc}"
            self._load_ok = False
            return
        if changed:
            with self._lock:
                self._persist_locked()

    def _persist_locked(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            serialized = json.dumps(
                {"version": self.VERSION, "records": self._records},
                ensure_ascii=False,
                indent=2,
            ) + "\n"
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            self.last_error = ""
            return True
        except OSError as exc:
            self.last_error = f"write_command_journal: {exc}"
            return False

    def start(self, command: dict[str, Any]) -> bool:
        command_id = str(command.get("id") or command.get("command_id") or "").strip()
        if not command_id:
            raise ValueError("command id is required")
        with self._lock:
            if not self._load_ok:
                return False
            previous = self._records.get(command_id)
            self._records[command_id] = {
                "state": "executing",
                "command": dict(command),
                "started_at": time.time(),
            }
            if self._persist_locked():
                return True
            if previous is None:
                self._records.pop(command_id, None)
            else:
                self._records[command_id] = previous
            return False

    def store_result(self, command_id: str, result: dict[str, Any]) -> bool:
        command_id = str(command_id or "").strip()
        if not command_id:
            raise ValueError("command id is required")
        with self._lock:
            record = dict(self._records.get(command_id) or {})
            record.update(
                state="result_pending",
                result=dict(result or {}),
                result_at=time.time(),
            )
            self._records[command_id] = record
            return self._persist_locked()

    def get(self, command_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._records.get(str(command_id or "").strip())
            return dict(record) if record is not None else None

    def pending_results(self) -> list[tuple[str, dict[str, Any]]]:
        with self._lock:
            return [
                (command_id, dict(record.get("result") or {}))
                for command_id, record in self._records.items()
                if record.get("state") == "result_pending"
                and isinstance(record.get("result"), dict)
            ]

    def remove(self, command_id: str) -> bool:
        with self._lock:
            self._records.pop(str(command_id or "").strip(), None)
            return self._persist_locked()

    def pending_count(self) -> int:
        with self._lock:
            return len(self._records)
