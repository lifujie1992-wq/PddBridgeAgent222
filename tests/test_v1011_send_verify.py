# -*- coding: utf-8 -*-
"""1.0.11：「页面已受理」不再直接算已发出，等卖家侧回显 / 客户端拒绝来定结果。"""
import threading
import time
import types

from bridge.agent import BridgeAgent

ACCEPTED = {"ok": True, "status": "accepted", "real_send": True, "delivery_uncertain": True,
            "echo_missing": True, "socket_state": "none", "via": "cdp_accepted"}


class Delivery:
    def __init__(self):
        self.events = []

    def enqueue(self, event, wake=True):
        self.events.append(dict(event))


def _agent():
    agent = object.__new__(BridgeAgent)
    agent.cfg = {"dual_write_local_workbench": True, "send_verify_seconds": 10, "agent_id": "a"}
    agent.platform = types.SimpleNamespace(name="pdd")
    agent._local_delivery = Delivery()
    agent._local_ingest_queue = None
    agent._stop = threading.Event()
    return agent


def _statuses(agent):
    return [e["delivery_status"] for e in agent._local_delivery.events]


def test_accepted_send_waits_then_echo_confirms():
    agent = _agent()
    agent._publish_self_reply("cs_1:2", "b1", "亲，我家都是2年质保的哦", ACCEPTED)
    assert _statuses(agent) == ["accepted"]          # 不再直接算「已确认发出」
    agent._match_send_echo({"role": "mall_cs", "buyer_id": "b1", "content": "亲，我家都是2年质保的哦"})
    assert _statuses(agent) == ["accepted", "confirmed"]
    events = agent._local_delivery.events
    assert events[0]["event_id"] == events[1]["event_id"]   # 同一条记录原地更新
    assert agent._diag_sends()["states"] == {"confirmed_echo": 1}


def test_no_echo_marks_unconfirmed_after_limit():
    agent = _agent()
    agent._publish_self_reply("cs_1:2", "b1", "在的亲", ACCEPTED)
    agent._send_records[0]["sent_at"] -= 11
    agent._stop.set()                                # 只跑一轮检查
    agent._stop = types.SimpleNamespace(wait=_once())
    agent._send_verify_loop()
    assert _statuses(agent) == ["accepted", "indeterminate"]
    assert "可能没发出" in agent._local_delivery.events[-1]["error"]


def _once():
    calls = {"n": 0}

    def wait(_timeout):
        calls["n"] += 1
        return calls["n"] > 1
    return wait


def test_late_native_rejection_marks_failed():
    agent = _agent()
    agent._publish_self_reply("cs_1:2", "b1", "三个名额先到先得", ACCEPTED)
    agent._on_late_send_error("b1", "三个名额先到先得", "content blocked")
    assert _statuses(agent) == ["accepted", "failed"]
    assert agent._diag_sends()["errors"] == {"content blocked": 1}


def test_echo_for_other_buyer_or_text_does_not_confirm():
    agent = _agent()
    agent._publish_self_reply("cs_1:2", "b1", "亲，我家都是2年质保的哦", ACCEPTED)
    agent._match_send_echo({"role": "mall_cs", "buyer_id": "b2", "content": "亲，我家都是2年质保的哦"})
    agent._match_send_echo({"role": "mall_cs", "buyer_id": "b1", "content": "好的收到"})
    assert _statuses(agent) == ["accepted"]


def test_two_identical_replies_each_need_their_own_echo():
    agent = _agent()
    agent._publish_self_reply("cs_1:2", "b1", "亲，我在的", ACCEPTED)
    agent._publish_self_reply("cs_1:2", "b1", "亲，我在的", ACCEPTED)
    agent._match_send_echo({"role": "mall_cs", "buyer_id": "b1", "content": "亲，我在的"})
    states = [r["state"] for r in agent._send_records]
    assert states == ["confirmed_echo", "pending"]


def test_confirmed_receipt_is_not_tracked_as_pending():
    agent = _agent()
    agent._publish_self_reply("cs_1:2", "b1", "好的", {"ok": True, "status": "confirmed", "via": "cdp+ws"})
    assert _statuses(agent) == ["confirmed"]
    assert agent._send_records[0]["state"] == "confirmed"


def test_source_forwards_late_send_error():
    from bridge.pddbridge_source import PddbridgeSource
    got = []
    src = PddbridgeSource({}, on_event=lambda m: None)
    src.on_send_error = lambda *a: got.append(a)
    src._handle_frame({"dir": "send_error", "uid": "b1", "content": "hi", "error": "blocked"})
    assert got and got[0][:3] == ("b1", "hi", "blocked")
