# -*- coding: utf-8 -*-
"""回复倒计时必须在"已回复"后停止（现场：AI 回复了、界面还在走 90s 倒计时）。"""
from __future__ import annotations

from queue import Queue
from types import SimpleNamespace

from bridge.agent import BridgeAgent
from run_frontend_service import LocalSeatState as Store


def _state(messages):
    store = object.__new__(Store)
    return Store._reply_wait_state(store, messages)


def _u(ts, role="user", delivery="", mid="1"):
    return {"role": role, "ts": ts, "_sequence": 0, "msg_id": mid,
            "delivery_status": delivery, "source": "pdd_native"}


def test_ai_reply_stops_countdown():
    """assistant_simulated（智能体气泡）也算回复 —— 旧实现把它排除，倒计时永远不停。"""
    msgs = [_u(1000), _u(1010, role="assistant_simulated", mid="2")]
    assert _state(msgs)["waiting_for_reply"] is False


def test_platform_echo_stops_countdown():
    msgs = [_u(1000), _u(1010, role="mall_cs", delivery="accepted", mid="2")]
    assert _state(msgs)["waiting_for_reply"] is False
    msgs = [_u(1000), _u(1010, role="mall_cs", delivery="confirmed", mid="2")]
    assert _state(msgs)["waiting_for_reply"] is False


def test_failed_send_keeps_waiting():
    msgs = [_u(1000), _u(1010, role="mall_cs", delivery="failed", mid="2")]
    assert _state(msgs)["waiting_for_reply"] is True
    msgs = [_u(1000), _u(1010, role="mall_cs", delivery="indeterminate", mid="2")]
    assert _state(msgs)["waiting_for_reply"] is True


def test_buyer_message_after_reply_restarts_countdown():
    msgs = [_u(1000), _u(1010, role="assistant_simulated", mid="2"), _u(1020, mid="3")]
    state = _state(msgs)
    assert state["waiting_for_reply"] is True
    assert state["waiting_since"] == 1020


def test_bridge_reply_requires_real_send_to_stop_countdown():
    agent = SimpleNamespace(
        cfg={},
        platform=SimpleNamespace(name="pdd"),
        _local_ingest_queue=Queue(),
    )
    publish = BridgeAgent._publish_self_reply
    publish(agent, "shop", "buyer", "reply",
            {"ok": True, "status": "accepted", "via": "dry_run", "real_send": False})
    assert agent._local_ingest_queue.empty()

    for real_send in (False, True):
        publish(agent, "shop", "buyer", "reply",
                {"ok": True, "status": "accepted", "real_send": real_send})
        event = agent._local_ingest_queue.get_nowait()
        assert _state([_u(1000), event])["waiting_for_reply"] is (not real_send)
