# -*- coding: utf-8 -*-
"""跨数据源入站判重。

PDD 上有**两条腿**同时收消息，各自判重、互不知情：

1. 探域日志 watcher（`LogWatcher`）—— 实时，`source=inside`
2. CDP 周期回拉（`PddbridgeSource`，`_history_only=True`）—— 每 `history_pull_seconds`
   回拉一次，`source=history`

两条腿都回调到 `agent._on_local_event`，所以账本必须放在那儿。判重表原先只在
`PddbridgeSource._seen_any` 里 —— watcher 那条腿从不写它，于是同一条买家消息被两边
各报一次。

实测（v0.9.3，2026-09-21 00:12，真实台账+日志）：

    00:12:26.673  captured  id=f62edf85...  history=False  source=inside   ← 探域日志腿
    00:12:27.770  event upload terminal=1 remaining=0                       ← 第一条已出队
    00:12:44.524  captured  id=f62edf85...  history=True   source=history  ← CDP 回拉腿
                  （同一个 event_id、同一平台 msg_id 1789920746142，间隔 17.9s）

中心按 event_id 幂等去了重，所以代价是流量翻倍 + 日志噪音，买家不会被打扰两次 ——
不是客户可见故障，但也绝不是"没问题"。
"""
import logging
import pathlib
import threading
import time
from collections import deque
from types import SimpleNamespace

import pytest

from bridge import agent as agent_mod
from bridge.agent import (BridgeAgent, _INGEST_DEDUP_MIN_SECONDS,
                          _INGEST_DEDUP_PULL_FACTOR, _ingest_window_seconds)


# 现场那条被重复上报的消息（值来自真实台账）
PLATFORM_ID = "1789920746142"
ACCOUNT = "cs_100000003:200000001"
BUYER = "3000000000001"


def ingest_agent():
    """只搭 `_on_local_event` 需要的状态。

    跳过 `__init__`（同仓库既有测试约定）：新加的实例属性必须能惰性兜底，否则
    `object.__new__(BridgeAgent)` 造出来的 agent 会被 AttributeError 打挂。
    """
    agent = object.__new__(BridgeAgent)
    agent.cfg = {"agent_token": "t", "agent_id": "a", "platform": "pdd"}
    agent.platform = SimpleNamespace(name="pdd")
    agent._pending = deque()
    agent._pending_lock = threading.Lock()
    agent._accounts_seen = {}
    agent._last_error = ""
    agent._local_ingest_queue = None       # 直接属性访问（非 getattr），必须赋 None
    agent._local_ingest_url = ""
    # 落盘 / 记来源时间与判重无关，隔离掉。
    # enqueued 是**入队流水**：drain_pending 会把队列腾空，只剩 _pending
    # 数不出"一共报了几条"。
    agent.enqueued = []
    agent._append_local_queue = lambda event: agent.enqueued.append(event)
    agent._remember_source_time = lambda event: None
    return agent


def drain_pending(agent):
    """模拟"第一条已经上传出队"。

    **不做这一步测出来的就是假绿**：现实里两条腿的间隔是 15~18s，第一条 9 秒前就
    被中心 accepted 并出队了（日志 `event upload terminal=1 remaining=0`），
    所以 `_pending_ids` 那层旧守卫**根本拦不住**第二次 —— 实测就是这么双投的。
    队列不腾空，第二次会被旧守卫拦下，测试通过但新逻辑一行没跑。
    """
    with agent._pending_lock:
        agent._pending.clear()
        agent._pending_ids.clear()


def inbound(platform_id=PLATFORM_ID, *, source="inside", history=False,
            ts=1789920746, content="1"):
    """一条入站买家消息。source=inside → 探域日志腿；source=history → CDP 回拉腿。"""
    return {
        "platform": "pdd",
        "msg_id": platform_id,
        "platform_message_id": platform_id,
        "identity_kind": "message_id",
        "buyer_id": BUYER,
        "role": "user",
        "content": content,
        "ts": ts,
        "account": ACCOUNT,
        "source": source,
        "is_history": history,
    }


# ==================================================== 1. 核心回归（端到端）

