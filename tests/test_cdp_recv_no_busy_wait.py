# -*- coding: utf-8 -*-
"""CDP 求值：不能忙等、不能突破调用方给的超时预算。

原实现：

    end = time.time() + timeout
    while time.time() < end:
        try:
            m = json.loads(self.ws.recv())
        except Exception:
            continue

两个问题，都在 10 店铺场景放大：
  1. `except Exception: continue` —— 连接已断时 recv **立刻**抛，于是变成全速
     空转把 CPU 烧满，直到 deadline 才罢休；
  2. recv 的超时用的是**连接超时**（默认 8s），不是这里的 timeout ——
     调用方给 4s，实际能阻塞 8 秒。串行轮 10 个会话就是这么攒出来的。
"""
import json
import time

import pytest
import websocket

from bridge.pddbridge.cdp import DEFAULT_EVAL_TIMEOUT, Cdp


class FakeWs:
    """可控的 recv：按脚本返回，或抛超时 / 断连。

    `block_when_exhausted=True` 时，脚本用完后 recv 会**睡满 settimeout 设的值**再抛
    超时 —— 和真 socket 一样。这个差别很关键：如果超时是"立刻抛"，那么
    `elapsed < 0.5` 这类断言对"收满 3 秒"的坏实现也会通过，测试就是假绿的
    （实测踩过：回归版只花 0.00s，断言照样绿）。
    """

    def __init__(self, script=None, raise_on_recv=None, block_when_exhausted=False):
        self.script = list(script or [])
        self.raise_on_recv = raise_on_recv
        self.block_when_exhausted = block_when_exhausted
        self.recv_calls = 0
        self.send_calls = []
        self.timeout = None

    def settimeout(self, value):
        self.timeout = value

    def send(self, payload):
        self.send_calls.append(json.loads(payload))

    def recv(self):
        self.recv_calls += 1
        if self.raise_on_recv is not None:
            raise self.raise_on_recv
        if self.script:
            return self.script.pop(0)
        # 脚本用完 = 真的没有更多数据了。真 socket 会等满超时才抛。
        if self.block_when_exhausted and self.timeout:
            time.sleep(self.timeout)
        raise websocket.WebSocketTimeoutException("timeout")

    def close(self):
        pass


def make_cdp(ws):
    cdp = Cdp.__new__(Cdp)          # 跳过真实连接，只装我们关心的状态
    cdp.ws = ws
    cdp.timeout = 8.0
    cdp._seq = 0
    cdp._contexts = {}
    return cdp


# ---------------------------------------------------------------- 不能忙等
def test_closed_connection_does_not_spin():
    """连接已断：必须**立刻抛**，不能自旋到 deadline。

    原来这里会全速转 4 秒 —— 10 个店铺同时断线时能把工作台拖卡。
    """
    ws = FakeWs(raise_on_recv=websocket.WebSocketConnectionClosedException("closed"))
    cdp = make_cdp(ws)

    started = time.monotonic()
    with pytest.raises(websocket.WebSocketConnectionClosedException):
        cdp.eval("1+1", 1, timeout=4.0)
    elapsed = time.monotonic() - started

    assert elapsed < 0.5, "应该立刻返回，实际耗时 %.2fs" % elapsed
    assert ws.recv_calls == 1, "断连后不该反复重试（实际 %d 次）" % ws.recv_calls


def test_empty_recv_is_treated_as_closed():
    """recv 返回空 = 流已结束，继续重试没有意义。"""
    ws = FakeWs(script=[""])
    cdp = make_cdp(ws)

    with pytest.raises(websocket.WebSocketConnectionClosedException):
        cdp.eval("1+1", 1, timeout=4.0)

    assert ws.recv_calls == 1


def test_instant_timeout_does_not_spin():
    """超时"等不满 budget"时不许 continue —— 那正是自旋的来源。"""
    ws = FakeWs(block_when_exhausted=False)      # 每次 recv 立刻抛超时
    cdp = make_cdp(ws)

    started = time.monotonic()
    result = cdp.eval("1+1", 1, timeout=4.0)
    elapsed = time.monotonic() - started

    assert result is None
    assert elapsed < 0.5, "立刻抛的超时不该被当成'还没到点'而反复重试（耗时 %.2fs）" % elapsed
    assert ws.recv_calls <= 2, "实际重试了 %d 次" % ws.recv_calls


