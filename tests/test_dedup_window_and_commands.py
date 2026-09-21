# -*- coding: utf-8 -*-
"""两处"静默失效"的回归钉子。

1. 判重窗口短于历史重发间隔 → 同一条消息被反复入队
2. 命令通道的两个静默点 → "中心没派发"和"轮询在报错"在日志里长得一样
"""
import json
import pathlib
import threading
import time
from collections import deque
from types import SimpleNamespace

import pytest

from bridge.agent import BridgeAgent
from bridge.client import BridgeClientError
from bridge.config import CONFIG_VERSION, load_config, relocate_paths_from_other_install
from bridge.pddbridge_source import PddbridgeSource, _DEDUP_WINDOW_SECONDS


# ============================================================ 1. 判重窗口

def make_source(**cfg_over):
    cfg = {"_history_only": True}
    cfg.update(cfg_over)
    return PddbridgeSource(cfg)


def test_window_defaults_to_floor_without_history_pull():
    src = make_source()
    assert src._dedup_window == _DEDUP_WINDOW_SECONDS


def test_window_grows_with_history_pull_interval():
    """窗口必须长于"同一条消息被重报的最长间隔"，那个间隔由历史补拉节奏决定。

    实测：history_pull_seconds=120 时，同一批消息每 ~243s 被重报一遍。
    原来窗口写死 120s → **每次都在重报之前就过期**，21 个 event_id 被重复入队 84 次。
    """
    src = make_source(history_pull_seconds=120)
    assert src._dedup_window > 243.0, "窗口必须盖得住实测的 243s 重报间隔"
    assert src._dedup_window == 600.0          # 120 × 5


def test_repeat_after_old_window_is_now_suppressed():
    """核心回归：间隔 243s 的重报，旧窗口(120s)放过，新窗口必须拦下。"""
    src = make_source(history_pull_seconds=120)
    now = time.time()

    # 第一次看到：不算重复
    assert src._seen_before("1789917788298") is False

    # 243 秒后重报（实测间隔）
    src._seen_any["1789917788298"] = now - 243.0
    assert src._seen_before("1789917788298") is True, \
        "243s 后的重报必须被判重拦下（旧窗口 120s 会放过它）"

    # 而超出新窗口之后，允许再次上报（窗口不能无限大）
    src._seen_any["1789917788298"] = now - src._dedup_window - 1.0
    assert src._seen_before("1789917788298") is False


def test_hit_refreshes_the_timestamp():
    """命中时必须刷新时间戳（和 _dedup 一致）。

    历史补拉是**按固定节奏反复重报同一条**的。不刷新的话窗口从"第一次上报"起算，
    累计出窗口就再漏一次 —— 实测 243s 重报 / 600s 窗口 = "压两条、漏一条"的锯齿。
    """
    src = make_source(history_pull_seconds=120)
    src._seen_any["m1"] = time.time() - 100.0
    stale = src._seen_any["m1"]

    assert src._seen_before("m1") is True
    assert src._seen_any["m1"] > stale, "命中时没刷新，累计到窗口外就会再漏一次"


def test_suppression_does_not_resurrect_by_accumulation():
    """上面那条的纯算法版：不依赖时钟打桩，直接算累计时间。

    旧行为（命中不刷新）：T=0 上报 → 243 拦 → 486 拦 → 729 **漏**。
    新行为（命中刷新）：永远拦。
    """
    window = 600.0
    for refresh_on_hit in (False, True):
        last = 0.0
        leaked = []
        for i in range(1, 6):
            t = 243.0 * i
            hit = (t - last) < window
            if hit and refresh_on_hit:
                last = t
            elif not hit:
                leaked.append(t)
                last = t
        label = "命中刷新(新)" if refresh_on_hit else "命中不刷新(旧)"
        assert (leaked == []) is refresh_on_hit, "%s 漏了 %s" % (label, leaked)


def test_explicit_window_overrides_auto():
    src = make_source(history_pull_seconds=120, dedup_window_seconds=45)
    assert src._dedup_window == 45.0


