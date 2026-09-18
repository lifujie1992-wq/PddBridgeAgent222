# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

import pytest

from bridge.client import BridgeClient
from bridge.config import load_config, machine_device_id
from bridge.parser import (
    BUILTIN_PARSER_PROFILE,
    configure_parser_profile,
    parse_line,
    parser_profile_status,
)
from bridge.watcher import LogWatcher
from run_frontend_service import (
    FrontHandler,
    LOCAL_GATEWAY_VERSION,
    LocalSeatState,
    SeatIdentityProvider,
    WEB,
    _filter_replayed_callback_batches,
    proxy_request_headers,
)
from run_pdd_client import (
    VERSION,
    LocalDeliveryQueue,
    _discover_pdd_shop_metadata,
    _event_from_message,
    _gateway_status_matches,
)


def _imws_line(body: dict, **event_fields) -> str:
    event = {"reqId": "req-1", "createTime": 1786150000000, "body": body, **event_fields}
    message = "拼多多-ImWs-上报聊天消息成功 内容:" + json.dumps([event], ensure_ascii=False)
    return json.dumps({"level": "info", "msg": message}, ensure_ascii=False)


def _buyer_body(**overrides) -> dict:
    body = {
        "sourceRef": "BUYER",
        "buyerAccount": "buyer-9988",
        "content": {"value": "你好😀 https://img.example/商品.png"},
        "msgInfo": {"messageId": "platform-message-1"},
        "msgTime": 1786150000123,
        "buyerNick": "测试买家",
        "cs_id": "cs_8899:77",
    }
    body.update(overrides)
    return body


@pytest.mark.parametrize("with_order", [False, True])
def test_imws_buyer_message_does_not_require_order_context(with_order: bool) -> None:
    body = _buyer_body()
    if with_order:
        body["firstLineOrderList"] = [{"orderSn": "240001234567890123"}]
    rows = parse_line(_imws_line(body), "logrus")
    assert len(rows) == 1
    row = rows[0]
    assert row["role"] == "user"
    assert row["buyer_id"] == "buyer-9988"
    assert row["msg_id"] == "platform-message-1"
    assert row["platform_message_id"] == "platform-message-1"
    assert row["account"] == "cs_8899:77"
    assert row["buyer_nick"] == "测试买家"
    assert row["content"] == "你好😀 https://img.example/商品.png"
    assert row["ts"] == 1786150000
    assert bool(row.get("order_context")) is with_order


@pytest.mark.parametrize(
    ("message_id", "req_id", "create_time", "expected_kind", "expected"),
    [
        ("mid-1", "rid-1", 1000, "message_id", "mid-1"),
        ("", "rid-1", 1000, "req_id", "rid-1"),
        ("", "", 1000, "create_time", "1000"),
        ("", "", "", "stable_hash", "imws-"),
    ],
)
def test_imws_message_id_fallback_order(
    message_id: str,
    req_id: str,
    create_time,
    expected_kind: str,
    expected: str,
) -> None:
    body = _buyer_body(msgInfo={"messageId": message_id})
    event = {"reqId": req_id, "createTime": create_time, "body": body}
    line = json.dumps({
        "msg": "拼多多-ImWs-上报聊天消息成功 内容:" + json.dumps([event], ensure_ascii=False)
    }, ensure_ascii=False)
    row = parse_line(line, "logrus")[0]
    assert row["identity_kind"] == expected_kind
    if expected_kind == "stable_hash":
        assert row["msg_id"].startswith(expected)
    else:
        assert row["msg_id"] == expected


@pytest.mark.parametrize("buyer_key", ["buyerAccount", "buyer_id", "buyerId", "userId"])
def test_logrus_accepts_only_explicit_buyer_identity_fields(buyer_key: str) -> None:
    payload = {
        "from": {"role": "user"},
        buyer_key: "buyer-explicit",
        "content": "普通咨询",
        "messageId": f"message-{buyer_key}",
        "ts": 1786150001,
        "account": "cs_8899:77",
    }
    rows = parse_line("buyer_msg=" + json.dumps(payload, ensure_ascii=False), "logrus")
    assert len(rows) == 1
    assert rows[0]["buyer_id"] == "buyer-explicit"


def test_logrus_accepts_from_user_uid_but_never_guesses_long_numbers() -> None:
    explicit = {
        "from": {"role": "user", "uid": "buyer-from-uid"},
        "content": "咨询内容",
        "messageId": "explicit-from-1",
        "account": "cs_8899:77",
    }
    guessed = {
        "content": "订单 240001234567890123 手机 13800138000",
        "orderSn": "240001234567890123",
        "phone": "13800138000",
        "account": "cs_8899:77",
    }
    assert parse_line("buyer_msg=" + json.dumps(explicit, ensure_ascii=False), "logrus")[0]["buyer_id"] == "buyer-from-uid"
    assert parse_line("buyer_msg=" + json.dumps(guessed, ensure_ascii=False), "logrus") == []