def test_real_timeout_still_waits_out_the_budget():
    """反过来：socket 真的按超时阻塞时，必须等满预算才放弃 —— 不能提前交还。

    这条是上一条的反面。少了它，把 `waited < budget * 0.5` 那句写成"永远提前返回"
    也能让上面的测试通过，而实际效果是 4 秒的预算只等 0 秒就报超时。
    """
    ws = FakeWs(block_when_exhausted=True)
    cdp = make_cdp(ws)

    started = time.monotonic()
    result = cdp.eval("1+1", 1, timeout=1.0)
    elapsed = time.monotonic() - started

    assert result is None
    assert 0.9 <= elapsed < 1.6, "应按预算等满 1.0s，实际 %.2fs" % elapsed


# ---------------------------------------------------------------- 遵守预算
def test_recv_uses_remaining_budget_not_connect_timeout():
    """单次 recv 的 socket 超时必须是 min(剩余预算, 连接超时)，否则预算形同虚设。"""
    ws = FakeWs(block_when_exhausted=True)
    cdp = make_cdp(ws)
    cdp.timeout = 8.0                  # 连接超时 8s

    cdp.eval("1+1", 1, timeout=1.0)

    assert ws.timeout is not None
    assert ws.timeout <= 1.0, "recv 超时被设成 %s，突破了 1.0s 的预算" % ws.timeout


# ---------------------------------------------------------------- 正常路径
def test_returns_matching_reply():
    ws = FakeWs(script=[json.dumps({"id": 1, "result": {"result": {"value": 42}}})])
    cdp = make_cdp(ws)

    reply = cdp.eval("40+2", 1)

    assert reply == 42


def test_collects_execution_contexts_while_waiting():
    """等应答的路上要顺手收 context 事件（扫描阶段全靠它）。"""
    ws = FakeWs(script=[
        json.dumps({"method": "Runtime.executionContextCreated",
                    "params": {"context": {"id": 7, "name": "main"}}}),
        json.dumps({"id": 1, "result": {"result": {"value": "ok"}}}),
    ])
    cdp = make_cdp(ws)

    assert cdp.eval("1", 1) == "ok"
    assert cdp.contexts() == {7: {"id": 7, "name": "main"}}


def test_ignores_other_ids_and_junk_frames():
    """其它 id 的应答 / 非 JSON / 非 dict 的帧要跳过，不能误当成本次应答。"""
    ws = FakeWs(script=[
        "not json at all",
        json.dumps([1, 2, 3]),
        json.dumps({"id": 99, "result": {"result": {"value": "别人的"}}}),
        json.dumps({"id": 1, "result": {"result": {"value": "我的"}}}),
    ])
    cdp = make_cdp(ws)

    assert cdp.eval("1", 1) == "我的"


def test_enable_runtime_collects_contexts_before_the_reply():
    """Chrome 先推 executionContextCreated、最后才回命令结果 —— 收齐即返回。"""
    ws = FakeWs(script=[
        json.dumps({"method": "Runtime.executionContextCreated",
                    "params": {"context": {"id": 11, "name": "main"}}}),
        json.dumps({"id": 1, "result": {}}),                     # 应答最后到
    ], block_when_exhausted=True)
    cdp = make_cdp(ws)

    contexts = cdp.enable_runtime(deadline_seconds=3.0)

    assert 11 in contexts


def test_enable_runtime_returns_on_reply_not_full_window():
    """**收到应答就返回**，不能等满窗口。

    这是 v0.9.1 开发中我自己引入又抓回来的回归：改成"收满 3 秒"之后，重扫阶段
    每个标签页白等 3 秒 —— 10 个店铺就是 30 秒，主循环整段堵死、谁都别想收消息。
    3.0 秒的 deadline 只对"一直不应答的坏页面"生效。
    """
    # block_when_exhausted=True：脚本用完后的 recv 会真的睡满超时。
    # 少了这个，"收满窗口"的坏实现只花 0.00s 就返回，elapsed 断言照样绿。
    ws = FakeWs(script=[json.dumps({"id": 1, "result": {}})], block_when_exhausted=True)
    cdp = make_cdp(ws)

    started = time.monotonic()
    cdp.enable_runtime(deadline_seconds=3.0)
    elapsed = time.monotonic() - started

    assert ws.recv_calls == 1, "收到应答后不该继续 recv（实际 %d 次）" % ws.recv_calls
    assert elapsed < 0.5, "应答已到就该返回，实际等了 %.2fs（收满窗口是回归）" % elapsed


def test_exception_details_are_reported_not_raised():
    ws = FakeWs(script=[json.dumps({
        "id": 1,
        "result": {"exceptionDetails": {"text": "ReferenceError: x is not defined"}},
    })])
    cdp = make_cdp(ws)

    result = cdp.eval("x", 1)

    assert result == {"__exception__": "ReferenceError: x is not defined"}


def test_default_eval_timeout_is_not_the_connect_timeout():
    """默认值就是 4s，不能再回到 8s 的连接超时。"""
    assert DEFAULT_EVAL_TIMEOUT <= 4.0