def test_dedup_off_still_wins():
    """dedup_mode=off 时窗口多大都不判重。"""
    src = make_source(history_pull_seconds=120, dedup_mode="off")
    src._seen_any["m1"] = time.time()
    assert src._seen_before("m1") is False


def test_window_is_visible_in_status():
    """"窗口配歪了"要能从状态里一眼看出来，不能只埋在日志里。"""
    src = make_source(history_pull_seconds=120)
    assert src.status()["dedup_window_seconds"] == 600.0


def _push_frame(msg_id, content="测试消息", ts=1789920746):
    return {"cmd": "push", "message": {
        "msg_id": msg_id, "content": content, "ts": ts, "type": 0,
        "from": {"role": "user", "uid": "buyer1", "csid": "cs_1:1"},
        "to": {"role": "mall_cs", "csid": "cs_1:1", "uid": "seat1"},
    }}


def _list_frame(msg_id, content="测试消息", ts=1789920746):
    return {"cmd": "list", "messages": [{
        "msg_id": msg_id, "content": content, "ts": ts, "type": 0,
        "from": {"role": "user", "uid": "buyer1", "csid": "cs_1:1"},
        "to": {"role": "mall_cs", "csid": "cs_1:1", "uid": "seat1"},
    }]}


def test_live_push_then_history_list_reports_once():
    """同一条消息：实时 push 报一次，历史补拉又报一次 —— 必须只出一遍。

    实测 v0.9.3 日志：8 个 event_id 有 **5 个被重复入队，间隔只有 15-18 秒**
    （远在 600s 判重窗口内）。243 秒那个重复源已经修了，这是**另一个**：
    实时路径和历史路径没共用同一把去重锁。
    """
    emitted = []
    src = PddbridgeSource({"report_all": True, "history_pull_seconds": 120,
                           "cdp_emit_history": False},
                          on_event=lambda m: emitted.append(m))

    M = "1789920746142"
    src._handle_frame({"t": 1789920746416, "dir": "in", "data": _push_frame(M)})
    assert len(emitted) == 1, "实时帧应该报 1 次"

    src._handle_frame({"t": 1789920764000, "dir": "in", "data": _list_frame(M)})
    assert len(emitted) == 1, \
        "同一条消息被历史补拉重报了一遍（判重没拦住）：共 %d 次" % len(emitted)


def test_history_list_twice_reports_once():
    """历史补拉自己重报两次也要拦住。"""
    emitted = []
    src = PddbridgeSource({"report_all": True, "history_pull_seconds": 120,
                           "cdp_emit_history": False},
                          on_event=lambda m: emitted.append(m))
    M = "1789920746999"
    src._handle_frame({"t": 1, "dir": "in", "data": _list_frame(M)})
    src._handle_frame({"t": 2, "dir": "in", "data": _list_frame(M)})
    assert len(emitted) == 1, "历史重报被放了 %d 次" % len(emitted)


# ============================================================ 1b. WS 重连退避

def _backoff_delays(establishes, monkeypatch, rounds=5):
    """跑 _run，把每次重连前 sleep 的间隔收集起来。

    establishes=True 模拟"握上手之后被断开"（网络抖动）；
    False 模拟"压根连不上"（服务端一直拒）。
    """
    import asyncio
    from bridge import center_ws as mod

    monkeypatch.setattr(mod, "RECONNECT_MIN_SECONDS", 1.0)
    monkeypatch.setattr(mod, "RECONNECT_MAX_SECONDS", 64.0)

    ch = mod.CenterEventChannel.__new__(mod.CenterEventChannel)
    ch._state_lock = threading.Lock()
    ch._stop = threading.Event()
    ch._connected = False
    ch._ws = None
    ch.last_error = ""
    ch.connected_at = 0.0
    ch._attempt_established = False
    ch.max_inflight = 0

    async def sess():
        if establishes:
            ch._attempt_established = True
        raise RuntimeError("keepalive ping timeout")

    ch._session = sess
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)
        if len(delays) >= rounds:
            ch._stop.set()

    real_sleep = asyncio.sleep
    monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)
    try:
        asyncio.run(ch._run())
    finally:
        monkeypatch.setattr(mod.asyncio, "sleep", real_sleep)
    return delays


