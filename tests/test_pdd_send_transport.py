from types import SimpleNamespace

from bridge import channel, pddbridge_source
from bridge.agent import _command_result_for_ack
from bridge.platforms.pdd import PddPlatform


def test_indeterminate_send_is_not_acknowledged_as_success():
    result = _command_result_for_ack({"ok": True, "status": "indeterminate", "real_send": False})

    assert result["ok"] is False
    assert result["delivery_uncertain"] is True
    assert result["retryable"] is False


def test_cdp_intake_uses_dll_sender_when_tanyu_is_available(monkeypatch):
    sent = {}

    def fake_send_text(buyer_id, content, account, **kwargs):
        sent.update(buyer_id=buyer_id, content=content, account=account, **kwargs)
        return {"ok": True, "status": "confirmed", "real_send": True, "via": "dll+log"}

    monkeypatch.setattr(channel, "channel_status", lambda **kwargs: {"dll_ready": True})
    monkeypatch.setattr(pddbridge_source, "channel_status_cdp",
                        lambda cfg: {"receive_ready": True, "send_ready": False})
    monkeypatch.setattr(channel, "send_text", fake_send_text)
    result = PddPlatform().send_text(
        "buyer-1", "hello", "cs_1:2",
        cfg={"_effective_source": "cdp", "tanyu_log_dir": "D:/tanyu/logs"},
    )

    assert result["via"] == "dll+log"
    assert sent["buyer_id"] == "buyer-1"
    assert sent["tanyu_log_dir"] == "D:/tanyu/logs"


def test_page_receive_readiness_does_not_allow_a_send():
    def unexpected_send(*args, **kwargs):
        raise AssertionError("send was dispatched before a channel was ready")

    source = SimpleNamespace(
        status=lambda: {"state": "listening", "send_ready": False},
        send_and_wait=unexpected_send,
    )
    result = pddbridge_source.send_text_cdp(
        "buyer-1", "hello", "cs_1:2", cfg={"_pddbridge_source": source},
    )

    assert result["error"] == "send_channel_not_ready"
    assert result["real_send"] is False


# ---------------------------------------------------------------- 回执匹配
def _bare_source():
    return pddbridge_source.PddbridgeSource({"tanyu_log_dir": ""}, on_event=lambda e: None)


def _register(source, account, content, uid="buyer-1"):
    import threading
    event = threading.Event()
    event.result = None
    source._pending_sends[(account, uid, content)] = {
        "uid": uid, "content": content, "event": event,
        "deadline": __import__("time").time() + 5, "account": account,
    }
    return event


def _receipt(content, msg_id="123", mall_id="1"):
    return {"msg_id": msg_id, "content": content,
            "obj": {"message": {"to": {"role": "mall_cs", "mall_id": mall_id}}}}


def test_receipt_matches_when_command_carried_no_account():
    """命令没带 account 时也必须能配上回执。

    0.8 把待确认表的 key 改成 (account, uid, content) 后，回执解析出的 account
    非空、命令那边为空 → 整条配不上，已发出的消息被报成 indeterminate。
    """
    source = _bare_source()
    event = _register(source, "", "hello")
    source._handle_send_receipt(_receipt("hello"), account_hint="cs_1:2")
    assert event.result is not None, "空 account 的待确认项必须靠内容兜底命中"
    assert event.result["status"] == "confirmed"
    assert not source._pending_sends, "命中后必须从待确认表里删掉"


def test_receipt_prefers_exact_account_when_content_is_ambiguous():
    """同一句话发给两个店：account 对得上的那条赢，不能瞎配。"""
    source = _bare_source()
    first = _register(source, "cs_1:2", "hello", uid="b-1")
    second = _register(source, "cs_3:4", "hello", uid="b-2")
    source._handle_send_receipt(_receipt("hello", mall_id="3"), account_hint="cs_3:4")
    assert second.result and second.result["status"] == "confirmed"
    assert first.result is None, "另一家店的同名待确认项不能被误确认"


def test_send_error_releases_pending_without_account():
    """send_error 帧同样要能靠内容兜底释放（否则发送线程还会干等到超时）。"""
    source = _bare_source()
    event = _register(source, "", "hello")
    source._handle_frame_locked({"dir": "send_error", "uid": "buyer-1",
                                 "content": "hello", "error": "rejected"})
    assert event.result and event.result["status"] == "failed"
    assert not source._pending_sends
