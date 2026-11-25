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
