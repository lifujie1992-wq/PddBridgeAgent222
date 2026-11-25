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


def test_receipt_matches_when_page_account_is_placeholder():
    """页面会话还没解析出店铺时 account 是 "?"，不是“另一家店”。

    现场（0.8.2 验收）：工作台刚重启，session.account="?"，
    _account_for_frame 返回 "?" → 严格 key 与兜底都配不上 → 干等 5s 报
    indeterminate，但消息其实已经发出去了。
    """
    source = _bare_source()
    source.sessions = [SimpleNamespace(account="?", mall_id="1")]
    event = _register(source, "cs_1:2", "hello")
    source._handle_send_receipt(_receipt("hello", mall_id="1"), account_hint="?")
    assert event.result and event.result["status"] == "confirmed"
    assert not source._pending_sends


def test_placeholder_account_does_not_cross_match_two_shops():
    """"?" 不能把两家店的同名待确认项混在一起。"""
    source = _bare_source()
    source.sessions = [SimpleNamespace(account="?", mall_id="1")]
    first = _register(source, "cs_1:2", "hello", uid="b-1")
    second = _register(source, "cs_3:4", "hello", uid="b-2")
    source._handle_send_receipt(_receipt("hello", mall_id="1"), account_hint="?")
    assert first.result is None and second.result is None, "多义时必须放弃，不能猜"
    assert len(source._pending_sends) == 2


# ------------------------------------------------- 排队没执行 vs 执行了没回执
def _send_source():
    source = _bare_source()
    source._thread = SimpleNamespace(is_alive=lambda: True)
    return source


def test_queued_send_that_never_ran_reports_failed_and_retryable():
    """没轮到执行就不能报“已提交”：中心不会重试，买家什么都收不到。"""
    source = _send_source()
    result = source.send_and_wait("buyer-1", "hello", "cs_1:2", timeout=0.05)
    assert result["status"] == "failed"
    assert result["real_send"] is False
    assert result["retryable"] is True
    assert result["error"] == "send_not_executed"


def test_page_accepted_without_echo_reports_accepted():
    """页面受理了、回执没等到：不能撤销（撤销=把已发出的变成没发出），
    也不能一直 indeterminate（浮窗倒计时永远不停）。"""
    import threading
    import time

    source = _send_source()

    def mark():
        time.sleep(0.05)
        with source._lock:
            acts, source._act = source._act, []
        for name, args in acts:
            if name == "send":
                args[3].started = True
                args[3].accepted = True

    threading.Thread(target=mark, daemon=True).start()
    result = source.send_and_wait("buyer-1", "hello", "cs_1:2", timeout=0.3)
    assert result["status"] == "accepted"
    assert result["real_send"] is True
    assert result["delivery_uncertain"] is True
    assert result["echo_missing"] is True


def test_started_but_not_accepted_stays_indeterminate():
    import threading
    import time

    source = _send_source()

    def mark():
        time.sleep(0.05)
        with source._lock:
            acts, source._act = source._act, []
        for name, args in acts:
            if name == "send":
                args[3].started = True

    threading.Thread(target=mark, daemon=True).start()
    result = source.send_and_wait("buyer-1", "hello", "cs_1:2", timeout=0.3)
    assert result["status"] == "indeterminate"
    assert result["real_send"] is None


def test_unresolved_session_block_is_retryable():
    """工作台刚重启、页面 account 还是 "?" 时选不到会话：消息确实没发，
    必须 retryable，否则中心不重投，这条回复就丢了（现场“有的没实际发出”）。"""
    source = _bare_source()
    event = pddbridge_source._SendWaiter()
    source._do_send_action("buyer-1", "hello", "cs_1:2", event, 5.0, None)
    assert event.result["status"] == "blocked"
    assert event.result["retryable"] is True
    assert event.result["error"] == "session_unresolved"