def test_backoff_resets_when_a_connection_was_established(monkeypatch):
    """连上过再断 = 网络抖动，退避必须回到最小，不能一路翻倍到 60 秒。

    原来 `delay = RECONNECT_MIN_SECONDS` 只在 `_session()` **正常返回**时执行，
    而它只要断开就抛异常 —— 那个重置永远跑不到。实测日志里 8→16→32→60s 就是这么
    涨上去的：几轮抖动之后每次重连都固定等 60 秒，期间 WS 一直是断的。
    """
    delays = _backoff_delays(establishes=True, monkeypatch=monkeypatch)
    assert delays, "应该发生过重连"
    assert set(delays) == {1.0}, "抖动不该让退避增长，实际 %s" % delays


def test_backoff_still_grows_when_never_connected(monkeypatch):
    """反面：压根连不上时必须退避，否则会给服务端打请求风暴。"""
    delays = _backoff_delays(establishes=False, monkeypatch=monkeypatch)
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0], "没连上就该指数退避，实际 %s" % delays


# ============================================================ 2. 命令通道可见性

def bare_agent():
    """**故意不设** _cmd_stats / _cmd_warn_at。

    测试里普遍用 `object.__new__(BridgeAgent)` 跳过 __init__，新加的实例属性必须
    能惰性兜底，否则整条命令处理会被 AttributeError 打挂（v0.9.0 接 WS 时踩过一次，
    v0.9.2 加计数时又踩了一次）。
    """
    agent = object.__new__(BridgeAgent)
    agent.cfg = {"agent_id": "offline-test", "dry_run": False}
    agent.platform = SimpleNamespace(name="pdd")
    agent._pending = deque()
    agent._pending_lock = threading.Lock()
    agent._commands_done = set()
    agent._last_error = ""
    agent._retry_command_results = lambda: None
    return agent


def test_bare_agent_without_init_does_not_crash():
    """回归钉子：跳过 __init__ 的 agent 走一遍命令处理不能抛异常。"""
    agent = bare_agent()
    agent.client = SimpleNamespace(pull_commands=lambda **_k: [])
    assert not hasattr(agent, "_cmd_stats")
    agent._handle_commands(wait_seconds=0.0)      # 不能抛
    assert agent._cmd_counters()["poll_ok"] == 1


def test_pull_failure_is_counted_and_logged(caplog):
    """长轮询失败原来是**完全静默**的：只写内存不写日志。

    于是"中心没派发"和"轮询在报错"在日志里长得一模一样 —— 实测因此查了很久。
    """
    agent = bare_agent()

    def boom(*_a, **_k):
        raise BridgeClientError("connection refused")

    agent.client = SimpleNamespace(pull_commands=boom)

    with caplog.at_level("WARNING"):
        agent._handle_commands(wait_seconds=0.0)

    assert agent._cmd_stats["poll_fail"] == 1
    assert "命令长轮询失败" in caplog.text, "轮询失败必须留下日志"
    assert "connection refused" in caplog.text


def test_pull_failure_log_is_throttled(caplog):
    """轮询每 1.5s 一次，失败不能刷屏。"""
    agent = bare_agent()

    def boom(*_a, **_k):
        raise BridgeClientError("down")

    agent.client = SimpleNamespace(pull_commands=boom)

    with caplog.at_level("WARNING"):
        for _ in range(20):
            agent._handle_commands(wait_seconds=0.0)

    assert agent._cmd_stats["poll_fail"] == 20
    assert caplog.text.count("命令长轮询失败") == 1, "应节流成一条"


