# -*- coding: utf-8 -*-
"""1.0.14：发送后到拼多多服务器聊天记录核对，缺了重发一次，仍缺才报失败。"""
import time

import pytest

from bridge.pddbridge_source import PddbridgeSource

ACCEPTED = {"ok": True, "status": "accepted", "real_send": True, "delivery_uncertain": True,
            "echo_missing": True, "socket_state": "none", "via": "cdp_accepted"}


def _source(monkeypatch, send_results, verdicts, cfg=None):
    src = PddbridgeSource(cfg or {}, on_event=lambda m: None)
    sends, checks = [], []
    send_iter, verdict_iter = iter(send_results), iter(verdicts)

    def send_once(uid, content, account, **_kw):
        sends.append((uid, content))
        return dict(next(send_iter))

    def verify(uid, content, account, since_ts, timeout=8.0):
        checks.append(uid)
        return dict(next(verdict_iter))

    monkeypatch.setattr(src, "_send_once", send_once)
    monkeypatch.setattr(src, "verify_on_server", verify)
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    return src, sends, checks


def test_found_on_server_is_confirmed_without_resend(monkeypatch):
    src, sends, _ = _source(monkeypatch, [ACCEPTED], [{"state": "found", "msg_id": "m1"}])
    r = src.send_and_wait("b1", "你好", "cs_1:2")
    assert r["status"] == "confirmed" and r["via"] == "cdp+server" and r["msg_id"] == "m1"
    assert len(sends) == 1 and r["send_attempts"] == 1


def test_late_arrival_is_not_resent(monkeypatch):
    verdicts = [{"state": "missing"}, {"state": "missing"}, {"state": "found", "msg_id": "m2"}]
    src, sends, checks = _source(monkeypatch, [ACCEPTED], verdicts)
    r = src.send_and_wait("b1", "你好", "cs_1:2")
    assert r["status"] == "confirmed" and len(sends) == 1 and len(checks) == 3


def test_missing_is_resent_once_then_confirmed(monkeypatch):
    verdicts = [{"state": "missing"}] * 4 + [{"state": "found", "msg_id": "m3"}]
    src, sends, _ = _source(monkeypatch, [ACCEPTED, ACCEPTED], verdicts)
    r = src.send_and_wait("b1", "你好", "cs_1:2")
    assert r["status"] == "confirmed" and r["send_attempts"] == 2 and len(sends) == 2
    assert src.send_verify_stats["resent"] == 1 and src.send_verify_stats["confirmed_after_resend"] == 1


def test_still_missing_after_resend_reports_failed(monkeypatch):
    src, sends, _ = _source(monkeypatch, [ACCEPTED, ACCEPTED], [{"state": "missing"}] * 8)
    r = src.send_and_wait("b1", "你好", "cs_1:2")
    assert r["ok"] is False and r["status"] == "failed" and r["error"] == "server_history_missing"
    assert len(sends) == 2 and src.send_verify_stats["failed"] == 1


def test_verify_unavailable_keeps_old_uncertain_result_and_never_resends(monkeypatch):
    src, sends, _ = _source(monkeypatch, [ACCEPTED], [{"state": "unknown", "error": "HTTP 403"}])
    r = src.send_and_wait("b1", "你好", "cs_1:2")
    assert r["status"] == "accepted" and r["echo_missing"] is True and len(sends) == 1


def test_expired_command_is_not_resent(monkeypatch):
    src, sends, _ = _source(monkeypatch, [ACCEPTED], [{"state": "missing"}] * 4)
    r = src.send_and_wait("b1", "你好", "cs_1:2", is_expired=lambda: True)
    assert r["status"] == "failed" and len(sends) == 1


def test_switch_off_restores_old_behaviour(monkeypatch):
    src, sends, checks = _source(monkeypatch, [ACCEPTED], [], cfg={"send_server_verify": False})
    r = src.send_and_wait("b1", "你好", "cs_1:2")
    assert r["status"] == "accepted" and checks == []


def test_confirmed_receipt_and_failures_skip_verification(monkeypatch):
    confirmed = {"ok": True, "status": "confirmed", "via": "cdp+ws"}
    src, _, checks = _source(monkeypatch, [confirmed], [])
    assert src.send_and_wait("b1", "你好", "cs_1:2")["via"] == "cdp+ws" and checks == []
    failed = {"ok": False, "status": "failed", "via": "cdp"}
    src, _, checks = _source(monkeypatch, [failed], [])
    assert src.send_and_wait("b1", "你好", "cs_1:2")["status"] == "failed" and checks == []


@pytest.mark.parametrize("row,expected", [
    ({"role": "mall_cs", "ts": "1000", "content": "亲，在的 哦～"}, True),
    ({"role": "mall_cs", "ts": 1000_000, "content": "亲，在的哦～"}, True),        # 毫秒时间戳
    ({"role": "user", "ts": "1000", "content": "亲，在的哦～"}, False),           # 买家说了同样的话不算
    ({"role": "mall_cs", "ts": "900", "content": "亲，在的哦～"}, False),          # 上一次发的同一句不算
    ({"role": "mall_cs", "ts": "1000", "content": "别的话"}, False),
])
def test_match_server_history(row, expected):
    assert bool(PddbridgeSource.match_server_history([row], "亲，在的哦～", 1000.0)) is expected


def test_verify_action_reads_server_history_through_the_session(monkeypatch):
    import types
    from bridge.pddbridge_source import _SendWaiter

    calls = []

    class FakeCdp:
        def eval(self, expr, cid, timeout=8, await_promise=False):
            calls.append((expr, await_promise))
            return {"ok": True, "messages": [{"role": "mall_cs", "ts": str(int(time.time())),
                                              "content": "亲，在的哦～", "msg_id": "m9"}]}

    src = PddbridgeSource({}, on_event=lambda m: None)
    session = types.SimpleNamespace(cdp=FakeCdp(), cid=7)
    monkeypatch.setattr(src, "_session_for_send", lambda _account: session)
    waiter = _SendWaiter()
    src._do_verify_action("b1", "亲，在的哦～", "cs_1:2", time.time(), waiter)
    assert waiter.result == {"state": "found", "msg_id": "m9"}
    assert calls[0][1] is True and "/plateau/chat/list" in calls[0][0] and '"b1"' in calls[0][0]

    waiter = _SendWaiter()
    src._do_verify_action("b1", "没发过的话", "cs_1:2", time.time(), waiter)
    assert waiter.result == {"state": "missing"}

    monkeypatch.setattr(src, "_session_for_send", lambda _account: None)
    waiter = _SendWaiter()
    src._do_verify_action("b1", "x", "cs_1:2", time.time(), waiter)
    assert waiter.result["state"] == "unknown"