def test_logrus_dll_uses_only_explicit_sender_receiver_roles() -> None:
    seller = (
        '{"account":"cs_150792824:188481947","msg":"拼多多-Dll-接收聊天消息：'
        '消息内容:[完整客服回复]，消息ID:[,seller-mid-1]，发送时间:[1786176416]，'
        '发送者:[cs_150792824:188481947,mall_cs]，接收者:[6388210425,user]"}'
    )
    row = parse_line(seller, "logrus")[0]
    assert row["buyer_id"] == "6388210425"
    assert row["role"] == "mall_cs"
    assert row["platform_message_id"] == "seller-mid-1"
    assert row["ts"] == 1786176416

    buyer = (
        '{"account":"cs_150792824:188481947","msg":"拼多多-Dll-接收聊天消息：'
        '消息内容:[普通咨询]，消息ID:[buyer-mid-1,server-mid-1]，发送时间:[1786176417]，'
        '发送者:[buyer-explicit,user]，接收者:[150792824,mall_cs]"}'
    )
    row = parse_line(buyer, "logrus")[0]
    assert row["buyer_id"] == "buyer-explicit"
    assert row["role"] == "user"
    assert row["platform_message_id"] == "buyer-mid-1"


def test_logrus_dll_does_not_treat_cs_sender_as_buyer_identity() -> None:
    missing_buyer = (
        '{"account":"cs_150792824:188481947","msg":"拼多多-Dll-接收聊天消息：'
        '消息内容:[不能猜买家]，消息ID:[buyer-mid-2,6388210425]，发送时间:[1786176418]，'
        '发送者:[cs_150792824:188481947,user]，接收者:[150792824,mall_cs]"}'
    )
    assert parse_line(missing_buyer, "logrus") == []


def test_inside_message_derives_account_from_recipient_and_target_id() -> None:
    origin = {
        "push_type": 2,
        "push_data": {
            "data": [{
                "message": {
                    "ts": "1786181041",
                    "content": "你好 1724",
                    "from": {"uid": "4764375385604", "role": "user"},
                    "to": {"uid": "427302374", "role": "mall_cs"},
                    "msg_id": "1786181041943",
                    "nickname": "蜡**舅",
                }
            }]
        },
        "target_id": 164945148,
    }
    rows = parse_line("dllRecvCallBack " + json.dumps(origin, ensure_ascii=False), "inside")
    assert len(rows) == 1
    assert rows[0]["account"] == "cs_427302374:164945148"
    assert rows[0]["buyer_nick"] == "蜡**舅"


def test_inside_seller_message_preserves_shop_name() -> None:
    origin = {
        "push_data": {
            "data": [{
                "message": {
                    "ts": "1786181142",
                    "content": "[玫瑰]",
                    "from": {"uid": "427302374", "mall_id": "427302374", "role": "mall_cs"},
                    "to": {"uid": "4764375385604", "role": "user"},
                    "msg_id": "1786181142014",
                    "mallName": "T3星球蓝莓",
                }
            }]
        },
        "target_id": 164945148,
    }
    row = parse_line("dllRecvCallBack " + json.dumps(origin, ensure_ascii=False), "inside")[0]
    assert row["account"] == "cs_427302374:164945148"
    assert row["shop_name"] == "T3星球蓝莓"


def test_inside_goods_card_exports_product_context_without_inventing_order() -> None:
    message = {
        "ts": "1786197887",
        "content": "https://mobile.yangkeduo.com/goods.html?goods_id=955151378485",
        "from": {"uid": "buyer-goods", "role": "user"},
        "to": {"uid": "427302374", "role": "mall_cs"},
        "msg_id": "goods-message-1",
        "info": {
            "goodsID": "955151378485",
            "goodsName": "Summer T-shirt",
            "goodsThumbUrl": "https://img.example/goods.jpg",
            "goodsPrice": "115",
            "linkUrl": "goods.html?goods_id=955151378485",
        },
    }
    origin = {"push_data": {"data": [{"message": message}]}, "target_id": 164945148}
    row = parse_line("dllRecvCallBack " + json.dumps(origin), "inside")[0]
    assert row["role"] == "user"
    assert row["goods_id"] == "955151378485"
    assert row["goods_name"] == "Summer T-shirt"
    assert row["goods_thumb_url"] == "https://img.example/goods.jpg"
    assert row["goods_price"] == "115"
    assert row["goods_url"].startswith("https://mobile.yangkeduo.com/")
    assert row["template_name"] == "user_goods_card"
    assert "Summer T-shirt" in row["content"]
    assert not row.get("order_id")
    assert not row.get("order_info")


