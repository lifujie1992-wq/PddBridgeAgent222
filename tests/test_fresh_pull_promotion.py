# -*- coding: utf-8 -*-
"""Phase-3：新鲜补拉提升为实时（消除 90 秒晚到）。

服务端规则：平台 ts 距今 > 90 秒即判 stale、不自动回复。
所以只要一条刚发的买家消息是走"周期补拉/打开会话加载"的 list 帧抓到的，
就必然晚到并丢回复。这里守住"能提升"和"不能提升"的边界。
"""
from __future__ import annotations

import time

from bridge.message_timing import history_ready, promote_fresh_history

NOW = 1_800_000_000.0


def _history(age=10, **extra):
    row = {"is_history": True, "ts": NOW - age, "msg_id": "m1",
           "history_reason": "replay", "capture_source": "pdd_cdp_history",
           "source": "history", "captured_at": NOW - age}
    row.update(extra)
    return row


def test_fresh_history_is_promoted():
    out = promote_fresh_history(_history(age=8), now=NOW, max_age=30)
    assert out["is_history"] is False
    assert out["auto_reply_eligible"] is True
    assert out["history_reason"] == "fresh_pull_promoted"
    assert out["source"] == "pdd_cdp"
    assert out["captured_at"] == NOW            # 真实发现时间，不是平台时间
    assert out["captured_at_ms"] == int(NOW * 1000)
    # 提升后不再受 90 秒窗口约束
    assert history_ready(out, NOW) is True


def test_stale_history_is_left_alone():
    """超过新鲜阈值的真历史不动，仍然走 90 秒窗口。"""
    # 60 秒前：超过新鲜阈值(30s)，但还没过 90 秒窗口 → 不提升，且被窗口压住
    held = promote_fresh_history(_history(age=60), now=NOW, max_age=30)
    assert held["is_history"] is True
    assert history_ready(held, NOW) is False
    # 120 秒前：真历史，不提升，只是允许照旧发送（带 is_history）
    old = promote_fresh_history(_history(age=120), now=NOW, max_age=30)
    assert old["is_history"] is True
    assert history_ready(old, NOW) is True


def test_replay_and_local_restore_never_promoted():
    for marker in ("spill", "replay", "local_queue"):
        out = promote_fresh_history(_history(age=5, capture_source=marker), now=NOW, max_age=30)
        assert out["is_history"] is True, marker
    out = promote_fresh_history(_history(age=5, replayed=1), now=NOW, max_age=30)
    assert out["is_history"] is True


def test_missing_or_future_platform_time_never_promoted():
    assert promote_fresh_history(_history(age=5, ts=0), now=NOW)["is_history"] is True
    future = _history(age=-600, history_reason="future_platform_time")
    assert promote_fresh_history(future, now=NOW)["is_history"] is True


def test_already_replied_message_not_promoted():
    """客服已经回过这条 → 不能当新问题提升（否则 AI 会重复回答）。"""
    out = promote_fresh_history(_history(age=5), now=NOW, max_age=30, has_seller_reply=True)
    assert out["is_history"] is True


def test_capture_source_marks_pull():
    out = promote_fresh_history(_history(age=5), now=NOW, max_age=30)
    assert out["capture_source"] == "pdd_history_pull"


def test_live_message_untouched():
    row = {"is_history": False, "ts": NOW - 2, "msg_id": "m2", "source": "pdd_cdp"}
    assert promote_fresh_history(row, now=NOW) == row


# ---------------- 端到端：进管线的行为 ----------------
def _source():
    from bridge.pddbridge_source import PddbridgeSource

    rows = []
    src = PddbridgeSource({}, on_event=rows.append)
    src.report_all = True
    return src, rows


def _list_frame(age, msg_id="m-fresh", content="在吗"):
    return {"cmd": "list", "target_id": 164945148, "messages": [{
        "msg_id": msg_id, "content": content, "ts": int(time.time() - age),
        "from": {"role": "user", "uid": "3000000000001",
                 "csid": "cs_427302374:164945148"},
        "to": {"role": "mall_cs", "uid": "427302374", "mall_id": "427302374"},
    }]}


def _push_frame(age, msg_id="m-fresh", content="在吗"):
    return {"cmd": "push", "target_id": 164945148, "message": {
        "msg_id": msg_id, "content": content, "ts": int(time.time() - age),
        "from": {"role": "user", "uid": "3000000000001",
                 "csid": "cs_427302374:164945148"},
        "to": {"role": "mall_cs", "uid": "427302374", "mall_id": "427302374"},
    }}


def test_fresh_list_frame_goes_out_as_live():
    """补拉到 5 秒前的消息 → 直接当实时上送，不再等 90 秒。"""
    src, rows = _source()
    src.feed_native_frame(_list_frame(age=5))
    assert rows, "没有产出事件"
    event = rows[0]
    assert event["is_history"] is False
    assert event["history_reason"] == "fresh_pull_promoted"
    assert event["capture_source"] == "pdd_history_pull"
    # captured_at 是发现时间（≈现在），不是平台时间
    assert abs(event["captured_at"] - time.time()) < 5


def test_old_list_frame_stays_history():
    src, rows = _source()
    src.feed_native_frame(_list_frame(age=600, msg_id="m-old"))
    assert rows and rows[0]["is_history"] is True


def test_live_push_upgrades_pending_history_copy():
    """先补拉到历史、后实时帧到 → 第二条标记 supersedes_history，供待发队列替换。"""
    src, rows = _source()
    src.feed_native_frame(_list_frame(age=600, msg_id="m-up"))
    assert rows and rows[0]["is_history"] is True
    src.feed_native_frame(_push_frame(age=1, msg_id="m-up"))
    assert len(rows) == 2
    upgraded = rows[1]
    assert upgraded["is_history"] is False
    assert upgraded.get("supersedes_history") is True
    assert upgraded["history_reason"] == "fresh_push_supersedes_history"
