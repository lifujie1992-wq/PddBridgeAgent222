# -*- coding: utf-8 -*-
"""Regression tests for the v0.5.15 send_text false-sent fix.

Customer symptom: workbench showed 已发出 but the buyer never received the
message, because send_text reported 'accepted' on a bare DLL HTTP 200 (the DLL
can silently no-op) and never passed the buyer nick.
"""
from __future__ import annotations

import inspect

from bridge import channel


def test_send_payload_uses_buyer_nick():
    src = inspect.getsource(channel.send_text)
    assert '"nick": nick' in src, "send payload must carry imServerUser.nick"
    assert "buyer_nick" in channel.send_text.__kwdefaults__


def test_send_waits_for_log_receipt():
    src = inspect.getsource(channel.send_text)
    assert "_pdd_wait_for_send_confirmation" in src, "must confirm via Tanyu log receipt"


def test_parse_send_confirmation_positive_and_negative():
    line = (
        "2026-08-25 12:00:00 INFO [Plugin] Send_Seller_Msg_Success buyer_id:123456 "
        "cs_id:cs_100:200 utf8_msg:您好，库存充足 msg_type:1 text_or_picture:text"
    )
    ok = channel._pdd_parse_send_confirmation(
        line, account="cs_100:200", buyer_id="123456", content="您好，库存充足"
    )
    assert ok["verified"]

    other_buyer = channel._pdd_parse_send_confirmation(
        line, account="cs_100:200", buyer_id="999", content="您好"
    )
    assert not other_buyer["verified"] and other_buyer["seen"]

    other_content = channel._pdd_parse_send_confirmation(
        line, account="cs_100:200", buyer_id="123456", content="完全不同的话术"
    )
    assert not other_content["verified"]


def test_parse_send_confirmation_accepts_native_inside_log():
    """探域 inside 日志的真实格式（userId = N message = ...）——插件日志已停写。"""
    line = (
        "2026-09-18 23:24:28.407 29812 -[inside]- DEBUG: "
        "[CMsgInterfaceWorkbench_pdd::handleMsg_sendMsg] send text message  success ,  "
        "userId = 6028043705165 message = 【新疆，西藏，云南及港澳台海外地区】限制无法使用，谢谢~"
    )
    ok = channel._pdd_parse_send_confirmation(
        line, account="cs_100000004:200000011", buyer_id="6028043705165",
        content="【新疆，西藏，云南及港澳台海外地区】限制无法使用，谢谢~")
    assert ok["verified"] and ok.get("source") == "inside"

    bad = channel._pdd_parse_send_confirmation(
        line, account="cs_100000004:200000011", buyer_id="999", content="谢谢~")
    assert not bad["verified"] and bad["seen"]


def test_unverified_result_is_indeterminate_not_accepted():
    # Simulate the no-receipt branch by checking result construction logic.
    src = inspect.getsource(channel.send_text)
    assert '"indeterminate"' in src
    assert '"real_send": True' in src  # only set on verified receipt
