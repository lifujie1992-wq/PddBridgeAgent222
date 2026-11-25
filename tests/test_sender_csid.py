# -*- coding: utf-8 -*-
"""客服身份 csid 的统一口径（0.7.17）。

现场：DLL 直发 15+ 条全部 `OK 0`、一条没发出去。根因是缺 sender label：
PDD 的 SendTextMsg/SendImageMsg 要的是页面上的客服身份（如「主账号」），
不是 `cs_mall:uid` 这种账号串。传错/传空一律返回 false。
"""
from __future__ import annotations

import time

from bridge.pddbridge_source import PddbridgeSource, _Session


class FakeCdp:
    def __init__(self, values):
        self.values = list(values)
        self.calls = []

    def eval(self, expr, cid=None, timeout=None):
        self.calls.append(expr)
        return self.values.pop(0) if self.values else None

    def close(self):
        pass


def _session(cdp, account="cs_1:100"):
    session = _Session(57165, "u", cdp, 1, "t1")
    session.account = account
    return session


def test_prefers_injection_helper():
    src = PddbridgeSource({}, on_event=lambda m: None)
    session = _session(FakeCdp(["主账号"]))
    assert src._page_sender_csid(session) == "主账号"


def test_falls_back_to_vue_expression():
    src = PddbridgeSource({}, on_event=lambda m: None)
    session = _session(FakeCdp([None, "主账号"]))
    assert src._page_sender_csid(session) == "主账号"


def test_empty_is_empty_not_string_none():
    src = PddbridgeSource({}, on_event=lambda m: None)
    session = _session(FakeCdp(["null", None]))
    assert src._page_sender_csid(session) == ""


def test_cached_for_a_while():
    src = PddbridgeSource({}, on_event=lambda m: None)
    cdp = FakeCdp(["主账号"])
    session = _session(cdp)
    assert src._page_sender_csid(session) == "主账号"
    calls = len(cdp.calls)
    assert src._page_sender_csid(session) == "主账号"      # 命中缓存，不再 eval
    assert len(cdp.calls) == calls
    # 过期后重新取
    session.sender_csid_at = time.time() - 999
    src._page_sender_csid(session)
    assert len(cdp.calls) > calls      # 缓存过期后确实重新取了


def test_send_image_uses_resolved_csid(monkeypatch):
    """发图必须带上解析出来的 sender label，否则 DLL 返回 OK 0。"""
    seen = {}

    class Sender:
        def send_image(self, uid, path, csid, account):
            seen.update(uid=uid, path=path, csid=csid, account=account)
            return {"ok": True, "status": "sent", "real_send": True,
                    "via": "dll_direct_image", "cost_ms": 1}

    src = PddbridgeSource({}, on_event=lambda m: None)
    src.direct_send = Sender()
    session = _session(FakeCdp(["主账号"]))
    src.sessions = [session]
    result = src.send_image("3000000000001", r"C:\tmp\a.png", "cs_1:100")
    assert result["ok"] is True
    assert seen["csid"] == "主账号", "发图没带正确 csid，DLL 必然 OK 0"
