# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import threading
from pathlib import Path

from bridge.agent import BridgeAgent
from bridge.client import BridgeClient
from bridge.parser import BUILTIN_PARSER_PROFILE, parse_line
from bridge.watcher import LogWatcher
from run_pdd_client import BUILD_HASH, VERSION, install_local_first


def _business_line(
    *,
    content: str = "虚构客服短消息",
    buyer_id: str | int = "900000000000000000001",
    seller_id: str = "cs_123_456",
    timestamp: str | int = "1787061600123",
    message_id: str = "fictional-message-1",
) -> str:
    origin = {
        "SendContent": content,
        "BuyId": buyer_id,
        "SellerId": seller_id,
        "TimeStamp": timestamp,
    }
    outer = {
        "cmd": "business_message",
        "MessageId": message_id,
        "data": {"originData": json.dumps(origin, ensure_ascii=False)},
    }
    return "2026-08-18 22:00:00.123 [inside] " + json.dumps(outer, ensure_ascii=False)


def test_nested_origin_data_short_text_and_account_normalization() -> None:
    row = parse_line(_business_line(), "inside")[0]
    assert row["role"] == "mall_cs"
    assert row["buyer_id"] == "900000000000000000001"
    assert row["account"] == "cs_123:456"
    assert row["content"] == "虚构客服短消息"
    assert row["ts"] == 1787061600
    assert row["platform_message_id"] == "fictional-message-1"
    assert row["delivery_status"] == "confirmed"


def test_origin_data_alone_accepts_number_buyer_seconds_and_stable_id() -> None:
    origin = {
        "SendContent": "独立 originData",
        "BuyerId": 9876543210123456789012345,
        "SellerId": "cs_789_12",
        "TimeStamp": 1787000001,
    }
    line = '"originData":' + json.dumps(json.dumps(origin, ensure_ascii=False), ensure_ascii=False)
    first = parse_line(line, "inside")[0]
    second = parse_line(line, "inject")[0]
    assert first["buyer_id"] == "9876543210123456789012345"
    assert first["account"] == "cs_789:12"
    assert first["ts"] == 1787000001
    assert first["msg_id"] == second["msg_id"]
    assert first["idempotency_key"] == second["idempotency_key"]


def test_multiline_emoji_quotes_and_newline_is_buffered() -> None:
    content = '第一行😀\n第二行 "引号"'
    line = _business_line(content=content)
    split_at = line.index('"originData"')
    physical = [line[:split_at], line[split_at:]]
    emitted: list[dict] = []
    watcher = LogWatcher(".", emitted.append, platform="pdd", attach_specs=[])
    watcher._emit_line(physical[0], "inside")
    assert emitted == []
    watcher._emit_line(physical[1], "inside")
    watcher._flush_pending(force=True)
    assert len(emitted) == 1
    assert emitted[0]["content"] == content
    assert len(watcher._multiline_candidates) == 0


def test_inside_inject_duplicate_business_message_emits_once() -> None:
    inside = parse_line(_business_line(), "inside")[0]
    inject = parse_line(_business_line(), "inject")[0]
    emitted: list[dict] = []
    watcher = LogWatcher(".", emitted.append, platform="pdd", attach_specs=[])
    watcher._queue_message(inside, "inside")
    watcher._queue_message(inject, "inject")
    watcher._flush_pending(force=True)
    assert len(emitted) == 1
    assert emitted[0]["source"] == "inside"


def test_multiline_candidate_buffer_is_bounded_and_expires() -> None:
    watcher = LogWatcher(".", lambda _row: None, platform="pdd", attach_specs=[])
    watcher._emit_line("business_message {", "inside")
    text, started_at = watcher._multiline_candidates["inside"]
    watcher._multiline_candidates["inside"] = (text, started_at - 3.0)
    watcher._expire_multiline_candidates()
    assert "inside" not in watcher._multiline_candidates

    watcher._emit_line("business_message " + "x" * (256 * 1024), "inside")
    assert "inside" not in watcher._multiline_candidates


def test_failure_marker_is_ignored() -> None:
    assert parse_line("Send_Seller_Msg_Failure " + _business_line(), "inside") == []
    failure = {"event": "Send_Seller_Msg_Failure", "data": {"originData": "{}"}}
    assert parse_line(json.dumps(failure), "inside") == []


def test_failure_text_inside_user_content_is_not_protocol_failure() -> None:
    payload = {
        "from": {"role": "user", "uid": "fictional-buyer"},
        "content": "Send_Seller_Msg_Failure",
        "messageId": "fictional-user-failure-text",
        "account": "cs_123:456",
        "ts": 1787000002,
    }
    rows = parse_line("buyer_msg=" + json.dumps(payload, ensure_ascii=False), "logrus")
    assert len(rows) == 1
    assert rows[0]["role"] == "user"
    assert rows[0]["content"] == "Send_Seller_Msg_Failure"


def test_stale_business_and_origin_data_callbacks_are_ignored() -> None:
    stale = _business_line(timestamp="1787000001000")
    assert parse_line(stale, "inside") == []
    origin = {
        "SendContent": "陈旧客服消息",
        "BuyId": "900000000000000000001",
        "SellerId": "cs_123_456",
        "TimeStamp": "1787000001000",
    }
    stale_origin = (
        "2026-08-18 22:00:00.123 [inside] "
        + '"originData":'
        + json.dumps(json.dumps(origin, ensure_ascii=False), ensure_ascii=False)
    )
    assert parse_line(stale_origin, "inside") == []


