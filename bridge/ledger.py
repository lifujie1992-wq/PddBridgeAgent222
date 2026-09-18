# -*- coding: utf-8 -*-
"""投递台账：每条消息的 4 个状态逐条留痕，用来回答「工作台有、大脑没有，是谁的锅」。

一行一个状态变更（append-only JSONL），同一条消息可能有多行：
    captured   采集到并进入本机待发队列（消息正文/买家/账号在此行）
    local_ok   本机工作台(127.0.0.1:18767)确认收到      source=ingest / local_first
    local_fail 本机工作台投递失败                        detail=错误
    center_accepted  中心已接收（大脑一定会看到）
    center_ignored   中心/大脑主动忽略                   detail=原因
    center_refused   中心拒收（内容过滤/校验不通过）      detail=原因
    center_retry     中心让重试
    dead_letter      已写入死信文件（*_refused.jsonl）
    requeued         用 requeue.py 补推过一次（结果见紧随其后的 center_* 行）

写入必须便宜且绝不抛异常：宁可少一行台账，不能影响主链路。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger("pdd.bridge")

MAX_BYTES = 20 * 1024 * 1024
SUMMARY_STATES = ("captured", "local_ok", "local_fail", "center_accepted",
                  "center_terminal", "center_ignored", "center_refused", "center_retry",
                  "center_duplicate_skipped", "dead_letter", "requeued")


class DeliveryLedger:
    def __init__(self, path, enabled: bool = True, max_bytes: int = MAX_BYTES) -> None:
        self.path = Path(path)
        self.enabled = bool(enabled)
        self.max_bytes = int(max_bytes)
        self._lock = threading.Lock()
        self._size = -1

    # ---------------- 写入 ----------------
    def record(self, event_id: str, state: str, *, event: dict | None = None,
               detail: str = "", source: str = "") -> None:
        if not self.enabled or not event_id:
            return
        row = {"at": round(time.time(), 3), "event_id": str(event_id), "state": state}
        if source:
            row["source"] = source
        if detail:
            row["detail"] = str(detail)[:300]
        if event:
            for key in ("msg_id", "platform_message_id", "buyer_id", "role", "account",
                        "ts", "is_history", "is_diagnostic", "skipped_reason", "frame_kind"):
                if event.get(key) not in (None, ""):
                    row[key] = event[key]
            content = str(event.get("content") or "")
            if content:
                row["content_head"] = content[:80]
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self._size < 0:
                    self._size = self.path.stat().st_size if self.path.exists() else 0
                if self._size > self.max_bytes:
                    self._rotate_locked()
                line = json.dumps(row, ensure_ascii=False) + "\n"
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
                self._size += len(line.encode("utf-8"))
        except Exception as exc:      # 台账绝不能影响主链路
            log.debug("delivery ledger write failed: %s", exc)

    def _rotate_locked(self) -> None:
        try:
            backup = self.path.with_suffix(self.path.suffix + ".1")
            if backup.exists():
                backup.unlink()
            self.path.replace(backup)
        except OSError:
            pass
        self._size = 0

    def status(self) -> dict:
        return {"path": str(self.path), "enabled": self.enabled,
                "bytes": self._size if self._size >= 0 else None}


def read_rows(path) -> list[dict]:
    """读台账（含上一份轮转文件），按时间升序。"""
    rows: list[dict] = []
    base = Path(path)
    for candidate in (base.with_suffix(base.suffix + ".1"), base):
        if not candidate.is_file():
            continue
        try:
            for line in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
        except OSError:
            continue
    rows.sort(key=lambda r: float(r.get("at") or 0.0))
    return rows


def summarize(rows: list[dict], since: float | None = None) -> dict:
    """按 event_id 汇总每条消息的最终状态与状态链。"""
    per_event: dict[str, dict] = {}
    for row in rows:
        at = float(row.get("at") or 0.0)
        if since is not None and at < since:
            continue
        event_id = str(row.get("event_id") or "")
        if not event_id:
            continue
        item = per_event.setdefault(event_id, {"event_id": event_id, "states": {}, "chain": [],
                                               "account": "", "buyer_id": "", "content": "",
                                               "at": at, "detail": ""})
        state = str(row.get("state") or "")
        item["states"][state] = at
        item["chain"].append(state)
        item["at"] = min(item["at"], at)
        for key, target in (("account", "account"), ("buyer_id", "buyer_id")):
            if row.get(key) and not item[target]:
                item[target] = row[key]
        if row.get("content_head") and not item["content"]:
            item["content"] = row["content_head"]
        if row.get("detail") and state in ("center_refused", "center_ignored", "center_retry",
                                           "local_fail", "dead_letter"):
            item["detail"] = "%s: %s" % (state, row["detail"])
    counts = {state: 0 for state in SUMMARY_STATES}
    for item in per_event.values():
        for state in item["states"]:
            if state in counts:
                counts[state] += 1
    return {"events": per_event, "counts": counts}
