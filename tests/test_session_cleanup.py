# -*- coding: utf-8 -*-
"""本地一键清理（/api/cleanup_sessions）。

策略：买家最后说话（没回复）、标记转人工、近 keep_minutes 分钟内有动静的会话保留；
      其余（已经回复完的老会话）整条删除；保留的会话只留最近 keep_messages 条。
"""
import time

from run_frontend_service import LocalSeatState


def _state(tmp_path):
    return LocalSeatState(tmp_path / "seat_local_state.json")


def _put(st, account, buyer, role, ts, *, handoff=False, count=3):
    messages = [
        {"msg_id": "%s-%d" % (buyer, i), "role": role, "content": "m%d" % i, "ts": ts - (count - i)}
        for i in range(count)
    ]
    key = LocalSeatState._key(account, buyer)
    st.sessions[key] = {
        "buyer_id": buyer,
        "account": account,
        "nickname": buyer,
        "shop_id": "mall_1",
        "shop_name": "PDD-测试店",
        "platform": "pdd",
        "unread": 0,
        "messages": messages,
        "msg_count": len(messages),
        "last_content": messages[-1]["content"],
        "last_role": role,
        "last_ts": messages[-1]["ts"],
        "handoff": handoff,
    }
    return key


def test_cleanup_clear_all_removes_everything(tmp_path):
    """用户要的是「点一下就全清」：连未回复/转人工也不留。"""
    st = _state(tmp_path)
    now = time.time()
    _put(st, "cs_1:2", "b-unreplied", "user", now)
    _put(st, "cs_1:2", "b-handoff", "mall_cs", now, handoff=True)
    _put(st, "cs_1:2", "b-recent", "mall_cs", now)

    result = st.cleanup_sessions(clear_all=True)

    assert result["cleared_all"] is True
    assert result["removed_sessions"] == 3
    assert result["removed_messages"] == 9
    assert result["kept_sessions"] == 0
    assert st.sessions == {}
    # 落盘了（重开还是空的）
    assert LocalSeatState(st.path).sessions == {}


def test_cleanup_keeps_active_and_drops_replied_old_sessions(tmp_path):
    st = _state(tmp_path)
    now = time.time()
    kept_unreplied = _put(st, "cs_1:2", "buyer-unreplied", "user", now)
    kept_recent = _put(st, "cs_1:2", "buyer-recent", "mall_cs", now - 5 * 60)
    dropped = _put(st, "cs_1:2", "buyer-done", "mall_cs", now - 120 * 60)
    kept_handoff = _put(st, "cs_1:2", "buyer-handoff", "mall_cs", now - 30 * 60, handoff=True)

    result = st.cleanup_sessions(keep_minutes=10, handoff_keep_minutes=60, keep_messages=50)

    assert result["removed_sessions"] == 1
    assert result["kept_sessions"] == 3
    assert result["removed_messages"] == 3
    assert dropped not in st.sessions
    for key in (kept_unreplied, kept_recent, kept_handoff):
        assert key in st.sessions
    assert result["keep_reasons"] == {"unreplied": 1, "recent": 1, "handoff": 1}


def test_cleanup_ages_out_stale_handoff_sessions(tmp_path):
    """转人工标记从中心下发后不会自动过期，必须靠老化时间收掉，否则列表永远清不干。"""
    st = _state(tmp_path)
    now = time.time()
    fresh_handoff = _put(st, "cs_1:2", "buyer-handoff-new", "mall_cs", now - 20 * 60, handoff=True)
    stale_handoff = _put(st, "cs_1:2", "buyer-handoff-old", "mall_cs", now - 180 * 60, handoff=True)
    # 人工已经回过话的转人工会话：算已处理，过了 keep_minutes 就可清
    human_handled = _put(st, "cs_1:2", "buyer-handoff-human", "mall_cs", now - 90 * 60, handoff=True)
    st.sessions[human_handled]["messages"][-1]["sent_by"] = "human"

    result = st.cleanup_sessions(keep_minutes=10, handoff_keep_minutes=60)

    assert fresh_handoff in st.sessions
    assert stale_handoff not in st.sessions
    assert human_handled not in st.sessions
    assert result["removed_sessions"] == 2
    assert result["keep_reasons"] == {"handoff": 1}


def test_cleanup_truncates_kept_sessions_to_keep_messages(tmp_path):
    st = _state(tmp_path)
    now = time.time()
    key = _put(st, "cs_1:2", "buyer-chatty", "user", now, count=120)

    result = st.cleanup_sessions(keep_minutes=10, handoff_keep_minutes=60)

    assert result["truncated_sessions"] == 1
    assert result["removed_sessions"] == 0
    session = st.sessions[key]
    assert len(session["messages"]) == 50
    assert session["msg_count"] == 50
    # 摘要跟着最新一条走
    assert session["last_role"] == "user"
    assert session["last_ts"] == session["messages"][-1]["ts"]


def test_cleanup_keeps_pinned_open_session(tmp_path):
    """当前正在看的会话不能因为「已回复 + 静默」被清掉。"""
    st = _state(tmp_path)
    now = time.time()
    pinned = _put(st, "cs_1:2", "buyer-open", "mall_cs", now - 120 * 60)
    _put(st, "cs_1:2", "buyer-done", "mall_cs", now - 120 * 60)

    result = st.cleanup_sessions(keep_minutes=10, handoff_keep_minutes=60,
                                 keep_account="cs_1:2", keep_buyer="buyer-open")

    assert pinned in st.sessions
    assert result["removed_sessions"] == 1
    assert result["kept_sessions"] == 1
    assert result["keep_reasons"] == {"open": 1}


def test_cleanup_persists_result(tmp_path):
    st = _state(tmp_path)
    now = time.time()
    _put(st, "cs_1:2", "buyer-done", "mall_cs", now - 120 * 60)
    st.cleanup_sessions(keep_minutes=10, handoff_keep_minutes=60)

    reloaded = LocalSeatState(st.path)
    assert reloaded.sessions == {}