def test_two_sources_reporting_the_same_message_enqueues_once():
    """**核心回归**：同一条消息经两条腿各报一次，只能入队一条。

    改动前 `_seen_any` 只被 PddbridgeSource 使用，探域日志那条腿从不写它，
    第二次照报 —— 现场 8 个 event_id 里 5 个被重复。
    """
    agent = ingest_agent()
    agent._on_local_event(inbound(source="inside", history=False))
    assert len(agent.enqueued) == 1, "第一条（探域日志腿）必须入队"

    drain_pending(agent)        # ← 关键：不腾空的话第二次会被旧守卫拦下，测了个假绿

    agent._on_local_event(inbound(source="history", history=True))
    assert len(agent.enqueued) == 1, "第二条（CDP 回拉腿）必须被跨源判重拦下"
    assert agent._ingest_stats_dict()["dropped"] == 1, "必须走的是新的跨源判重，不是旧守卫"


def test_order_does_not_matter():
    """哪条腿先到都要拦 —— CDP 回拉先到、探域日志后到也一样。"""
    agent = ingest_agent()
    agent._on_local_event(inbound(source="history", history=True))
    drain_pending(agent)
    agent._on_local_event(inbound(source="inside", history=False))
    assert agent._ingest_stats_dict()["dropped"] == 1


def test_suppressed_duplicate_is_not_pushed_to_the_local_workbench():
    """被拦下的重复不能推给本机工作台 —— 否则浮窗上买家会看到两条一样的。"""
    agent = ingest_agent()
    pushed = []
    agent._local_ingest_queue = SimpleNamespace(put_nowait=pushed.append)
    agent._on_local_event(inbound(source="inside"))
    drain_pending(agent)
    agent._on_local_event(inbound(source="history", history=True))
    assert len(pushed) == 1


def test_distinct_messages_are_not_suppressed():
    """误杀的代价比重复大得多：不同平台 id 一条都不能拦。"""
    agent = ingest_agent()
    for pid in ("1789920746142", "1789920748542", "1789920749279"):
        agent._on_local_event(inbound(pid))
        drain_pending(agent)
    assert len(agent.enqueued) == 3


def test_the_real_regression_scenario_five_duplicates():
    """照抄现场：8 条唯一消息，其中 5 条被两条腿各报一次 → 必须只入队 8 条。"""
    agent = ingest_agent()
    ids = ["1789920746142", "1789920748542", "1789920749279",
           "1789920124718", "1789920125171", "1789920143692",
           "1789920124167", "1789920761000"]
    duplicated = set(ids[:5])
    for pid in ids:                                   # 探域日志腿：8 条实时
        agent._on_local_event(inbound(pid, source="inside"))
        drain_pending(agent)
    for pid in sorted(duplicated):                    # CDP 回拉腿：重报其中 5 条
        agent._on_local_event(inbound(pid, source="history", history=True, ts=1789920124))
        drain_pending(agent)
    assert len(agent.enqueued) == 8, "8 条唯一消息，多一条都是重复上报"


def test_same_id_after_window_is_reported_again():
    """窗口过期后允许再报 —— 窗口不能无限大，否则长期运行会变成永久静音。"""
    agent = ingest_agent()
    agent._ingest_window = 600.0
    agent._on_local_event(inbound(source="inside"))
    drain_pending(agent)
    agent._ingest_seen_table()[PLATFORM_ID] = time.time() - 601.0
    agent._on_local_event(inbound(source="history", history=True))
    assert len(agent.enqueued) == 2, "窗口过期后必须放行"


# ==================================================== 2. 只对入站生效

def test_outgoing_is_left_to_the_dedicated_merge():
    """出站有自己的合并逻辑（`_merge_outgoing_duplicate`：按内容前缀合并、保留
    "更完整"的一条），跨源判重必须让路 —— "先到先得"会把带平台 id 的那条丢掉。"""
    agent = ingest_agent()
    event = inbound()
    event["role"] = "mall_cs"
    assert agent._cross_source_duplicate(event) is False
    assert agent._cross_source_duplicate(dict(event)) is False


# ==================================================== 3. 只认平台 id

@pytest.mark.parametrize("identity", [
    "callback-833d73efae2d02df7302",    # 发送回执的合成 id（现场日志里出现过）
    "frame-9f8e7d6c5b4a39281706",       # 诊断帧
    "imws-0123456789abcdef0123",        # 探域日志兜底
    "0123456789abcdef01234567",         # 24 位内容+时间哈希：同秒同内容会撞哈希
    "",
    "   ",
])
def test_synthetic_ids_are_never_used_for_dedup(identity):
    """合成的兜底 id 会撞哈希，拿它判重会**误杀真实消息** —— 宁可重复也不丢。

    和 `pddbridge_source._is_fallback_id` 是同一个理由。
    """
    agent = ingest_agent()
    event = inbound()
    event["platform_message_id"] = identity
    event["msg_id"] = identity
    assert agent._ingest_identity(event) == ""
    # 连续两条同 id 也必须都放行
    assert agent._cross_source_duplicate(event) is False
    assert agent._cross_source_duplicate(dict(event)) is False


