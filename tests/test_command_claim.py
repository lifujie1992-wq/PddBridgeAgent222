# -*- coding: utf-8 -*-
"""命令并发轮询的原子认领。

实测（2026-09-21，直连中心）：
    TCP 握手     0.015s
    /commands    9.14s / 9.16s / 9.19s   ← 固定 ~9.15 秒开销，与 wait_seconds 无关
    wait_seconds=1.5 -> 11.6s

桥接因此每 10.7 秒才能取一次指令；大脑的回复若在取到之前作废，就"发不出去"。
对策是并发开 N 条轮询把间隔压下来。

**并发的前提是原子认领**：多条轮询可能同时拿到同一条指令，
不认领就会**把同一条回复发给买家两次** —— 这个文件钉住它。
"""
import threading
from collections import deque
from types import SimpleNamespace

from bridge.agent import BridgeAgent


def bare_agent():
    """跳过 __init__ 造 agent（仓库既有约定；新属性必须惰性兜底）。"""
    agent = object.__new__(BridgeAgent)
    agent.cfg = {}
    agent._commands_done = set()
    return agent


def test_same_command_can_only_be_claimed_once():
    """**核心**：同一条指令只能被认领一次 —— 并发时另一条轮询必须拿不到。"""
    agent = bare_agent()
    assert agent._claim_command("cmd-1") is True
    assert agent._claim_command("cmd-1") is False, "重复认领会把回复发两遍"


def test_already_done_command_cannot_be_claimed():
    """本机已处理过的指令不能再认领（重连后中心重投的老指令）。"""
    agent = bare_agent()
    agent._commands_done.add("cmd-2")
    assert agent._claim_command("cmd-2") is False


def test_concurrent_claims_only_one_wins():
    """真并发下**有且只有一个**赢 —— 多线程竞争同一批 id。"""
    agent = bare_agent()
    winners = []
    lock = threading.Lock()
    start = threading.Barrier(8)

    def worker():
        start.wait()
        for index in range(50):
            if agent._claim_command("cmd-%d" % index):
                with lock:
                    winners.append("cmd-%d" % index)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(winners, key=lambda x: int(x.split("-")[1])) == ["cmd-%d" % i for i in range(50)], \
        "每个 id 必须恰好被认领一次，实际 %d 次" % len(winners)


def test_claim_set_stays_bounded():
    """认领集合必须有界，否则跑满一天班会涨到几十万条。"""
    agent = bare_agent()
    for index in range(2600):
        agent._claim_command("cmd-%d" % index)
    assert len(agent._command_claimed) <= 2000


def test_bare_agent_without_init_does_not_crash():
    """惰性兜底：`object.__new__(BridgeAgent)` 上认领不能抛。"""
    agent = object.__new__(BridgeAgent)
    agent.cfg = {}
    agent._commands_done = set()
    assert agent._claim_command("x") is True      # 不能抛