def test_user_source_goods_info_and_pre_msg_id_are_preserved() -> None:
    message = {
        "ts": "1786208285",
        "content": "[current user came from product page]",
        "from": {"uid": "9922018843449", "role": "user"},
        "to": {"uid": "150792824", "mall_id": "150792824", "role": "mall_cs"},
        "msg_id": "1786208285212",
        "pre_msg_id": "1786208284905",
        "type": 41,
        "template_name": "user_source",
        "info": {
            "title": "current user came from product page",
            "goods_info": {
                "goods_id": 985057237899,
                "goods_name": "Lenovo portable WiFi",
                "goods_thumb_url": "https://img.example/985057237899.jpg",
                "total_amount": 4680,
                "mall_link_url": "https://mobile.yangkeduo.com/goods.html?goods_id=985057237899",
            },
        },
    }
    origin = {"push_data": {"data": [{"message": message}]}, "target_id": 188481947}
    row = parse_line("dllRecvCallBack " + json.dumps(origin), "inside")[0]
    assert row["goods_id"] == "985057237899"
    assert row["goods_name"] == "Lenovo portable WiFi"
    assert row["goods_price"] == "46.8"
    assert row["goods_url"].endswith("goods_id=985057237899")
    assert row["pre_msg_id"] == "1786208284905"
    assert "Lenovo portable WiFi" in row["content"]


def test_inside_order_card_exports_only_explicit_structured_order_id() -> None:
    message = {
        "ts": "1786197999",
        "content": "Order consultation 13800138000",
        "from": {"uid": "buyer-order", "role": "user"},
        "to": {"uid": "427302374", "role": "mall_cs"},
        "msg_id": "order-message-1",
        "info": {
            "goodsID": "939266367254",
            "goodsName": "Portable WiFi",
            "orderSequenceNo": "260426-525598748294053",
            "order_id": "329889748294053",
            "orderStatus": "pending return",
            "shipping_status": 2,
            "pay_status": 2,
        },
    }
    origin = {"push_data": {"data": [{"message": message}]}, "target_id": 164945148}
    row = parse_line("dllRecvCallBack " + json.dumps(origin), "inside")[0]
    assert row["order_id"] == "260426-525598748294053"
    assert row["order_id"] != "329889748294053"
    assert row["order_id"] != "13800138000"
    assert row["order_info"]["source"] == "pdd_goods_card_info"
    assert row["order_info"]["order_status"] == "pending return"
    assert row["order_info"]["shipping_status"] == "2"
    assert "329889748294053" not in json.dumps(row["order_info"])


def test_imws_order_context_exports_normalized_order_and_product_fields() -> None:
    body = _buyer_body(firstLineOrderList=[{
        "orderSn": "260426-525598748294053",
        "goodsID": "939266367254",
        "goodsName": "Portable WiFi",
        "goodsThumbUrl": "https://img.example/order.jpg",
    }])
    row = parse_line(_imws_line(body), "logrus")[0]
    assert row["order_id"] == "260426-525598748294053"
    assert row["order_info"]["source"] == "pdd_imws_order_context"
    assert row["goods_id"] == "939266367254"
    assert row["goods_name"] == "Portable WiFi"
    assert row["goods_thumb_url"] == "https://img.example/order.jpg"


def _send_callback_line(*, inner_ts: str, outer_ts: str = "2026-08-08 17:43:25", marker: str = "dllSendCallBack") -> str:
    origin = {
        "SendContent": "seller reply",
        "BuyId": "4764375385604",
        "SellerId": "cs_427302374_164945148",
        "TimeStamp": inner_ts,
    }
    return f"{outer_ts} {marker} " + json.dumps({"originData": json.dumps(origin)}, ensure_ascii=False)


def test_live_send_callback_uses_inner_platform_time_and_seller_account() -> None:
    rows = parse_line(_send_callback_line(inner_ts="1786182204888"), "inside")
    assert len(rows) == 1
    row = rows[0]
    assert row["role"] == "mall_cs"
    assert row["content"] == "seller reply"
    assert row["buyer_id"] == "4764375385604"
    assert row["account"] == "cs_427302374:164945148"
    assert row["ts"] == 1786182204
    assert row["platform_ts_key"] == "1786182204888"
    assert row["msg_id"].startswith("callback-")


def test_origin_data_without_cmd_is_a_callback_when_timestamp_is_live() -> None:
    line = _send_callback_line(inner_ts="1786182204888", marker="originData")
    row = parse_line(line, "inside")[0]
    assert row["role"] == "mall_cs"
    assert row["msg_id"].startswith("callback-")


def test_stale_live_marker_history_replay_is_not_a_callback() -> None:
    line = _send_callback_line(
        inner_ts="1786182204888",
        outer_ts="2026-08-08 18:31:23",
    )
    assert parse_line(line, "inside") == []