def test_platform_id_wins_over_msg_id():
    """`platform_message_id` 可用就用它；缺失才回退到 `msg_id`。"""
    event = inbound()
    event["msg_id"] = "imws-0123456789abcdef0123"      # 合成
    assert BridgeAgent._ingest_identity(event) == PLATFORM_ID

    del event["platform_message_id"]
    assert BridgeAgent._ingest_identity(event) == ""   # 回退到的也是合成 id → 放弃判重


def test_diagnostic_events_are_skipped():
    event = inbound()
    event["is_diagnostic"] = True
    assert BridgeAgent._ingest_identity(event) == ""


# ==================================================== 4. 窗口

def test_hit_refreshes_timestamp_so_repeats_stay_suppressed():
    """命中必须刷新时间戳。

    CDP 回拉是**按固定节奏反复重报同一条**的。不刷新的话窗口从"第一次上报"起算，
    累计出窗口就再漏一条 —— 实测 243s 重报 / 600s 窗口 = "压两条、漏一条"的锯齿。
    """
    agent = ingest_agent()
    table = agent._ingest_seen_table()
    table[PLATFORM_ID] = time.time() - 300.0
    assert agent._cross_source_duplicate(inbound()) is True
    assert table[PLATFORM_ID] >= time.time() - 1.0, "命中必须刷新时间戳"


def test_window_floor_without_history_pull():
    assert _ingest_window_seconds({}) == _INGEST_DEDUP_MIN_SECONDS


def test_window_covers_the_pull_interval():
    """窗口必须盖过 CDP 腿的回拉间隔，否则账本在重报之前就过期了。"""
    window = _ingest_window_seconds({"history_pull_seconds": 120})
    assert window > 120.0
    assert window == 120 * _INGEST_DEDUP_PULL_FACTOR


def test_explicit_window_overrides_auto():
    assert _ingest_window_seconds({"cross_source_dedup_seconds": 1200}) == 1200.0


def test_explicit_window_cannot_go_below_the_floor():
    """配得比下限还短等于没有判重 —— 钳到下限。"""
    assert _ingest_window_seconds({"cross_source_dedup_seconds": 5}) == _INGEST_DEDUP_MIN_SECONDS


@pytest.mark.parametrize("bad", ["abc", None, {}, []])
def test_garbage_window_config_falls_back_to_auto(bad):
    assert _ingest_window_seconds({"cross_source_dedup_seconds": bad}) == _INGEST_DEDUP_MIN_SECONDS


# ==================================================== 5. 可观测性 / 惰性兜底

def test_bare_agent_without_init_does_not_crash():
    """回归钉子：跳过 `__init__` 的 agent 走判重不能抛异常。

    v0.9.0 接 WS、v0.9.2 加命令计数时，都因为直接访问新属性把整条链路打挂过。
    """
    agent = object.__new__(BridgeAgent)
    agent.cfg = {}
    assert agent._cross_source_duplicate(inbound()) is False
    assert agent._cross_source_duplicate(inbound()) is True      # 第二次同 id
    assert agent._ingest_stats_dict()["dropped"] == 1


def test_drops_are_counted_and_logged(caplog):
    """被拦下的重复要能在日志/status 里看见 —— 静默判重没法排查。"""
    agent = ingest_agent()
    agent._on_local_event(inbound(source="inside"))
    drain_pending(agent)
    with caplog.at_level(logging.INFO, logger="pdd.bridge"):
        agent._on_local_event(inbound(source="history", history=True))
    assert agent._ingest_stats_dict()["dropped"] == 1
    assert any("跨源重复已拦" in record.getMessage() for record in caplog.records), \
        "拦下时必须留一行日志"


def test_prune_drops_only_expired_entries(monkeypatch):
    """清理只能清窗口外的条目，否则会把判重能力一起清掉。"""
    monkeypatch.setattr(agent_mod, "_INGEST_DEDUP_PRUNE_THRESHOLD", 1)
    agent = ingest_agent()
    agent._ingest_window = 600.0
    agent._ingest_pruned_at = 0.0
    table = agent._ingest_seen_table()
    now = time.time()
    table["old"] = now - 601.0
    table["fresh"] = now - 1.0
    agent._prune_ingest_seen(now)
    assert "old" not in table
    assert "fresh" in table


