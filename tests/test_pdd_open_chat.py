from __future__ import annotations

import json

from bridge import channel

# urllib.request 是全局模块对象，monkeypatch 掉它的 urlopen 会连别的后台线程一起接到
# （本地工作台推送失败会重试），它们的载荷会把「最后一次调用」冲掉。只认探域 PDD DLL 端点。
PDD_DLL_PATH = "/tanyu/client/pdd/dll/httpTestApi"


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return b'{"msg":"request success"}'


def test_normalize_pdd_account_accepts_underscore_and_colon() -> None:
    assert channel._normalize_pdd_account("cs_427302374_164945148") == "cs_427302374:164945148"
    assert channel._normalize_pdd_account("cs_427302374:164945148") == "cs_427302374:164945148"


def test_parse_same_store_native_open_confirmation() -> None:
    result = channel._pdd_parse_open_confirmation(
        "handleMsg_openCustomerDialog] open customer dialog, csId: "
        "cs_427302374_164945148 , userId: 4764375385604",
        account="cs_427302374:164945148",
        buyer_id="4764375385604",
    )

    assert result == {
        "verified": True,
        "source": "native_open",
        "account": "cs_427302374:164945148",
        "buyer_id": "4764375385604",
    }


def test_parse_cross_store_switch_then_native_open_confirmation() -> None:
    log_text = """
recv msg from inside ipc, msg = {"cmd":"change_business","data":{"account":"cs_427302374:164945148","platformType":1}}
handleMsg_openCustomerDialog] open customer dialog, csId: cs_427302374_164945148 , userId: 4764375385604
"""

    result = channel._pdd_parse_open_confirmation(
        log_text,
        account="cs_427302374:164945148",
        buyer_id="4764375385604",
    )

    assert result["verified"] is True
    assert result["account"] == "cs_427302374:164945148"
    assert result["buyer_id"] == "4764375385604"


def test_parse_wrong_store_confirmation_fails() -> None:
    result = channel._pdd_parse_open_confirmation(
        "handleMsg_openCustomerDialog] open customer dialog, csId: "
        "cs_150792824_188481947 , userId: 4764375385604",
        account="cs_427302374:164945148",
        buyer_id="4764375385604",
    )

    assert result["verified"] is False
    assert result["seen"][0]["account"] == "cs_150792824:188481947"


def test_parse_current_buyer_confirmation_requires_account_and_buyer() -> None:
    line = (
        'recv msg = {"cmd":"currentBuyerChange","data":'
        '{"mallId":"427302374","sellerId":"cs_427302374:164945148",'
        '"buyerId":"4764375385604","buyerNick":"buyer"}}'
    )

    result = channel._pdd_parse_open_confirmation(
        line,
        account="cs_427302374:164945148",
        buyer_id="4764375385604",
    )

    assert result["verified"] is True
    assert result["source"] == "current_buyer"


def test_open_chat_uses_native_dialog_command_and_waits_for_confirmation(monkeypatch) -> None:
    posted = {}
    monkeypatch.setattr(channel, "discover_ports", lambda **_kwargs: (47495, 22852, "test"))
    monkeypatch.setattr(channel, "_pdd_log_cursor", lambda _log_dir: {"inside.log": 123})

    def fake_urlopen(request, timeout):
        if PDD_DLL_PATH in request.full_url:
            posted["payload"] = json.loads(request.data.decode("utf-8"))
            posted["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(channel.urlrequest, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        channel,
        "_pdd_wait_for_open_confirmation",
        lambda *_args, **_kwargs: {
            "verified": True,
            "source": "native_open",
            "account": "cs_427302374:164945148",
            "buyer_id": "4764375385604",
        },
    )

    result = channel.open_chat_pdd(
        "4764375385604",
        "cs_427302374_164945148",
        buyer_nick="buyer",
        tanyu_log_dir=r"D:\logs",
    )

    assert result["ok"] is True
    assert posted["payload"]["cmd"] == "open_customer_dialog"
    assert posted["payload"]["data"]["account"] == "cs_427302374:164945148"
    assert posted["payload"]["data"]["imServerUser"] == {
        "userId": "4764375385604",
        "nick": "buyer",
        "appKey": "cnpdd",
    }
    assert posted["payload"]["data"]["isHighLight"] is True
    assert posted["payload"]["data"].get("msgList") is None


def test_automatic_pdd_send_does_not_focus_official_composer(monkeypatch) -> None:
    posted = {}
    monkeypatch.setattr(channel, "discover_ports", lambda **_kwargs: (47495, 22852, "test"))

    def fake_urlopen(request, timeout):
        if PDD_DLL_PATH in request.full_url:
            posted["payload"] = json.loads(request.data.decode("utf-8"))
            posted["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(channel.urlrequest, "urlopen", fake_urlopen)

    result = channel.send_text("buyer-1", "hello", "cs_100:1")

    assert result["ok"] is True
    assert posted["payload"]["cmd"] == "send_text"
    assert posted["payload"]["data"]["isHighLight"] is False


def test_open_chat_http_200_without_native_confirmation_fails(monkeypatch) -> None:
    monkeypatch.setattr(channel, "discover_ports", lambda **_kwargs: (47495, 22852, "test"))
    monkeypatch.setattr(channel, "_pdd_log_cursor", lambda _log_dir: {})
    monkeypatch.setattr(channel.urlrequest, "urlopen", lambda *_args, **_kwargs: _Response())
    monkeypatch.setattr(
        channel,
        "_pdd_wait_for_open_confirmation",
        lambda *_args, **_kwargs: {"verified": False, "seen": []},
    )

    result = channel.open_chat_pdd("buyer-1", "cs_100:1")

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["cmd"] == "open_customer_dialog"
    assert "did not confirm" in result["error"]
