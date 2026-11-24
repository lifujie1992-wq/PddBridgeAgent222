"""催付闸门：口径是「没买就催，买了绝不催」。

背景：大脑已经有催付，但分不清买家下没下单，出现「人家已经付款还催付」。
这不是让 AI 更会猜，而是在发送前用工作台自己的订单接口做一次确定性判定
（`/latitude/order/userAllOrder`，见 bridge/pdd_context.py）。
"""
from __future__ import annotations

import time

from bridge.pdd_context import (order_gate_decision, order_verdict)
from bridge.agent import BridgeAgent

NOW = 1_800_000_000.0


def _result(orders):
    return {"ok": True, "orders": orders}


def _order(order_id, pay_status=0, status=None, age=120):
    row = {"order_id": order_id, "created_at": NOW - age, "pay_status": pay_status}
    if status is not None:
        row["order_status"] = status
    return row


def test_paid_blocks():
    """已付款 → 绝不催（这就是「人家买了还催付」的根因）。"""
    for pay in (1, 2, "1", "2", True, "paid"):
        verdict = order_verdict(_result([_order("A", pay_status=pay)]), now=NOW)
        assert verdict["state"] == "paid" and verdict["bought"] is True, pay
        allowed, reason = order_gate_decision(verdict, "not_paid")
        assert (allowed, reason) == (False, "already_paid")


def test_unpaid_and_closed_are_allowed():
    """待付款、付款前关闭 → 都算「没买」，都要催。"""
    unpaid = order_verdict(_result([_order("B", pay_status=0)]), now=NOW)
    closed = order_verdict(_result([_order("C", pay_status=0, status=2)]), now=NOW)
    assert unpaid["state"] == "unpaid" and closed["state"] == "closed"
    assert order_gate_decision(unpaid, "not_paid")[0] is True
    assert order_gate_decision(closed, "not_paid")[0] is True


def test_no_order_is_allowed():
    """压根没下单 → 催。"""
    verdict = order_verdict(_result([]), now=NOW)
    assert verdict["state"] == "no_order" and verdict["allowed"] is True
    assert order_gate_decision(verdict, "not_paid")[0] is True


def test_lookup_failure_is_fail_closed_by_default():
    """查不到就默认不催（宁可少催，不可乱催）；配置可放开。"""
    verdict = order_verdict({"ok": False, "reason": "panel_not_ready"}, now=NOW)
    assert verdict["state"] == "unknown"
    assert order_gate_decision(verdict, "not_paid", fail_open=False) == (False, "order_unknown_blocked")
    assert order_gate_decision(verdict, "not_paid", fail_open=True) == (True, "order_unknown")


def test_mixed_orders_latest_paid_wins():
    """同一买家多单：有一单已付就算买了（宁可少催）。"""
    verdict = order_verdict(_result([
        _order("old_unpaid", pay_status=0, age=600),
        _order("new_paid", pay_status=1, age=30),
    ]), now=NOW)
    assert verdict["state"] == "paid"
    assert order_gate_decision(verdict, "not_paid")[0] is False


def test_window_excludes_stale_history():
    """窗口外的历史已付订单不算「这次买了」——窗口可配。"""
    stale = _result([_order("E", pay_status=1, age=86400 * 3)])
    assert order_verdict(stale, now=NOW, window_seconds=86400)["state"] == "no_order"
    # 放宽到 7 天就又能看见，仍然拦
    assert order_verdict(stale, now=NOW, window_seconds=86400 * 7)["state"] == "paid"


def test_mode_off_and_legacy_paid_only():
    unpaid = order_verdict(_result([_order("F", pay_status=0)]), now=NOW)
    assert order_gate_decision(unpaid, "")[0] is True              # 关闭
    assert order_gate_decision(unpaid, "paid_only")[0] is True      # 旧口径：只放待付款
    no_order = order_verdict(_result([]), now=NOW)
    assert order_gate_decision(no_order, "paid_only")[0] is False    # 旧口径不催无单


def test_gate_returns_blocked_result_without_sending():
    """闸门拦截返回结构化结果（status=blocked + 原因），不会真发。"""
    class Lookup:
        def __init__(self, verdict):
            self.verdict = verdict

        def lookup(self, payload):
            return {"ok": True, "orders": [_order("G", pay_status=1)]}

    class Stub:
        cfg = {"order_gate_state": "not_paid"}
        _pdd_order_lookup = None

        def _order_lookup(self):
            return Lookup(None)

        log = type("L", (), {"info": staticmethod(lambda *a, **k: None)})()

    gate = BridgeAgent._order_gate(Stub(), {}, "cs_1:100", "3000000000001")
    assert gate is not None
    assert gate["status"] == "blocked" and gate["real_send"] is False
    assert gate["via"] == "order_gate"
    assert gate["order_state"] == "paid"
    assert "已付款" in gate["error_user"]

    Stub.cfg = {}                      # 闸门关闭 → 放行
    Stub._order_lookup = lambda self: Lookup(None)
    assert BridgeAgent._order_gate(Stub(), {}, "cs_1:100", "1") is None
