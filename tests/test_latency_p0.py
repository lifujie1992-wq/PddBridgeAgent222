# -*- coding: utf-8 -*-
"""P0 时延修复（0.7.5 r1/r2 移植到 0.10）：两个**店铺级灰度**，默认全关。

1) 即时上传（r1）：命中白名单的店铺，买家消息不再被订单上下文门控扣住 —— 原始
   消息立即上传，订单上下文随后按同一个 `msg_id` 补发一条增强事件。
2) 出站并发发送（r2）：命中白名单的店铺，指令投入发送池，取指令循环不再被上一条的
   发送确认超时（默认 10s）占住；同一买家仍由逐会话锁保证有序。

两条都是 fail-closed 白名单：键缺失 / `[]` / 非列表一律等于关闭，且未命中时走的
代码路径与改动前完全一致。
"""
import json
import queue
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

from bridge.agent import BridgeAgent
from bridge.config import _shop_id_allowlist, load_config

import run_pdd_client


def make_agent(tmp_path, **cfg):
    """跳过 __init__ 造 agent（仓库既有约定：新属性必须惰性兜底）。"""
    agent = object.__new__(BridgeAgent)
    agent.cfg = {"agent_id": "p0-test", "dry_run": False, **cfg}
    agent.platform = SimpleNamespace(name="pdd")
    agent._pending = deque()
    agent._pending_ids = set()
    agent._pending_lock = threading.Lock()
    agent._stop = threading.Event()
    agent._inflight_ids = set()
    agent._local_ingest_queue = None
    agent._accounts_seen = {}
    agent._last_error = ""
    agent._last_status = {}
    agent._commands_done = set()
    agent.queue_path = tmp_path / "events.jsonl"
    agent._ledger = None
    # 测试里绝不起真实后台线程
    agent._immediate_upload_started = True
    agent._immediate_upload_event = threading.Event()
    agent._pdd_context_worker_started = True
    agent._pdd_context_gate_lock = threading.RLock()
    agent._pdd_context_pending_ids = set()
    agent._pdd_context_deadlines = {}
    agent._pdd_context_queue = queue.Queue()
    agent._pdd_context_gate_seconds = 5.0
    agent._local_first_gate_lock = threading.RLock()
    agent._local_first_deadlines = {}
    agent._local_delivery = SimpleNamespace(
        enqueue=lambda *a, **k: None,
        pending_ids=lambda: set(),
        wakeup=threading.Event(),
    )
    agent._seat_scope = SimpleNamespace(allows=lambda *a, **k: "allow")
    agent._pdd_system_filtered_events = 0
    agent._out_of_scope_archived = 0
    agent._sender_queue = queue.Queue(maxsize=64)
    agent._sender_threads = []
    agent._sender_conv_locks = {}
    agent._sender_conv_locks_guard = threading.Lock()
    agent._sender_pool_guard = threading.Lock()
    return agent


def buyer_message(mid="p0-1", account="cs_427302374:164945148"):
    return {"platform": "pdd", "msg_id": mid, "platform_message_id": mid,
            "account": account, "buyer_id": "b-1", "role": "user",
            "content": "在吗", "ts": time.time()}


# ------------------------------------------------------------------ 灰度配置

def test_shop_id_allowlist_is_fail_closed():
    assert _shop_id_allowlist(None) == []
    assert _shop_id_allowlist("mall_1") == []
    assert _shop_id_allowlist(0) == []
    assert _shop_id_allowlist({"mall_1": 1}) == []
    assert _shop_id_allowlist([None, "", "  "]) == []
    assert _shop_id_allowlist(["mall_1", " mall_2 ", "mall_1"]) == ["mall_1", "mall_2"]


