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


def test_local_first_path_publishes_via_local_delivery():
    """正式运行路径（install_local_first）里没有 _local_ingest_queue。

    0.8.1 现场：整场日志 0 条 "self reply published locally"，倒计时照旧不停 ——
    因为修好的代码只看 _local_ingest_queue，而它在这个模式下恒为 None。
    """
    published = []

    class Delivery:
        def enqueue(self, event, **kwargs):
            published.append(event)

    agent = SimpleNamespace(
        cfg={},
        platform=SimpleNamespace(name="pdd"),
        _local_ingest_queue=None,
        _local_delivery=Delivery(),
    )
    BridgeAgent._publish_self_reply(
        agent, "cs_1:2", "buyer", "已回复",
        {"ok": True, "status": "confirmed", "real_send": True})
    assert len(published) == 1, "local_first 模式下必须走 _local_delivery"
    assert published[0]["role"] == "mall_cs"
    assert published[0]["delivery_status"] == "confirmed"
    assert _state([_u(1000), published[0]])["waiting_for_reply"] is False


def test_local_first_path_skips_unconfirmed_send():
    """没确认发出（indeterminate）不能写成本地已回复，否则倒计时会误停。"""
    published = []

    class Delivery:
        def enqueue(self, event, **kwargs):
            published.append(event)

    agent = SimpleNamespace(
        cfg={}, platform=SimpleNamespace(name="pdd"),
        _local_ingest_queue=None, _local_delivery=Delivery())
    BridgeAgent._publish_self_reply(
        agent, "cs_1:2", "buyer", "已回复",
        {"ok": False, "status": "failed", "real_send": False})
    assert published == [], "明确失败的不写本地会话"