def test_command_without_id_is_counted_and_logged(caplog):
    """缺 id 的指令原来是直接 continue、一个字都不记。

    中心换了字段名 → 所有指令静默消失，日志上完全看不出来。
    """
    agent = bare_agent()
    agent.client = SimpleNamespace(pull_commands=lambda **_k: [
        {"type": "send_text", "content": "无 id 的指令", "buyer_id": "b1"},
    ])

    with caplog.at_level("WARNING"):
        agent._handle_commands(wait_seconds=0.0)

    assert agent._cmd_stats["no_id"] == 1
    assert agent._cmd_stats["received"] == 0
    assert "没有 id/command_id" in caplog.text
    assert "无 id 的指令" in caplog.text, "要打出指令内容，方便定位中心字段名"


def test_ok_poll_is_counted():
    agent = bare_agent()
    agent.client = SimpleNamespace(pull_commands=lambda **_k: [])
    agent._handle_commands(wait_seconds=0.0)
    assert agent._cmd_stats["poll_ok"] == 1
    assert agent._cmd_stats["poll_fail"] == 0


def test_command_stats_are_in_the_status_payload():
    """received=0 且 poll_fail 在涨 → 轮询出错；poll_ok 在涨 → 中心没派发。
    这两个数要能直接从心跳里读出来。"""
    agent = bare_agent()
    agent._stop = threading.Event()
    agent._status_payload_core = None
    # 直接查键是否存在，避免搭整个 _status_payload 的依赖
    import inspect
    src = inspect.getsource(BridgeAgent._status_payload)
    assert '"commands"' in src


# ============================================================ 2b. CDP 扫描不能被静默吞掉

def test_find_socketutil_sessions_actually_scans_targets(monkeypatch):
    """端到端跑一遍扫描：必须真的扫到 socketUtil 上下文。

    v0.9.2 挂过一次：`enable_runtime` 的参数从 collect_seconds 改名成
    deadline_seconds，但 `find_socketutil_sessions` 里的调用点没跟着改 →
    TypeError → 被那里一个笼统的 `except Exception: continue` 吞掉 →
    每个 target 都被静默跳过 → hits=[] → 日志报"CDP 未就绪, 自动降级"，
    而**日志里一个错字都没有**。这个测试就是钉住那条链路。
    """
    from bridge.pddbridge import cdp as C

    calls = []

    monkeypatch.setattr(C, "find_chat_targets", lambda port: [
        {"webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/page/x",
         "url": "https://mms.pinduoduo.com/workbench/notification?tab=conversation",
         "id": "t1", "title": "chat"},
    ])

    class FakeWs:
        """按 method 应答：Runtime.enable 回一个 context，Runtime.evaluate 回真值。"""

        def __init__(self):
            self.timeout = None
            self._seq = 0

        def settimeout(self, v):
            self.timeout = v

        def send(self, payload):
            req = json.loads(payload)
            self._pending = req
            calls.append(req.get("method"))

        def recv(self):
            req = self._pending
            if req.get("method") == "Runtime.enable":
                if not getattr(self, "_ctx_sent", False):
                    self._ctx_sent = True
                    return json.dumps({"method": "Runtime.executionContextCreated",
                                       "params": {"context": {"id": 11, "name": "main"}}})
                return json.dumps({"id": req["id"], "result": {}})
            # Runtime.evaluate("!!window.socketUtil")
            return json.dumps({"id": req["id"],
                               "result": {"result": {"type": "boolean", "value": True}}})

        def close(self):
            pass

    monkeypatch.setattr(C.websocket, "create_connection", lambda *a, **k: FakeWs())

    hits = C.find_socketutil_sessions(57165)

    assert len(hits) == 1, "应该扫到 1 个会话（扫到 0 个 = 又被静默吞了）"
    assert hits[0]["cid"] == 11
    assert "Runtime.enable" in calls and "Runtime.evaluate" in calls