def test_business_shape_inside_buyer_content_never_becomes_mall_cs() -> None:
    fake_business = {
        "SendContent": "伪造客服消息",
        "BuyId": "900000000000000000001",
        "SellerId": "cs_123_456",
        "TimeStamp": "1787061600123",
    }
    payload = {
        "from": {"role": "user", "uid": "fictional-buyer"},
        "content": json.dumps(fake_business, ensure_ascii=False),
        "messageId": "fictional-user-business-shape",
        "account": "cs_123:456",
        "ts": 1787061600,
    }
    rows = parse_line("buyer_msg=" + json.dumps(payload, ensure_ascii=False), "logrus")
    assert len(rows) == 1
    assert rows[0]["role"] == "user"


def test_false_multiline_candidate_does_not_swallow_next_complete_messages() -> None:
    emitted: list[dict] = []
    watcher = LogWatcher(".", emitted.append, platform="pdd", attach_specs=[])
    watcher._emit_line("business_message {", "inside")
    user = {
        "from": {"role": "user", "uid": "fictional-next-user"},
        "content": "正常用户消息",
        "messageId": "fictional-next-user-message",
        "account": "cs_123:456",
        "ts": 1787061601,
    }
    watcher._emit_line("buyer_msg=" + json.dumps(user, ensure_ascii=False), "inside")
    watcher._emit_line("originData:", "inside")
    watcher._emit_line(_business_line(message_id="fictional-next-seller-message"), "inside")
    watcher._flush_pending(force=True)
    assert [row["role"] for row in emitted] == ["user", "mall_cs"]
    assert "inside" not in watcher._multiline_candidates


def test_real_failure_clears_multiline_candidate() -> None:
    watcher = LogWatcher(".", lambda _row: None, platform="pdd", attach_specs=[])
    watcher._emit_line("business_message {", "inside")
    assert "inside" in watcher._multiline_candidates
    watcher._emit_line("Send_Seller_Msg_Failure result=-1", "inside")
    assert "inside" not in watcher._multiline_candidates


def test_existing_user_message_path_still_parses() -> None:
    payload = {
        "from": {"role": "user", "uid": "fictional-buyer"},
        "content": "虚构用户消息",
        "messageId": "fictional-user-message",
        "account": "cs_123:456",
        "ts": 1787000002,
    }
    row = parse_line("buyer_msg=" + json.dumps(payload, ensure_ascii=False), "logrus")[0]
    assert row["role"] == "user"
    assert row["buyer_id"] == "fictional-buyer"
    assert row["content"] == "虚构用户消息"


def test_business_message_enters_local_and_center_queues(tmp_path: Path, monkeypatch) -> None:
    install_local_first()
    monkeypatch.setattr("run_pdd_client.LocalDeliveryQueue._run", lambda self: None)
    monkeypatch.setattr("bridge.agent.BridgeAgent._load_local_queue", lambda self: None)
    cfg = {
        "platform": "pdd",
        "server_url": "http://127.0.0.1:1",
        "agent_token": "fictional-token",
        "agent_id": "fictional-agent",
        "device_id": "fictional-device",
        "tanyu_log_dir": str(tmp_path),
        "local_queue_path": str(tmp_path / "center.jsonl"),
        "command_journal_path": str(tmp_path / "commands.json"),
        "local_workbench_url": "http://127.0.0.1:1",
        "dual_write_local_workbench": True,
        "parser_profile_cache_path": str(tmp_path / "profile.json"),
    }
    agent = BridgeAgent(cfg)
    try:
        row = parse_line(_business_line(), "inside")[0]
        agent._on_local_event(row)
        event_id = row["idempotency_key"]
        assert event_id in agent._local_delivery.pending_ids()
        assert any(item["event_id"] == event_id for item in agent._pending)
        local_event = agent._local_delivery.pending[event_id]
        center_event = next(item for item in agent._pending if item["event_id"] == event_id)
        assert local_event["role"] == center_event["role"] == "mall_cs"
        agent.platform.channel_status = lambda _cfg: {}
        heartbeat = agent._status_payload()
        assert heartbeat["version"] == "0.7.0.2"
        assert heartbeat["build_hash"] == BUILD_HASH
        assert heartbeat["parser_profile"]["version"] == "pdd-imws-v2-business-message"
    finally:
        agent._stop.set()
        agent._local_delivery.wakeup.set()
        agent._local_delivery.thread.join(timeout=1)


def test_v0515_observability_and_register_version(monkeypatch) -> None:
    assert VERSION == "0.7.0.2"
    assert len(BUILD_HASH) == 16
    assert BUILTIN_PARSER_PROFILE["version"] == "pdd-imws-v2-business-message"
    captured = {}

    def request(self, method, path, body=None, **kwargs):
        captured.update(body or {})
        return {"ok": True}

    monkeypatch.setattr(BridgeClient, "_request", request)
    BridgeClient("http://127.0.0.1", "fictional-token", "fictional-agent").register()
    assert captured["version"] == "0.7.0.2"


def test_agent_and_gateway_versions_stay_in_lockstep() -> None:
    """run_pdd_client._gateway_status_matches 要求 gateway_version == VERSION 严格相等，
    只升一边会让 agent 把网关反复判为不合格、浮窗永远起不来。"""
    from run_frontend_service import LOCAL_GATEWAY_VERSION

    assert LOCAL_GATEWAY_VERSION == VERSION
