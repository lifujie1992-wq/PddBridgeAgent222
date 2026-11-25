"""DLL 直发返回 OK 0 时必须回退页面发送（90科技 2026-11-25：R45KXY7 上 17 次 OK 0 一条没发出）。"""
import time
from types import SimpleNamespace

from bridge.pdd_send_direct import DirectSender
from bridge.pddbridge_source import PddbridgeSource, _SendWaiter, _Session, LISTENING


def _source(dll_reply, *, page_csid="客服小宝"):
    source = PddbridgeSource({})
    source.state = LISTENING
    calls = []

    def evaluate(expression, cid, **_kwargs):
        calls.append(expression)
        if "__pddBridge_info" in expression:
            return {"globalMallId": "123"}
        if "userInfo" in expression and "__pddBridge_sendText" not in expression:
            return page_csid
        if "__pddBridge_sendText" in expression:
            return {"ok": True, "accepted": True, "csid": page_csid}
        return None

    session = _Session(57165, "u", SimpleNamespace(eval=evaluate), 1)
    source.sessions = [session]
    sender = DirectSender("\\\\.\\pipe\\test")
    lines = []
    sender._exchange = lambda line: lines.append(line) or dll_reply  # type: ignore[assignment]
    source.direct_send = sender
    waiter = _SendWaiter()
    waiter.deadline = time.monotonic() + 5
    return source, session, waiter, calls, lines


def _page_send_calls(calls):
    return [c for c in calls if "__pddBridge_sendText(" in c]


def test_ok0_falls_back_to_page_send():
    source, session, waiter, calls, lines = _source("OK 0")
    source._do_send(session, "3427769341", "你好", "cs_123:456", waiter, 5, None)
    assert len(lines) == 1
    assert _page_send_calls(calls), "OK 0 后必须走页面发送"
    # 页面发送已受理、等回执：不能被 DLL 的失败结果提前定案
    assert waiter.result is None
    assert source.direct_fallback_count == 1
    assert source.last_direct_fallback["reply"] == "OK 0"
    assert source.status()["dll_send"]["fallback_to_page"] == 1


def test_missing_csid_is_filled_from_page_before_dll():
    source, session, waiter, calls, lines = _source("OK 1", page_csid="客服小宝")
    source._do_send(session, "3427769341", "你好", "cs_123:456", waiter, 5, None)
    assert lines == ["TEXT|客服小宝|3427769341|你好"]
    assert waiter.result["ok"] is True
    assert waiter.result["via"] == "dll_direct"
    assert not _page_send_calls(calls), "DLL 成功就不能再走页面，避免重复发送"


def test_explicit_csid_is_kept():
    source, session, waiter, calls, lines = _source("OK 1", page_csid="页面身份")
    source._do_send(session, "u1", "hi", "cs_123:456", waiter, 5, "指定身份")
    assert lines == ["TEXT|指定身份|u1|hi"]
    assert not any("userInfo" in c and "__pddBridge_sendText" not in c for c in calls)


def test_other_dll_failure_does_not_fall_back():
    # 只有明确的 OK 0（同步返回未发送）才回退；未知回复保守判失败，防止重复发送
    source, session, waiter, calls, lines = _source("ERR busy")
    source._do_send(session, "u1", "hi", "cs_123:456", waiter, 5, None)
    assert waiter.result["ok"] is False
    assert waiter.result["via"] == "dll_direct"
    assert not _page_send_calls(calls)
    assert source.direct_fallback_count == 0


def test_page_csid_unavailable_still_tries_dll_then_falls_back():
    source, session, waiter, calls, lines = _source("OK 0", page_csid=None)
    source._do_send(session, "u1", "hi", "cs_123:456", waiter, 5, None)
    assert lines == ["TEXT||u1|hi"]
    assert _page_send_calls(calls)