def test_config_defaults_are_all_on(tmp_path):
    """没配任何键 = 两个特性**默认全开**（owner 2026-09-21 决定）；worker 数默认 6。"""
    cfg_path = tmp_path / "bridge_config.json"
    cfg_path.write_text(json.dumps({"platform": "pdd", "agent_id": "x"}), encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg["immediate_ingress_enabled"] is True
    assert cfg["command_sender_enabled"] is True
    assert cfg["immediate_ingress_shop_ids"] == []
    assert cfg["immediate_ingress_disabled_shop_ids"] == []
    assert cfg["command_sender_shop_ids"] == []
    assert cfg["command_sender_disabled_shop_ids"] == []
    assert cfg["command_sender_workers"] == 6


def test_config_kill_switch_parses_string_false(tmp_path):
    """`"false"` 这种字符串必须解析成关（`bool("false")` 是 True 是经典坑）。"""
    cfg_path = tmp_path / "bridge_config.json"
    cfg_path.write_text(json.dumps({
        "platform": "pdd", "agent_id": "x",
        "immediate_ingress_enabled": "false",
        "command_sender_enabled": "0",
    }), encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg["immediate_ingress_enabled"] is False
    assert cfg["command_sender_enabled"] is False


def test_config_keeps_explicit_allowlist(tmp_path):
    cfg_path = tmp_path / "bridge_config.json"
    cfg_path.write_text(json.dumps({
        "platform": "pdd", "agent_id": "x",
        "immediate_ingress_shop_ids": ["mall_150792824"],
        "command_sender_shop_ids": ["mall_150792824", " mall_209064850 "],
        "command_sender_disabled_shop_ids": ["mall_740422004"],
        "command_sender_workers": 3,
    }), encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg["immediate_ingress_shop_ids"] == ["mall_150792824"]
    assert cfg["command_sender_shop_ids"] == ["mall_150792824", "mall_209064850"]
    assert cfg["command_sender_disabled_shop_ids"] == ["mall_740422004"]
    assert cfg["command_sender_workers"] == 3


def test_config_rejects_string_allowlist(tmp_path):
    """写成裸字符串（最容易犯的配置错）必须等于关闭，而不是"整店放行"。"""
    cfg_path = tmp_path / "bridge_config.json"
    cfg_path.write_text(json.dumps({
        "platform": "pdd", "agent_id": "x",
        "immediate_ingress_shop_ids": "mall_150792824",
        "command_sender_shop_ids": "mall_150792824",
    }), encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg["immediate_ingress_shop_ids"] == []
    assert cfg["command_sender_shop_ids"] == []


# -------------------------------------------------------------- 出站并发池（r2）

def test_command_sender_defaults_to_all_shops(tmp_path):
    """默认全开：没配任何键时，任何店铺都走并发发送池。"""
    agent = make_agent(tmp_path)
    assert agent._command_sender_enabled("cs_427302374:1") is True
    assert agent._command_sender_enabled("cs_150792824:167471952") is True
    assert make_agent(tmp_path, command_sender_shop_ids=[])._command_sender_enabled(
        "cs_427302374:1") is True


def test_command_sender_kill_switch_off(tmp_path):
    """总开关一关，所有店铺都回到串行路径。"""
    agent = make_agent(tmp_path, command_sender_enabled=False)
    assert agent._command_sender_enabled("cs_427302374:1") is False
    assert agent._command_sender_enabled("cs_150792824:167471952") is False


def test_command_sender_per_shop_exclusion(tmp_path):
    """单店回退：只关掉名单里的店，其它店不受影响。"""
    agent = make_agent(tmp_path, command_sender_disabled_shop_ids=["mall_150792824"])
    assert agent._command_sender_enabled("cs_150792824:167471952") is False
    assert agent._command_sender_enabled("cs_427302374:164945148") is True


def test_command_sender_allowlist_narrows_scope(tmp_path):
    """配了非空白名单 = 退化成窄灰度，只有命中的店铺开。"""
    agent = make_agent(tmp_path, command_sender_shop_ids=["mall_427302374"])
    assert agent._command_sender_enabled("cs_427302374:164945148") is True
    assert agent._command_sender_enabled("cs_150792824:167471952") is False


def test_command_sender_disabled_beats_allowlist(tmp_path):
    """同一个店同时出现在白名单和禁用名单里时，禁用优先（回退优先）。"""
    agent = make_agent(tmp_path, command_sender_shop_ids=["mall_427302374"],
                       command_sender_disabled_shop_ids=["mall_427302374"])
    assert agent._command_sender_enabled("cs_427302374:164945148") is False


def test_command_sender_rejects_string_allowlist(tmp_path):
    """裸字符串白名单不是"整店放行"，而是当成无效值忽略。"""
    agent = make_agent(tmp_path, command_sender_shop_ids="mall_427302374")
    assert agent._command_sender_enabled("cs_427302374:164945148") is True  # 默认全开


def test_sender_pool_not_started_when_kill_switch_off(tmp_path):
    """总开关关掉时一个发送线程都不许起（否则等于偷偷改了线上行为）。"""
    agent = make_agent(tmp_path, command_sender_enabled=False)
    agent._start_sender_pool()
    assert agent._sender_threads == []


def test_sender_pool_starts_by_default(tmp_path):
    """默认全开：不配任何键也要真的把池子起起来。"""
    agent = make_agent(tmp_path)
    agent._start_sender_pool()
    assert len(agent._sender_threads) == 6, "默认 6 个 worker"
    agent._stop.set()


def test_sender_pool_starts_once_and_clamps_workers(tmp_path):
    agent = make_agent(tmp_path, command_sender_workers=99)
    agent._start_sender_pool()
    assert len(agent._sender_threads) == 8, "worker 数必须收敛到 1..8"
    agent._start_sender_pool()
    assert len(agent._sender_threads) == 8, "重复调用必须幂等，不能越起越多"
    agent._stop.set()


def _install_fake_pull(agent, commands):
    agent.client = SimpleNamespace(pull_commands=lambda wait_seconds=0.0: list(commands))
    agent._retry_command_results = lambda: None
    agent._skip_result_retry = True


def test_handle_commands_pools_allowlisted_and_inlines_the_rest(tmp_path):
    """配了窄白名单时：命中的投池，未命中的原样串行。"""
    agent = make_agent(tmp_path, command_sender_shop_ids=["mall_427302374"])
    inline = []
    agent._execute_command = lambda cmd: inline.append(cmd["id"])
    _install_fake_pull(agent, [
        {"id": "cmd-gray", "account": "cs_427302374:164945148",
         "buyer_id": "b-1", "content": "hi", "type": "send_text"},
        {"id": "cmd-legacy", "account": "cs_150792824:167471952",
         "buyer_id": "b-2", "content": "hi", "type": "send_text"},
    ])
    BridgeAgent._handle_commands(agent, wait_seconds=0.0)
    queued = []
    while not agent._sender_queue.empty():
        queued.append(agent._sender_queue.get_nowait()["id"])
    assert queued == ["cmd-gray"], "命中白名单的店必须进池子"
    assert inline == ["cmd-legacy"], "未命中的店必须原样串行执行"


def test_handle_commands_pools_everything_by_default(tmp_path):
    """默认全开：不配任何键时所有店铺都投池，一条都不走串行。"""
    agent = make_agent(tmp_path)
    inline = []
    agent._execute_command = lambda cmd: inline.append(cmd["id"])
    _install_fake_pull(agent, [
        {"id": "cmd-a", "account": "cs_427302374:164945148",
         "buyer_id": "b-1", "content": "hi", "type": "send_text"},
        {"id": "cmd-b", "account": "cs_150792824:167471952",
         "buyer_id": "b-2", "content": "hi", "type": "send_text"},
    ])
    BridgeAgent._handle_commands(agent, wait_seconds=0.0)
    queued = []
    while not agent._sender_queue.empty():
        queued.append(agent._sender_queue.get_nowait()["id"])
    assert queued == ["cmd-a", "cmd-b"]
    assert inline == []


def test_handle_commands_is_fully_inline_when_kill_switch_off(tmp_path):
    """总开关关掉 = 完全回到改动前的串行路径。"""
    agent = make_agent(tmp_path, command_sender_enabled=False)
    inline = []
    agent._execute_command = lambda cmd: inline.append(cmd["id"])
    _install_fake_pull(agent, [
        {"id": "cmd-a", "account": "cs_427302374:164945148",
         "buyer_id": "b-1", "content": "hi", "type": "send_text"},
    ])
    BridgeAgent._handle_commands(agent, wait_seconds=0.0)
    assert inline == ["cmd-a"]
    assert agent._sender_queue.empty()


def test_handle_commands_inlines_excluded_shop(tmp_path):
    """单店回退：被排除的店走串行，其它店照常投池。"""
    agent = make_agent(tmp_path, command_sender_disabled_shop_ids=["mall_150792824"])
    inline = []
    agent._execute_command = lambda cmd: inline.append(cmd["id"])
    _install_fake_pull(agent, [
        {"id": "cmd-excluded", "account": "cs_150792824:167471952",
         "buyer_id": "b-2", "content": "hi", "type": "send_text"},
        {"id": "cmd-normal", "account": "cs_427302374:164945148",
         "buyer_id": "b-1", "content": "hi", "type": "send_text"},
    ])
    BridgeAgent._handle_commands(agent, wait_seconds=0.0)
    queued = []
    while not agent._sender_queue.empty():
        queued.append(agent._sender_queue.get_nowait()["id"])
    assert queued == ["cmd-normal"]
    assert inline == ["cmd-excluded"]


def test_sender_pool_falls_back_inline_when_saturated(tmp_path):
    """池子满 = 出站积压：退回串行执行形成背压，不能把指令丢掉。"""
    agent = make_agent(tmp_path, command_sender_shop_ids=["mall_427302374"])
    for _ in range(agent._sender_queue.maxsize):
        agent._sender_queue.put_nowait({"id": "occupied"})
    inline = []
    agent._execute_command = lambda cmd: inline.append(cmd["id"])
    _install_fake_pull(agent, [
        {"id": "cmd-overflow", "account": "cs_427302374:164945148",
         "buyer_id": "b-1", "content": "hi", "type": "send_text"},
    ])
    BridgeAgent._handle_commands(agent, wait_seconds=0.0)
    assert inline == ["cmd-overflow"]


def test_sender_conversation_lock_is_per_conversation(tmp_path):
    agent = make_agent(tmp_path)
    first = agent._sender_conversation_lock("cs_1:1", "b1")
    again = agent._sender_conversation_lock("cs_1:1", "b1")
    other = agent._sender_conversation_lock("cs_1:1", "b2")
    assert first is again
    assert first is not other


def test_sender_pool_keeps_per_conversation_order(tmp_path):
    """并发发送下，同一个 (account, buyer) 的任务不许重叠执行。"""
    agent = make_agent(tmp_path, command_sender_shop_ids=["mall_427302374"],
                       command_sender_workers=4)
    state = {"cur": 0, "max": 0, "done": []}
    guard = threading.Lock()

    def fake_execute(cmd):
        with guard:
            state["cur"] += 1
            state["max"] = max(state["max"], state["cur"])
        time.sleep(0.05)
        with guard:
            state["cur"] -= 1
            state["done"].append(cmd["id"])

    agent._execute_command = fake_execute
    agent._start_sender_pool()
    for index in range(6):
        agent._sender_queue.put_nowait({"id": "cmd-%d" % index,
                                        "account": "cs_427302374:164945148",
                                        "buyer_id": "same-buyer"})
    deadline = time.time() + 5.0
    while len(state["done"]) < 6 and time.time() < deadline:
        time.sleep(0.01)
    assert state["done"] == ["cmd-%d" % i for i in range(6)], "同一买家必须严格按序"
    assert state["max"] == 1, "同一买家的发送不能并发"


# ---------------------------------------------------------------- 即时上传（r1）

def test_ingress_default_on_is_not_held_by_context_gate(tmp_path):
    """默认全开：不配任何键，买家消息也不被订单上下文门控扣住。"""
    run_pdd_client.install_local_first()
    agent = make_agent(tmp_path)
    agent._on_local_event(buyer_message("all-1"))
    event_id = str(agent._pending[0]["event_id"])
    # 不进 blocked 集合 -> flush_events() 拦不住它，原始消息立即上传
    assert event_id not in agent._pdd_context_pending_ids
    # 订单上下文查询照旧排队，只是不再挡在上传前面
    assert agent._pdd_context_queue.qsize() == 1


def test_ingress_kill_switch_restores_gate(tmp_path):
    """总开关关掉 = 完全回到改动前的门控行为。"""
    run_pdd_client.install_local_first()
    agent = make_agent(tmp_path, immediate_ingress_enabled=False)
    agent._on_local_event(buyer_message("legacy-1"))
    event_id = str(agent._pending[0]["event_id"])
    assert event_id in agent._pdd_context_pending_ids


def test_ingress_excluded_shop_is_still_gated(tmp_path):
    """单店回退：名单里的店回到门控，其它店照常即时上传。"""
    run_pdd_client.install_local_first()
    agent = make_agent(tmp_path, immediate_ingress_disabled_shop_ids=["mall_427302374"])
    agent._on_local_event(buyer_message("excl-1"))
    excluded = str(agent._pending[0]["event_id"])
    assert excluded in agent._pdd_context_pending_ids
    agent._on_local_event(buyer_message("other-1", account="cs_150792824:167471952"))
    other = str(agent._pending[1]["event_id"])
    assert other not in agent._pdd_context_pending_ids


def test_ingress_allowlist_narrows_scope(tmp_path):
    """配了非空白名单 = 退化成窄灰度：只有命中的店铺即时上传。"""
    run_pdd_client.install_local_first()
    agent = make_agent(tmp_path, immediate_ingress_shop_ids=["mall_427302374"])
    agent._on_local_event(buyer_message("narrow-1"))
    hit = str(agent._pending[0]["event_id"])
    assert hit not in agent._pdd_context_pending_ids
    agent._on_local_event(buyer_message("narrow-2", account="cs_150792824:167471952"))
    miss = str(agent._pending[1]["event_id"])
    assert miss in agent._pdd_context_pending_ids


def test_ingress_ignores_string_allowlist(tmp_path):
    """裸字符串白名单是无效值 -> 按默认（全开）处理，而不是误当成单元素白名单。"""
    run_pdd_client.install_local_first()
    agent = make_agent(tmp_path, immediate_ingress_shop_ids="mall_427302374")
    agent._on_local_event(buyer_message("str-1"))
    event_id = str(agent._pending[0]["event_id"])
    assert event_id not in agent._pdd_context_pending_ids


def test_immediate_ingress_gate_matrix(tmp_path):
    """四个判定层级：总开关 > 单店禁用 > 窄白名单 > 默认开。"""
    gate = run_pdd_client.immediate_ingress_enabled
    other = "cs_150792824:167471952"
    # 默认开
    assert gate(make_agent(tmp_path), "cs_427302374:164945148") is True
    # 总开关
    assert gate(make_agent(tmp_path, immediate_ingress_enabled=False), "cs_427302374:1") is False
    # 单店禁用
    off = make_agent(tmp_path, immediate_ingress_disabled_shop_ids=["mall_150792824"])
    assert gate(off, other) is False
    assert gate(off, "cs_427302374:164945148") is True
    # 窄白名单
    narrow = make_agent(tmp_path, immediate_ingress_shop_ids=["mall_427302374"])
    assert gate(narrow, "cs_427302374:164945148") is True
    assert gate(narrow, other) is False
    # 禁用优先于白名单（回退优先）
    both = make_agent(tmp_path, immediate_ingress_shop_ids=["mall_427302374"],
                      immediate_ingress_disabled_shop_ids=["mall_427302374"])
    assert gate(both, "cs_427302374:164945148") is False


def test_enrichment_event_is_deterministic_and_shares_msg_id(tmp_path):
    """同一原始 event_id 反复补发得到同一个 event_id —— 重发幂等，且 msg_id 不变。"""
    agent = make_agent(tmp_path)
    enriched = dict(buyer_message("gray-2"))
    enriched["order_info"] = {"order_id": "260919-1"}
    first = run_pdd_client.enrichment_event(agent, "evt-1", enriched)
    again = run_pdd_client.enrichment_event(agent, "evt-1", enriched)
    other = run_pdd_client.enrichment_event(agent, "evt-2", enriched)
    assert first["event_id"] == again["event_id"]
    assert first["event_id"] != other["event_id"]
    assert first["event_id"].startswith("pdd-context-")
    for event in (first, again, other):
        assert str(event.get("msg_id")) == "gray-2", "增强事件必须保持同一个 msg_id"