def test_callback_platform_milliseconds_keep_distinct_ids() -> None:
    first = parse_line(_send_callback_line(inner_ts="1786182204888"), "inside")[0]
    second = parse_line(_send_callback_line(inner_ts="1786182204999"), "inside")[0]
    assert first["ts"] == second["ts"]
    assert first["msg_id"] != second["msg_id"]


def test_replayed_callback_batch_is_hidden_but_single_repeat_is_kept() -> None:
    messages = []
    contents = ["hello", "rose", "available", "welcome", "plan"]
    for timestamp, prefix in ((100, "first"), (200, "replay")):
        for index, content in enumerate(contents):
            messages.append({
                "msg_id": f"callback-{prefix}-{index}",
                "role": "mall_cs",
                "content": content,
                "ts": timestamp,
            })
    messages.append({
        "msg_id": "callback-real-repeat",
        "role": "mall_cs",
        "content": "hello",
        "ts": 300,
    })
    filtered = _filter_replayed_callback_batches(messages)
    ids = {message["msg_id"] for message in filtered}
    assert all(f"callback-first-{index}" in ids for index in range(5))
    assert all(f"callback-replay-{index}" not in ids for index in range(5))
    assert "callback-real-repeat" in ids


def test_inside_repairs_gb18030_bytes_misdecoded_as_latin1() -> None:
    origin = {
        "push_data": {
            "data": [{
                "message": {
                    "ts": "1786181041",
                    "content": "你好",
                    "from": {"uid": "4764375385604", "role": "user"},
                    "to": {"uid": "427302374", "role": "mall_cs"},
                    "msg_id": "gb-nick-1",
                    "nickname": "À¯**¾Ë",
                }
            }]
        },
        "target_id": 164945148,
    }
    row = parse_line("dllRecvCallBack " + json.dumps(origin, ensure_ascii=False), "inside")[0]
    assert row["buyer_nick"] == "蜡**舅"


