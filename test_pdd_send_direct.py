"""直发通道单测：命令编码 / 回复解析 / 不可用时回退 / 计数。"""
from bridge.pdd_send_direct import DirectSender


class _Fake(DirectSender):
    """替换 _exchange，避免依赖真实命名管道。"""

    def __init__(self, replies, **kw):
        super().__init__(**kw)
        self.replies = list(replies)
        self.sent = []

    def _exchange(self, line):
        self.sent.append(line)
        return self.replies.pop(0) if self.replies else None


def test_disabled_returns_none_and_sends_nothing():
    s = DirectSender(enabled=False)
    assert s.send("123", "hi") is None
    assert s.ready() is False


def test_ok_reply_maps_to_cdp_compatible_result():
    s = _Fake(["OK 1"])
    r = s.send("4764375385604", "你好", "cs_1_2", "mall-a")
    assert r["ok"] is True and r["real_send"] is True and r["via"] == "dll_direct"
    assert r["status"] == "sent" and r["buyer_id"] == "4764375385604"
    assert s.sent == ["TEXT|cs_1_2|4764375385604|你好"]
    assert s.ok_count == 1 and s.fail_count == 0


def test_newlines_are_flattened_so_the_line_protocol_stays_intact():
    s = _Fake(["OK 1"])
    s.send("1", "第一行\n第二行\r\n第三行")
    assert s.sent[0] == "TEXT||1|第一行 第二行  第三行"


def test_fail_reply_and_unavailable_pipe():
    s = _Fake(["OK 0"])
    r = s.send("1", "x")
    assert r["ok"] is False and r["retryable"] is True and s.fail_count == 1

    s2 = _Fake([None])
    assert s2.send("1", "x") is None
    assert s2.skip_count == 1


def test_state_reply_drives_ready():
    assert _Fake(["READY 1 INSTANCE 0x1234"]).ready() is True
    assert _Fake(["READY 0 INSTANCE 0x0"]).ready() is False


def test_empty_input_never_hits_the_pipe():
    s = _Fake(["OK 1"])
    assert s.send("", "x") is None and s.send("1", "") is None
    assert s.sent == []
