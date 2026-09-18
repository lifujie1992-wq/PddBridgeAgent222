# -*- coding: utf-8 -*-
"""投递台账与对账：必须能逐条回答「工作台有、大脑没有，是谁的锅」。"""
from __future__ import annotations

import json
from types import SimpleNamespace
from collections import deque
import threading

from bridge.agent import BridgeAgent
from bridge.ledger import DeliveryLedger, read_rows, summarize


def test_ledger_records_chain_and_rotates(tmp_path):
    path = tmp_path / "ledger.jsonl"
    led = DeliveryLedger(path, max_bytes=200)
    for i in range(50):
        led.record("e%d" % i, "captured", event={"buyer_id": "b", "content": "x" * 40})
    rows = read_rows(path)
    assert rows, "台账必须有内容"
    assert (tmp_path / "ledger.jsonl.1").exists(), "超过阈值要轮转一份"
    assert all(r["state"] == "captured" for r in rows)


def test_summarize_groups_by_event(tmp_path):
    path = tmp_path / "ledger.jsonl"
    led = DeliveryLedger(path)
    led.record("e1", "captured", event={"buyer_id": "b1", "content": "你好"})
    led.record("e1", "local_ok", source="ingest")
    led.record("e1", "center_refused", detail="invalid_message")
    led.record("e2", "captured", event={"buyer_id": "b2"})
    led.record("e2", "center_accepted")
    summary = summarize(read_rows(path))
    assert summary["counts"]["captured"] == 2
    assert summary["counts"]["center_accepted"] == 1
    assert summary["counts"]["center_refused"] == 1
    assert summary["events"]["e1"]["chain"] == ["captured", "local_ok", "center_refused"]
    assert summary["events"]["e1"]["detail"].startswith("center_refused")


def bare_agent(tmp_path):
    """只带台账所需字段的 agent（不启动线程，也不依赖 ingest 包装器）。"""
    agent = object.__new__(BridgeAgent)
    agent.cfg = {"agent_id": "offline-test", "dry_run": False}
    agent.platform = SimpleNamespace(name="pdd")
    agent.queue_path = tmp_path / "bridge_queue.jsonl"
    agent._ledger = DeliveryLedger(tmp_path / "ledger.jsonl")
    return agent


def test_ledger_captured_row_carries_buyer_and_content(tmp_path):
    agent = bare_agent(tmp_path)
    agent._ledger_note("e1", "captured", event={"buyer_id": "b1", "content": "在吗",
                                                "account": "cs_1:2", "msg_id": "m1"})
    rows = read_rows(tmp_path / "ledger.jsonl")
    assert [r["state"] for r in rows] == ["captured"]
    assert rows[0]["buyer_id"] == "b1" and rows[0]["content_head"] == "在吗"


def test_ledger_center_acks_cover_all_four_outcomes(tmp_path):
    agent = bare_agent(tmp_path)
    sent = {"e1": {"buyer_id": "b1"}, "e2": {"buyer_id": "b2"},
            "e3": {"buyer_id": "b3"}, "e4": {"buyer_id": "b4", "is_diagnostic": True}}
    agent._ledger_center_acks([
        {"event_id": "e1", "status": "accepted", "committed": True},
        {"event_id": "e2", "status": "ignored", "reason": "history"},
        {"event_id": "e3", "status": "rejected", "retryable": False,
         "error": {"code": "invalid_message"}},
        {"event_id": "e4", "status": "rejected", "retryable": True,
         "error": {"code": "unsupported_role"}},
    ], sent)
    counts = summarize(read_rows(tmp_path / "ledger.jsonl"))["counts"]
    assert counts["center_accepted"] == 1
    assert counts["center_ignored"] == 1
    assert counts["center_refused"] == 2
    assert counts["center_retry"] == 1


def test_ledger_dead_letter_row(tmp_path):
    agent = bare_agent(tmp_path)
    batch = [{"event_id": "e9", "buyer_id": "b9", "content": "98643187513451678"}]
    agent._record_event_loss(batch, [(batch[0], "invalid_message")])
    summary = summarize(read_rows(tmp_path / "ledger.jsonl"))
    assert summary["counts"]["dead_letter"] == 1
    assert "invalid_message" in summary["events"]["e9"]["detail"]
    dead = tmp_path / "bridge_queue_refused.jsonl"
    assert dead.exists() and "invalid_message" in dead.read_text(encoding="utf-8")


def test_reconcile_verdict_counts_the_gap(tmp_path):
    """工作台有、大脑没有 → 对账必须把它算成差额并归因。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "reconcile", str(__import__("pathlib").Path(__file__).resolve().parent.parent / "reconcile.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    path = tmp_path / "ledger.jsonl"
    led = DeliveryLedger(path)
    led.record("e1", "captured", event={"buyer_id": "b1"})
    led.record("e1", "local_ok", source="ingest")
    led.record("e1", "center_accepted")
    led.record("e2", "captured", event={"buyer_id": "b2"})
    led.record("e2", "local_ok", source="ingest")
    led.record("e2", "center_refused", detail="invalid_message")
    led.record("e3", "captured", event={"buyer_id": "b3"})
    led.record("e3", "local_ok", source="ingest")
    text = "\n".join(mod.verdict(summarize(read_rows(path))))
    assert "中心明确拒收     1 条" in text
    assert "没到大脑          2 条" in text


def test_log_backfill_parses_multi_id_refusal_and_terminal():
    """多 id 的 terminal / refused 行必须逐个解析（早期只用第一个 id, 会误报“无结果”）。"""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "reconcile2", str(Path(__file__).resolve().parent.parent / "reconcile.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    import tempfile, time
    log = Path(tempfile.mkdtemp()) / "bridge-pipeline.log"
    log.write_text(
        "2026-09-17 17:20:00,000 [INFO] pdd.bridge: event queued id=aaa111 msg_id=m1 ts=1 "
        "captured_at_ms=1 enqueued_at=1.0 history=False source=pdd_cdp\n"
        "2026-09-17 17:20:00,100 [INFO] pdd.bridge: event queued id=bbb222 msg_id=m2 ts=2 "
        "captured_at_ms=2 enqueued_at=2.0 history=False source=pdd_cdp\n"
        "2026-09-17 17:20:01,000 [INFO] pdd.bridge: event upload terminal=1 remaining=0 "
        "terminal_ids=['aaa111', 'bbb222']\n"
        "2026-09-17 17:20:02,000 [WARNING] pdd.bridge: event upload refused count=2 "
        "reasons=['invalid_message', 'ignored'] ids=['aaa111', 'bbb222']\n",
        encoding="utf-8")
    rows = mod.rows_from_pipeline_log(log, since=0)
    states = {}
    for row in rows:
        states.setdefault(row["event_id"], set()).add(row["state"])
    assert "center_terminal" in states["aaa111"] and "center_terminal" in states["bbb222"]
    assert "center_refused" in states["aaa111"], "被拒的 id 必须归到 center_refused"
    assert "center_refused" in states["bbb222"] or "center_ignored" in states["bbb222"]
    print("states:", {k: sorted(v) for k, v in states.items()})