def test_discovers_shop_metadata_without_replaying_chat(tmp_path: Path) -> None:
    origin = {
        "push_data": {"data": [{"message": {
            "ts": "1786181142",
            "content": "[玫瑰]",
            "from": {"uid": "427302374", "mall_id": "427302374", "role": "mall_cs"},
            "to": {"uid": "4764375385604", "role": "user"},
            "msg_id": "1786181142014",
            "mallName": "T3星球蓝莓",
        }}]},
        "target_id": 164945148,
    }
    (tmp_path / "inside_test.log").write_text(
        "dllRecvCallBack " + json.dumps(origin, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    rows = _discover_pdd_shop_metadata(tmp_path)
    assert len(rows) == 1
    assert rows[0]["type"] == "shop_metadata"
    assert rows[0]["shop_id"] == "mall_427302374"
    assert rows[0]["shop_name"] == "T3星球蓝莓"
    assert rows[0]["account"] == "cs_427302374:164945148"


def test_watcher_decodes_gb18030_and_preserves_emoji_utf8() -> None:
    assert LogWatcher._decode_line("中文消息".encode("gb18030")) == "中文消息"
    assert LogWatcher._decode_line("中文😀".encode("utf-8")) == "中文😀"


def _watcher() -> tuple[LogWatcher, list[dict]]:
    emitted: list[dict] = []
    watcher = LogWatcher(".", emitted.append, platform="pdd", attach_specs=[])
    return watcher, emitted


def _message(*, role="user", content="你好", ts=1000, source="plugin", platform_id="") -> dict:
    return {
        "platform": "pdd",
        "account": "cs_8899:77",
        "buyer_id": "buyer-1",
        "role": role,
        "content": content,
        "ts": ts,
        "platform_ts_key": str(ts),
        "msg_id": platform_id or f"{source}-{ts}",
        "platform_message_id": platform_id,
        "source": source,
    }


def test_cross_source_platform_message_id_dedup_prefers_inside() -> None:
    watcher, emitted = _watcher()
    watcher._queue_message(_message(source="plugin", platform_id="same-mid"), "plugin")
    watcher._queue_message(_message(source="inside", platform_id="same-mid"), "inside")
    watcher._flush_pending(force=True)
    assert len(emitted) == 1
    assert emitted[0]["source"] == "inside"


def test_buyer_repeated_text_at_different_platform_times_is_not_dropped() -> None:
    watcher, emitted = _watcher()
    watcher._queue_message(_message(ts=1000), "plugin")
    watcher._queue_message(_message(ts=1001), "logrus")
    watcher._flush_pending(force=True)
    assert [row["ts"] for row in emitted] == [1000, 1001]


def test_buyer_text_inherits_product_from_exact_pre_msg_chain() -> None:
    watcher, emitted = _watcher()
    product = _message(ts=1000, source="inside", platform_id="product-context")
    product.update({
        "goods_id": "985057237899",
        "goods_name": "Lenovo portable WiFi",
        "goods_url": "https://mobile.yangkeduo.com/goods.html?goods_id=985057237899",
    })
    buyer_text = _message(ts=1051, source="inside", platform_id="buyer-text")
    buyer_text.update({"content": "can I recharge as needed", "pre_msg_id": "product-context"})
    watcher._queue_message(product, "inside")
    watcher._queue_message(buyer_text, "inside")
    watcher._flush_pending(force=True)
    actual = next(row for row in emitted if row["platform_message_id"] == "buyer-text")
    assert actual["goods_id"] == "985057237899"
    assert actual["goods_name"] == "Lenovo portable WiFi"
    assert actual["goods_context_source"] == "pdd_pre_msg_chain"


def test_product_context_never_crosses_buyer_or_shop_scope() -> None:
    watcher, emitted = _watcher()
    product = _message(ts=1000, source="inside", platform_id="product-context")
    product["goods_id"] = "985057237899"
    watcher._queue_message(product, "inside")
    other_buyer = _message(ts=1001, source="inside", platform_id="other-buyer")
    other_buyer.update({"buyer_id": "buyer-2", "pre_msg_id": "product-context"})
    other_shop = _message(ts=1002, source="inside", platform_id="other-shop")
    other_shop.update({"account": "cs_9999:88", "pre_msg_id": "product-context"})
    watcher._queue_message(other_buyer, "inside")
    watcher._queue_message(other_shop, "inside")
    watcher._flush_pending(force=True)
    unrelated = [row for row in emitted if row["platform_message_id"] != "product-context"]
    assert all(not row.get("goods_id") for row in unrelated)


def test_seller_cross_source_semantic_dedup_prefers_complete_callback() -> None:
    watcher, emitted = _watcher()
    watcher._queue_message(
        _message(role="mall_cs", content="您好，这个商品今天可以正常发…", source="plugin"),
        "plugin",
    )
    callback = _message(
        role="mall_cs",
        content="您好，这个商品今天可以正常发货，预计明天揽收",
        source="inside",
    )
    callback["delivery_status"] = "confirmed"
    callback["msg_id"] = "callback-different-id"
    watcher._queue_message(callback, "inside")
    watcher._flush_pending(force=True)
    assert len(emitted) == 1
    assert emitted[0]["content"].endswith("预计明天揽收")


def test_unmatched_samples_are_bounded_and_redacted() -> None:
    watcher, _emitted = _watcher()
    for index in range(5):
        watcher._record_unmatched_sample(
            "logrus",
            f"agent_token=secret-{index} phone=13800138000 order=240001234567890123",
        )
    samples = watcher.diagnostics()["unmatched_candidate_samples"]["logrus"]
    assert 1 <= len(samples) <= 3
    assert all(len(row) <= 512 for row in samples)
    assert all("secret" not in row and "13800138000" not in row and "240001234567890123" not in row for row in samples)


def test_parser_profile_validates_caches_and_falls_back(tmp_path: Path) -> None:
    cache = tmp_path / "profile.json"
    profile = json.loads(json.dumps(BUILTIN_PARSER_PROFILE))
    profile["version"] = "remote-v2"
    try:
        assert configure_parser_profile(profile, cache) is True
        assert parser_profile_status()["source"] == "configured"
        assert cache.is_file()
        assert configure_parser_profile({"schema_version": 99}, cache) is True
        status = parser_profile_status()
        assert status["source"] == "cache"
        assert status["version"] == "remote-v2"
        assert "configured" in status["last_error"]
    finally:
        configure_parser_profile(BUILTIN_PARSER_PROFILE)


def test_event_and_local_gateway_preserve_and_upgrade_product_order_context(tmp_path: Path) -> None:
    class FakeAgent:
        cfg = {"agent_id": "pdd-agent-a"}
        platform = type("Platform", (), {"name": "pdd"})()

        @staticmethod
        def _ensure_event_id(event: dict) -> dict:
            return {**event, "event_id": "event-product-order-1"}

    message = {
        "msg_id": "product-order-message-1",
        "buyer_id": "buyer-context",
        "role": "user",
        "content": "Portable WiFi\nhttps://mobile.yangkeduo.com/goods.html?goods_id=939266367254",
        "ts": 1786197999,
        "account": "cs_427302374:164945148",
        "source": "inside",
        "goods_id": "939266367254",
        "goods_name": "Portable WiFi",
        "goods_url": "https://mobile.yangkeduo.com/goods.html?goods_id=939266367254",
        "goods_thumb_url": "https://img.example/order.jpg",
        "goods_price": "99.5",
        "goods_spec": "WiFi 6",
        "order_id": "260426-525598748294053",
        "order_info": {"source": "pdd_goods_card_info", "context_received": True},
        "order_context": {"present": True},
        "template_name": "user_goods_card",
        "raw_type": 24,
    }
    event = _event_from_message(FakeAgent(), message)
    for field in (
        "goods_id", "goods_name", "goods_url", "goods_thumb_url", "goods_price",
        "goods_spec", "order_id", "order_info", "order_context", "template_name", "raw_type",
    ):
        assert event[field] == message[field]

    state = LocalSeatState(tmp_path / "seat.json")
    state.configure(
        backend="",
        agent_token="token",
        agent_id="pdd-agent-a",
        device_id="device-a",
        platform="pdd",
    )
    sparse = {
        "msg_id": message["msg_id"],
        "buyer_id": message["buyer_id"],
        "role": "user",
        "content": "Product consultation",
        "ts": message["ts"],
        "account": message["account"],
        "platform": "pdd",
        "source": "plugin",
    }
    state.publish(sparse)
    state.publish(event)
    key = state._key(message["account"], message["buyer_id"])
    stored = state.sessions[key]["messages"]
    assert len(stored) == 1
    assert stored[0]["content"] == message["content"]
    assert stored[0]["goods_id"] == message["goods_id"]
    assert stored[0]["goods_thumb_url"] == message["goods_thumb_url"]
    assert stored[0]["order_id"] == message["order_id"]
    assert stored[0]["order_info"] == message["order_info"]

    detail_path = f"/api/session/{message['buyer_id']}?account={message['account']}"
    state.remote_cache[detail_path] = {"messages": [sparse]}
    merged = state.merge_detail(detail_path)
    assert merged is not None
    assert len(merged["messages"]) == 1
    assert merged["messages"][0]["goods_name"] == "Portable WiFi"
    assert merged["messages"][0]["order_id"] == "260426-525598748294053"


def _write_config(path: Path, token: str, agent_id: str = "pdd-agent-a") -> None:
    path.write_text(json.dumps({
        "platform": "pdd",
        "server_url": "http://127.0.0.1:1",
        "agent_token": token,
        "agent_id": agent_id,
        "device_id": "device-a",
    }), encoding="utf-8")


@pytest.fixture
def hot_gateway(tmp_path: Path):
    config_path = tmp_path / "bridge_config.json"
    _write_config(config_path, "token-old")
    provider = SeatIdentityProvider(config_path)
    identity = provider.snapshot()
    state = LocalSeatState(tmp_path / "seat_state.json")
    state.configure(
        backend=identity["server_url"],
        agent_token=identity["agent_token"],
        agent_id=identity["agent_id"],
        device_id=identity["device_id"],
        platform="pdd",
        config_path=str(config_path),
        identity_provider=provider,
    )

    class Handler(FrontHandler):
        local_state = state
        identity_provider = provider
        ui_role = "seat"
        seat_agent_token = identity["agent_token"]
        seat_agent_id = identity["agent_id"]
        seat_device_id = identity["device_id"]
        backend_base = identity["server_url"]
        web_root = WEB

        def log_message(self, _fmt, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state, provider, config_path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _post(base_url: str, token: str, event_id: str) -> int:
    event = {
        "event_id": event_id,
        "platform": "pdd",
        "account": "cs_8899:77",
        "buyer_id": "buyer-1",
        "role": "user",
        "content": "热更新测试",
        "ts": time.time(),
    }
    req = urlrequest.Request(
        base_url + "/api/bridge/v1/events",
        data=json.dumps({"events": [event]}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Agent-Token": token},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=3) as response:
            response.read()
            return int(response.status)
    except urlerror.HTTPError as exc:
        return int(exc.code)


def test_gateway_hot_reloads_token_and_invalidates_old_token(hot_gateway) -> None:
    base_url, _state, provider, config_path = hot_gateway
    assert _post(base_url, "token-old", "old-1") == 202
    time.sleep(0.01)
    _write_config(config_path, "token-new")
    assert _post(base_url, "token-old", "old-2") == 403
    assert _post(base_url, "token-new", "new-1") == 202
    with urlrequest.urlopen(base_url + "/api/local-seat/v1/status", timeout=3) as response:
        status = json.loads(response.read().decode("utf-8"))
    assert status["service"] == "pdd-local-seat-gateway"
    assert status["gateway_version"] == LOCAL_GATEWAY_VERSION
    assert status["platform"] == "pdd"
    assert status["agent_id"] == "pdd-agent-a"
    assert status["device_id"] == "device-a"
    assert Path(status["config_path"]) == config_path.resolve()
    assert provider.reload_count >= 2


def test_gateway_repairs_placeholder_session_and_persists_shop_name(tmp_path: Path) -> None:
    state = LocalSeatState(tmp_path / "seat.json")
    state.configure(
        backend="",
        agent_token="token",
        agent_id="pdd-agent-a",
        device_id="device-a",
        platform="pdd",
    )
    common = {
        "msg_id": "1786181041943",
        "buyer_id": "4764375385604",
        "role": "user",
        "content": "你好 1724",
        "ts": 1786181041,
        "platform": "pdd",
    }
    state.publish({**common, "account": "local_pdd:pdd-agent-a"})
    state.publish({
        **common,
        "account": "cs_427302374:164945148",
        "buyer_nick": "蜡**舅",
        "shop_name": "T3星球蓝莓",
    })
    state.publish({
        **common,
        "msg_id": "1786181142014",
        "role": "mall_cs",
        "content": "[玫瑰]",
        "account": "cs_427302374:164945148",
        "shop_name": "T3星球蓝莓",
    })
    correct_key = state._key("cs_427302374:164945148", "4764375385604")
    placeholder_key = state._key("local_pdd:pdd-agent-a", "4764375385604")
    assert correct_key in state.sessions
    assert placeholder_key not in state.sessions
    assert state.sessions[correct_key]["nickname"] == "蜡**舅"
    assert state.sessions[correct_key]["shop_name"] == "T3星球蓝莓"
    assert state.shop_names["mall_427302374"] == "T3星球蓝莓"
    assert state.shop_accounts["mall_427302374"] == "cs_427302374:164945148"

    metadata_result = state.publish({
        "type": "shop_metadata",
        "platform": "pdd",
        "shop_id": "mall_150792824",
        "shop_name": "VHE远见专卖店",
        "account": "cs_150792824:188481947",
        "event_id": "metadata-vhe",
    })
    assert metadata_result["committed"] is True
    assert metadata_result["accepted"] is True
    assert state.shop_names["mall_150792824"] == "VHE远见专卖店"


def test_center_shop_name_repairs_local_numeric_fallback_and_persists(tmp_path: Path) -> None:
    state_path = tmp_path / "seat.json"
    state = LocalSeatState(state_path)
    state.configure(
        backend="http://center",
        agent_token="token",
        agent_id="pdd-agent-a",
        device_id="device-a",
        platform="pdd",
    )
    state.publish({
        "platform": "pdd",
        "account": "cs_200:88",
        "buyer_id": "buyer-shop-name",
        "msg_id": "shop-name-message",
        "role": "user",
        "content": "hello",
        "ts": 200,
    })
    sessions_path = "/api/sessions?scope=active"
    center_name = "\u5ba2\u6237\u4e2d\u6587\u5e97\u94fa"
    state.remote_cache[sessions_path] = {
        "ok": True,
        "sessions": [{
            "account": "cs_200:88",
            "buyer_id": "buyer-shop-name",
            "shop_id": "mall_200",
            "shop_name": center_name,
            "last_content": "hello",
            "last_role": "user",
            "last_ts": 199,
        }],
    }

    merged = state.merge_sessions(sessions_path)
    assert merged["sessions"][0]["shop_name"] == center_name
    assert state.shop_names["mall_200"] == center_name
    stored = json.loads(state_path.read_text(encoding="utf-8"))
    assert stored["shop_names"]["mall_200"] == center_name

    with state.lock:
        changed = state._learn_remote_shop_names_locked({
            "shops": [{"shop_id": "mall_200", "shop_name": "\u5e97\u94fa200"}],
        })
    assert changed is False
    assert state.shop_names["mall_200"] == center_name


def test_center_shop_name_map_is_learned_without_local_log_metadata(tmp_path: Path) -> None:
    state = LocalSeatState(tmp_path / "seat.json")
    with state.lock:
        changed = state._learn_remote_shop_names_locked({
            "shop_name_map": {"cs_300:99": "\u4e2d\u5fc3\u914d\u7f6e\u5e97\u540d"},
        })
    assert changed is True
    assert state.shop_names["mall_300"] == "\u4e2d\u5fc3\u914d\u7f6e\u5e97\u540d"


def test_bridge_local_403_forces_resync_and_retries_once(hot_gateway, tmp_path: Path) -> None:
    base_url, _state, _provider, config_path = hot_gateway
    time.sleep(0.01)
    _write_config(config_path, "token-new")
    stop = threading.Event()
    queue = LocalDeliveryQueue(
        endpoint=base_url,
        token="token-old",
        agent_id="pdd-agent-a",
        device_id="device-a",
        config_path=config_path,
        path=tmp_path / "local.jsonl",
        stop_event=stop,
    )
    try:
        queue._config_mtime_ns = config_path.stat().st_mtime_ns
        queue.enqueue({
            "event_id": "retry-403-1",
            "platform": "pdd",
            "account": "cs_8899:77",
            "buyer_id": "buyer-1",
            "role": "user",
            "content": "403恢复",
            "ts": time.time(),
        })
        deadline = time.time() + 3
        while time.time() < deadline and queue.status()["pending"]:
            time.sleep(0.05)
        assert queue.status()["pending"] == 0
        assert queue.status()["last_error"] == ""
        assert queue.token == "token-new"
    finally:
        stop.set()
        queue.wakeup.set()
        queue.thread.join(timeout=2)


def test_pdd_status_probe_rejects_qianniu_or_other_identity(tmp_path: Path) -> None:
    config_path = tmp_path / "bridge_config.json"
    raw = {"agent_id": "pdd-a", "device_id": "device-a"}
    pdd = {
        "ok": True,
        "service": "pdd-local-seat-gateway",
        "gateway_version": VERSION,
        "platform": "pdd",
        "agent_id": "pdd-a",
        "device_id": "device-a",
        "config_path": str(config_path),
    }
    assert _gateway_status_matches(pdd, raw, config_path)
    assert not _gateway_status_matches({**pdd, "service": "qianniu-local-seat-gateway", "platform": "taobao"}, raw, config_path)
    assert not _gateway_status_matches({**pdd, "agent_id": "qianniu-a"}, raw, config_path)
    assert load_config(config_path)["local_workbench_url"].endswith(":18767")


def test_local_queue_survives_failure_and_drains_after_gateway_recovers(
    tmp_path: Path,
    hot_gateway,
) -> None:
    base_url, _state, _provider, _config_path = hot_gateway
    stop = threading.Event()
    queue_path = tmp_path / "local-recovery.jsonl"
    queue = LocalDeliveryQueue(
        endpoint="http://127.0.0.1:1",
        token="token-old",
        agent_id="pdd-agent-a",
        path=queue_path,
        stop_event=stop,
    )
    try:
        queue.enqueue({
            "event_id": "offline-1",
            "platform": "pdd",
            "account": "cs_8899:77",
            "buyer_id": "buyer-1",
            "role": "user",
            "content": "断网不丢",
            "ts": time.time(),
        })
        time.sleep(0.2)
        assert queue.status()["pending"] == 1
        assert queue_path.is_file() and "offline-1" in queue_path.read_text(encoding="utf-8")
        queue.endpoint = base_url
        queue.wakeup.set()
        deadline = time.time() + 3
        while time.time() < deadline and queue.status()["pending"]:
            time.sleep(0.05)
        assert queue.status()["pending"] == 0
        assert queue.status()["last_error"] == ""
    finally:
        stop.set()
        queue.wakeup.set()
        queue.thread.join(timeout=2)


def test_bridge_client_uses_stable_machine_device_header(monkeypatch) -> None:
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ok":true}'

    def fake_urlopen(request, timeout):
        captured.update({key.lower(): value for key, value in request.header_items()})
        assert timeout == 15.0
        return Response()

    monkeypatch.setattr("bridge.client.urllib.request.urlopen", fake_urlopen)
    device_id = machine_device_id()
    assert device_id.startswith("device-")
    BridgeClient("http://center", "token", "pdd-agent", "PDD", device_id).register()
    assert captured["x-device-id"] == device_id


def test_seat_proxy_discards_all_browser_credentials() -> None:
    headers = {
        "Authorization": "Bearer stale-browser-token",
        "Cookie": "session=stale",
        "X-User-Token": "stale-user-token",
        "X-Agent-Token": "caller-agent-token",
        "Accept": "application/json",
    }
    proxied = proxy_request_headers(
        headers,
        ui_role="seat",
        agent_token="local-agent-token",
        agent_id="pdd-agent",
        device_id="device-a",
    )
    lowered = {key.lower(): value for key, value in proxied.items()}
    assert "authorization" not in lowered
    assert "cookie" not in lowered
    assert "x-user-token" not in lowered
    assert lowered["x-agent-token"] == "local-agent-token"
    assert lowered["x-agent-id"] == "pdd-agent"
    assert lowered["x-device-id"] == "device-a"


def test_remote_session_changes_wake_the_local_workbench(tmp_path: Path) -> None:
    state = LocalSeatState(tmp_path / "seat.json")
    state.configure(
        backend="http://center",
        agent_token="token",
        agent_id="pdd-agent",
        device_id="device-a",
    )
    path = "/api/sessions?scope=active"
    payload = {"ok": True, "sessions": [{"buyer_id": "buyer-1", "last_ts": 1}]}
    state._remote_json = lambda _path: json.loads(json.dumps(payload))

    def wait_refresh() -> None:
        deadline = time.time() + 2
        while time.time() < deadline:
            with state.lock:
                if not state.inflight:
                    return
            time.sleep(0.01)
        raise AssertionError("remote refresh did not finish")

    state.schedule_refresh(path)
    wait_refresh()
    assert state.event_version == 1
    state.schedule_refresh(path)
    wait_refresh()
    assert state.event_version == 1
    payload["sessions"][0]["last_ts"] = 2
    state.schedule_refresh(path)
    wait_refresh()
    assert state.event_version == 2
