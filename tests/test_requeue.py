# -*- coding: utf-8 -*-
"""死信补推：默认不发、只发买家消息、接收的从死信里删、仍被拒的保留。"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from bridge.client import BridgeClientError
from bridge.ledger import DeliveryLedger, read_rows, summarize


def load_module():
    spec = importlib.util.spec_from_file_location(
        "requeue", str(Path(__file__).resolve().parent.parent / "requeue.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def row(event_id, reason, *, content="在吗", diagnostic=False, ts=1789636966):
    item = {"event_id": event_id, "buyer_id": "b1", "role": "user", "content": content,
            "ts": ts, "account": "cs_1:2", "refused_reason": reason}
    if diagnostic:
        item["is_diagnostic"] = True
        item["role"] = "platform"
    return item


class FakeClient:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.sent = []

    def upload_events(self, events):
        self.sent.extend(events)
        return {"ack_version": 1, "event_acks": [
            {"event_id": e["event_id"], **self.outcomes.get(e["event_id"], {"status": "rejected",
             "retryable": False, "error": {"code": "invalid_message"}})}
            for e in events]}


def test_select_excludes_diagnostics_by_default():
    mod = load_module()
    rows = [row("e1", "invalid_message"), row("e2", "unsupported_role", diagnostic=True),
            row("e3", "ignored", content="历史")]
    picked = mod.select_rows(rows)
    assert [r["event_id"] for r in picked] == ["e1", "e3"]
    assert [r["event_id"] for r in mod.select_rows(rows, include_diagnostics=True)] == ["e1", "e2", "e3"]
    assert [r["event_id"] for r in mod.select_rows(rows, reason="ignored")] == ["e3"]


def test_dry_run_sends_nothing():
    mod = load_module()
    client = FakeClient({})
    result = mod.requeue([row("e1", "invalid_message")], client, apply=False)
    assert client.sent == [] and result["sent"] == 0


def test_apply_records_outcomes_and_ledger(tmp_path):
    mod = load_module()
    rows = [row("e1", "invalid_message"), row("e2", "invalid_message"), row("e3", "invalid_message")]
    client = FakeClient({"e1": {"status": "accepted", "committed": True},
                         "e2": {"status": "ignored", "reason": "history"}})
    ledger = DeliveryLedger(tmp_path / "ledger.jsonl")
    result = mod.requeue(rows, client, apply=True, ledger=ledger)

    assert [e["event_id"] for e in client.sent] == ["e1", "e2", "e3"]
    assert "refused_reason" not in client.sent[0], "本地字段不该发回中心"
    assert (result["accepted"], result["ignored"], result["refused"]) == (1, 1, 1)
    summary = summarize(read_rows(tmp_path / "ledger.jsonl"))
    assert summary["counts"]["requeued"] == 3
    assert summary["counts"]["center_accepted"] == 1
    assert summary["counts"]["center_ignored"] == 1
    assert summary["counts"]["center_refused"] == 1


def test_upload_error_keeps_rows_and_reports():
    mod = load_module()

    class Boom:
        def upload_events(self, events):
            raise BridgeClientError("center down")

    result = mod.requeue([row("e1", "invalid_message")], Boom(), apply=True)
    assert result["sent"] == 0 and result["errors"]
    assert result["outcomes"]["e1"][0] == "error"


def test_select_dedupes_same_event_id():
    """死信里同一事件可能有多行（上传在 ack 前重发过）——补推不能推两遍。"""
    mod = load_module()
    rows = [row("e1", "invalid_message"), row("e1", "invalid_message"), row("e2", "invalid_message")]
    assert [r["event_id"] for r in mod.select_rows(rows)] == ["e1", "e2"]


def test_unselected_rows_survive_dead_letter_rewrite(tmp_path):
    """--apply 重写死信文件时，本次没尝试的行（诊断帧）不能被删掉。"""
    mod = load_module()
    rows = [row("e1", "invalid_message"), row("e2", "unsupported_role", diagnostic=True)]
    picked = mod.select_rows(rows)
    assert [r["event_id"] for r in picked] == ["e1"]
    client = FakeClient({"e1": {"status": "accepted", "committed": True}})
    result = mod.requeue(picked, client, apply=True)

    still = []
    for r in rows:
        outcome = result["outcomes"].get(r["event_id"])
        if outcome and outcome[0] == "accepted":
            continue
        still.append(r)
    assert [r["event_id"] for r in still] == ["e2"], "诊断帧必须留在死信里等中心改规则"
