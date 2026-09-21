# -*- coding: utf-8 -*-
"""智能体 / 人工 标记。

背景：探域客户端日志里，AI（中心指令发出）和人工手打的回复长得一模一样，
只凭 `role=mall_cs` 分不出来，之前浮窗把人工回复也显示成「智能体」。
现在桥接在执行中心 send_text 指令时登记一条“自己发出去的”指纹，
本地工作台据此把消息标成 sent_by=agent / human。
"""
import time

from bridge.agent import BridgeAgent, _same_seat, _same_sent_text, _sent_text_key


class _Stub:
    """只借 BridgeAgent 的三个纯方法，避免起整个 agent。"""

    cfg = {}
    _self_send_buffer = BridgeAgent._self_send_buffer
    _note_self_send = BridgeAgent._note_self_send
    _classify_sent_by = BridgeAgent._classify_sent_by


def test_sent_text_key_keeps_only_cjk_and_alnum():
    assert _sent_text_key("✔️【联想】30天 单月\n超值流量套餐") == "联想30天单月超值流量套餐"
    assert _sent_text_key("??【联想】30天") == "联想30天"
    assert _sent_text_key("") == ""


def test_same_sent_text_tolerates_emoji_mojibake_and_truncation():
    ours = "✔️【联想】30天单月超值流量套餐\n🍉100G/1个月→29元\n🍍300G/1个月→39元"
    # 探域日志把 emoji 变成 ??，而且只留了第一行
    assert _same_sent_text(ours, "??【联想】30天单月超值流量套餐")
    # 完全不同的话术不算同一条
    assert not _same_sent_text(ours, "亲亲，这边帮您查一下物流哦")
    assert not _same_sent_text("", "任意内容")


def test_same_seat_tolerates_format_differences():
    assert _same_seat("cs_100000002:200000010", "cs_100000002:200000010")
    assert _same_seat("cs_100000002:200000010", "mall_100000002")
    assert _same_seat("", "cs_1:2")            # 缺一边不拦
    assert not _same_seat("cs_100000002:200000010", "cs_100000001:200000002")


def test_classify_marks_agent_for_self_sent_and_human_for_the_rest():
    stub = _Stub()
    stub._note_self_send("cs_100000001:200000002", "3000000000002",
                         "✔️【联想】30天单月超值流量套餐\n🍉100G/1个月→29元")
    base = {"role": "mall_cs", "account": "cs_100000001:200000002", "buyer_id": "3000000000002"}
    # 自己发的（哪怕日志乱码/截断）= 智能体
    assert stub._classify_sent_by({**base, "content": "??【联想】30天单月超值流量套餐"}) == "agent"
    # 同会话里另一句话 = 人工
    assert stub._classify_sent_by({**base, "content": "亲亲，这边帮您转接售后"}) == "human"
    # 别的买家不能被这次发送影响
    assert stub._classify_sent_by({**base, "buyer_id": "123456", "content": "【联想】30天单月超值流量套餐"}) == "human"
    # 买家自己的消息不参与判定
    assert stub._classify_sent_by({**base, "role": "user", "content": "在吗"}) == ""
    # 历史补拉帧：没匹配上的不猜（返回空，前端退回旧启发式）；匹配上的仍算自己发的
    assert stub._classify_sent_by({**base, "content": "很久以前谁发的都不知道", "is_history": True}) == ""


class _AgentStub:
    class platform:  # noqa: N801 - 只是想模仿 agent.platform.name
        name = "pdd"

    cfg = {"agent_id": "agent-test"}
    _ensure_event_id = staticmethod(BridgeAgent._ensure_event_id)
    _note_self_send = BridgeAgent._note_self_send
    _classify_sent_by = BridgeAgent._classify_sent_by
    _self_send_buffer = BridgeAgent._self_send_buffer


def test_event_from_message_carries_sent_by():
    """本地工作台那条推送路径（_event_from_message）也必须带 sent_by。

    浮窗读的是本地 gateway 的会话状态，而它是靠 run_pdd_client 里的
    _event_from_message 构造事件的；漏了这个字段，浮窗的「智能体/人工」就永远错乱。
    """
    from run_pdd_client import _event_from_message

    agent = _AgentStub()
    agent._note_self_send("cs_1:2", "b1", "在的哦亲亲，马上帮您处理")
    now = time.time()

    mine = _event_from_message(agent, {
        "role": "mall_cs", "msg_id": "m-agent", "buyer_id": "b1", "account": "cs_1:2",
        "content": "在的哦亲亲，马上帮您处理", "nickname": "b1", "source": "inside", "ts": now,
    })
    assert mine.get("sent_by") == "agent"

    manual = _event_from_message(agent, {
        "role": "mall_cs", "msg_id": "m-human", "buyer_id": "b1", "account": "cs_1:2",
        "content": "我这边人工回复一句", "nickname": "b1", "source": "inside", "ts": now,
    })
    assert manual.get("sent_by") == "human"

    incoming = _event_from_message(agent, {
        "role": "user", "msg_id": "m-user", "buyer_id": "b1", "account": "cs_1:2",
        "content": "在吗", "nickname": "b1", "source": "inside", "ts": now,
    })
    assert incoming.get("sent_by") == ""

    # 历史补拉帧不猜（stamp_message 会把无时间戳的帧标成 history）
    historical = _event_from_message(agent, {
        "role": "mall_cs", "msg_id": "m-hist", "buyer_id": "b1", "account": "cs_1:2",
        "content": "很久以前谁发的都不知道", "nickname": "b1", "source": "inside",
    })
    assert historical.get("sent_by") == ""


def test_classify_respects_window(monkeypatch):
    stub = _Stub()
    stub.cfg = {"self_send_match_seconds": 60}
    stub._note_self_send("cs_1:2", "b1", "在的哦亲亲")
    stub._self_sends[0]["ts"] -= 3600   # 一小时前的发送不该再认
    assert stub._classify_sent_by({"role": "mall_cs", "account": "cs_1:2", "buyer_id": "b1",
                                   "content": "在的哦亲亲"}) == "human"
    stub._self_sends[0]["ts"] += 3600
    assert stub._classify_sent_by({"role": "mall_cs", "account": "cs_1:2", "buyer_id": "b1",
                                   "content": "在的哦亲亲"}) == "agent"