def test_status_exposes_the_dedup_state():
    """心跳/status 里要能看到判重窗口和拦了多少 —— 否则线上没法确认它在工作。"""
    from bridge.config import CONFIG_VERSION       # noqa: F401  (确保配置模块可用)
    assert _ingest_window_seconds({"history_pull_seconds": 120}) == 600.0
    assert _INGEST_DEDUP_MIN_SECONDS == 600.0


# ==================================================== 6. 救援副本（v0.9.5 回归）

def test_rescue_copy_gets_through_after_first_copy_is_dead_lettered():
    # 不用 pytest 的 tmp_path fixture：这台机 pytest 收尾解析临时目录必崩
    # （WinError 448 重解析点），会把失败详情一起吞掉。
    import tempfile
    """**核心回归（v0.9.5）**：第一条副本死信后，另一条腿的救援副本必须能进来。

    实测场景（2026-09-21 00:31 报、00:36:45 被拦）：

        00:31:28  event queued  id=a800a893  msg_id=1789921887701  account 空  source=inside
                  （中心拒收 PERSISTENCE_UNAVAILABLE → dead_letter，**从没到达中心**）
        00:36:45  event 跨源重复已拦  identity=1789921887701（另一数据源 317.6s 前已上报）
                  ↑ 这一份带着完整 account，本来能送达 —— 才是该放行的那份

    v0.9.4 的判重只记"这条被另一条腿报过了"，不管那次上报成没成功，于是把
    "延迟 5 分钟送达"变成了"永久丢失"。v0.9.3（加判重之前）本来能救回来。
    """
    agent = ingest_agent()
    agent.queue_path = pathlib.Path(tempfile.mkdtemp()) / "bridge_queue_pdd.jsonl"

    # 第一次：探域日志腿，account 为空（现场就是这样）
    first = inbound(source="inside")
    first["account"] = ""
    agent._on_local_event(first)
    assert len(agent.enqueued) == 1
    assert agent._ingest_seen_table(), "第一次上报应当占住判重身份"

    # 中心拒收 → 落死信
    agent._record_event_loss([first], [(first, "PERSISTENCE_UNAVAILABLE")])
    drain_pending(agent)

    # 第二次：CDP 腿带完整 account 重报同一条 —— 必须放行
    agent._on_local_event(inbound(source="history", history=True))
    assert len(agent.enqueued) == 2, \
        "救援副本被挡住了 = 这条买家消息永久丢失，大脑永远收不到"


def test_successful_report_still_suppresses_the_other_leg():
    """反面钉子：**成功送达**的那条仍然要压住另一条腿（否则流量翻倍又回来了）。"""
    agent = ingest_agent()
    agent._on_local_event(inbound(source="inside"))
    drain_pending(agent)
    agent._on_local_event(inbound(source="history", history=True))
    assert len(agent.enqueued) == 1, "成功送达的不能重复上报"


def test_release_frees_the_identity():
    """`_ingest_release` 只放开这一条身份，不能把整张表清掉。"""
    agent = ingest_agent()
    agent._on_local_event(inbound("1789920746142"))
    agent._on_local_event(inbound("1789920748542"))
    assert len(agent._ingest_seen_table()) == 2
    agent._ingest_release(inbound("1789920746142"))
    table = agent._ingest_seen_table()
    assert "1789920746142" not in table
    assert "1789920748542" in table


def test_release_ignores_synthetic_ids():
    """合成 id 本来就没进表，release 不能抛也不能误删。"""
    agent = ingest_agent()
    agent._ingest_seen_table()["1789920746142"] = time.time()
    event = inbound()
    event["platform_message_id"] = "callback-833d73efae2d02df7302"
    event["msg_id"] = "callback-833d73efae2d02df7302"
    agent._ingest_release(event)
    assert "1789920746142" in agent._ingest_seen_table()


def test_release_is_safe_on_a_bare_agent():
    """跳过 __init__ 的 agent 上 release 也不能抛（惰性兜底）。"""
    agent = object.__new__(BridgeAgent)
    agent.cfg = {}
    agent._ingest_release(inbound())        # 不能抛