def test_programming_errors_are_not_swallowed_by_the_scan(monkeypatch):
    """签名不对这类编程错误必须抛出去，不能被当成"这个 target 不可用"。"""
    from bridge.pddbridge import cdp as C

    monkeypatch.setattr(C, "find_chat_targets", lambda port: [
        {"webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/page/x",
         "url": "u", "id": "t1", "title": "t"},
    ])
    monkeypatch.setattr(C.websocket, "create_connection", lambda *a, **k: object())

    class Boom:
        def __init__(self, *a, **k):
            pass

        def enable_runtime(self, *, deadline_seconds=3.0):
            raise TypeError("模拟签名不匹配")

        def contexts(self):
            return {}

        def close(self):
            pass

    monkeypatch.setattr(C, "Cdp", Boom)
    with pytest.raises(TypeError):
        C.find_socketutil_sessions(57165)


# ============================================================ 3. 路径串目录

def _write_config(d, payload):
    d.mkdir(parents=True, exist_ok=True)
    p = d / "bridge_config.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def test_paths_pointing_at_another_install_are_relocated(tmp_path, caplog):
    old = tmp_path / "old-install"
    new = tmp_path / "new-install"
    _write_config(old, {"platform": "pdd", "agent_token": "t"})
    _write_config(new, {"platform": "pdd", "agent_token": "t"})

    data = {
        "local_queue_path": str(old / "bridge_queue_pdd.jsonl"),
        "command_journal_path": str(old / "bridge_commands_pdd.json"),
    }

    with caplog.at_level("WARNING"):
        moved = relocate_paths_from_other_install(new / "bridge_config.json", data)

    assert sorted(moved) == ["command_journal_path", "local_queue_path"]
    assert data["local_queue_path"] == str(new / "bridge_queue_pdd.jsonl")
    assert data["command_journal_path"] == str(new / "bridge_commands_pdd.json")
    assert "指向另一套安装" in caplog.text


def test_shared_directory_is_left_alone(tmp_path):
    """用户故意把队列放到共享目录 —— 那目录里没有 bridge_config.json，不许动。"""
    shared = tmp_path / "shared"
    new = tmp_path / "new-install"
    shared.mkdir()
    _write_config(new, {"platform": "pdd", "agent_token": "t"})

    data = {"local_queue_path": str(shared / "bridge_queue_pdd.jsonl")}
    moved = relocate_paths_from_other_install(new / "bridge_config.json", data)

    assert moved == []
    assert data["local_queue_path"] == str(shared / "bridge_queue_pdd.jsonl")


def test_paths_already_in_own_dir_are_untouched(tmp_path):
    new = tmp_path / "new-install"
    _write_config(new, {"platform": "pdd", "agent_token": "t"})
    data = {"local_queue_path": str(new / "bridge_queue_pdd.jsonl")}

    assert relocate_paths_from_other_install(new / "bridge_config.json", data) == []


def test_load_config_repairs_cross_install_paths(tmp_path, caplog):
    """端到端：拷进来的老配置，加载时就该被纠正并告警。"""
    old = tmp_path / "old-install"
    new = tmp_path / "new-install"
    _write_config(old, {"platform": "pdd", "agent_token": "t"})
    cfg_path = _write_config(new, {
        "platform": "pdd",
        "agent_token": "t",
        "config_version": CONFIG_VERSION,      # 版本号已是最新，迁移不会跑
        "local_queue_path": str(old / "bridge_queue_pdd.jsonl"),
    })

    with caplog.at_level("WARNING"):
        cfg = load_config(cfg_path)

    assert cfg["local_queue_path"] == str(new / "bridge_queue_pdd.jsonl")
    assert "指向另一套安装" in caplog.text


def test_migration_persists_the_relocation(tmp_path):
    """版本号落后时走迁移，纠正结果要**落盘**（否则每次启动都要重纠一遍）。"""
    old = tmp_path / "old-install"
    new = tmp_path / "new-install"
    _write_config(old, {"platform": "pdd", "agent_token": "t"})
    cfg_path = _write_config(new, {
        "platform": "pdd",
        "agent_token": "t",
        "local_queue_path": str(old / "bridge_queue_pdd.jsonl"),
    })

    load_config(cfg_path)

    saved = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    assert saved["local_queue_path"] == str(new / "bridge_queue_pdd.jsonl")
