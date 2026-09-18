# -*- coding: utf-8 -*-
"""CDP 多账号会话 + 注入存活判定 + 推送通道 回归。

背景:
- 老实现只 eval `drain ? drain() : []`, 页面重载/换页后注入消失会静默返回 [], 表现为
  「队列常年 pending=0、无任何告警、连 rescan 都没有」。
- 老实现只取 hits[0] 并在第一个成功端口上 return: 一个客服挂多个店铺账号时, 第二个账号
  的消息会整段静默消失。
- 每条帧都必须能说清去向: 推送成功不进缓冲、推送失败补缓冲、溢出/判重/未识别都要计数。
"""
from __future__ import annotations

import json
import queue
import shutil
import subprocess
import sys
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from bridge import pddbridge_source
from bridge.config import resolve_log_dir
from bridge.pddbridge_source import PddbridgeSource, SCANNING, _Session

ROOT = Path(__file__).resolve().parent.parent
PUSH = json.dumps({
    "response": "push",
    "message": {
        "from": {"role": "user", "uid": "4764375385604", "csid": "cs_12345:678"},
        "to": {"role": "mall_cs"},
        "msg_id": "m1", "content": "你好", "type": 0, "ts": 1756000000000,
    },
})
PUSH_NO_CSID = json.dumps({
    "response": "push",
    "message": {
        "from": {"role": "user", "uid": "4764375385604"},
        "to": {"role": "mall_cs", "uid": "427302374"},
        "msg_id": "m2", "content": "老板你好", "type": 0, "ts": 1756000000001,
    },
})


class FakeCdp:
    """最小替身: 按表达式区分 drain / 注入 / 账号信息。"""

    def __init__(self, hooked=True, frames=None, stats=None, inject_ok=True, info=None,
                 raises=None):
        self.hooked = hooked
        self.frames = frames if frames is not None else []
        self.stats = stats
        self.inject_ok = inject_ok
        self.info = info
        self.raises = raises
        self.closed = False

    def eval(self, expression, context_id=None):
        if self.raises:
            raise self.raises
        if "__pddBridge_push_url =" in expression:              # 注入脚本（含前置变量）
            return {"ok": True, "attached": 1} if self.inject_ok else {"ok": False}
        if "__pddBridge_hooked" in expression:                  # drain + 存活
            return {"hooked": self.hooked, "frames": self.frames, "stats": self.stats}
        if "__pddBridge_info" in expression:                    # 账号/店铺信息
            return self.info
        return None

    def close(self):
        self.closed = True


def make_session(port=57165, cid=1, url="https://mms.pinduoduo.com/workbench/notification",
                 **kwargs):
    return _Session(port, url, FakeCdp(**kwargs), cid)


def source_with(*sessions, on_event=None, cfg=None):
    src = PddbridgeSource(cfg or {}, on_event=on_event or (lambda m: None))
    src.sessions = list(sessions)
    src.state = pddbridge_source.LISTENING
    return src


# ---------------------------------------------------------------- 端口发现

def test_discover_ports_uses_listener_pid_when_cmdline_is_hidden(monkeypatch):
    """Windows 权限导致命令行不可读时，仍能按 PDD 进程的监听端口连接 CDP。"""
    process = types.SimpleNamespace(
        pid=4321,
        info={"name": "pddwebworkbench.exe", "cmdline": None},
        name=lambda: "pddwebworkbench.exe",
        cmdline=lambda: (_ for _ in ()).throw(PermissionError()),
    )
    conn = types.SimpleNamespace(
        pid=4321,
        status=pddbridge_source.pdd_cdp.psutil.CONN_LISTEN,
        laddr=types.SimpleNamespace(port=57166),
    )
    monkeypatch.setattr(pddbridge_source.pdd_cdp.psutil, "process_iter", lambda *args: iter([process]))
    monkeypatch.setattr(pddbridge_source.pdd_cdp.psutil, "net_connections", lambda **kwargs: [conn])
    assert 57166 in pddbridge_source.pdd_cdp.discover_ports(only_pdd=True)


# ---------------------------------------------------------------- 注入存活
def test_lost_hook_is_detected_and_counted():
    src = source_with(make_session(hooked=False, frames=None))
    assert src._pump_session(src.sessions[0]) == "lost"
    assert src.drops["hook_lost"] == 1


def test_legacy_inject_returning_bare_array_is_treated_as_lost():
    # 老注入脚本 drain 只返回数组 → 无法确认存活, 必须重新注入
    class LegacyCdp:
        def eval(self, expression, context_id=None):
            return [{"dir": "in", "data": PUSH}]

        def close(self):
            pass

    src = source_with(make_session())
    src.sessions[0].cdp = LegacyCdp()
    assert src._pump_session(src.sessions[0]) == "lost"
    assert src.drops["hook_lost"] == 1


def test_none_eval_is_reported_as_error_not_silent_empty():
    src = source_with(make_session(raises=None))
    src.sessions[0].cdp = type("C", (), {"eval": lambda self, e, c=None: None,
                                         "close": lambda self: None})()
    assert src._pump_session(src.sessions[0]) == "error"
    assert src.drops["hook_lost"] == 0


def test_healthy_hook_emits_message():
    rows = []
    src = source_with(make_session(frames=[{"dir": "in", "data": PUSH, "t": 1}]),
                      on_event=rows.append)
    assert src._pump_session(src.sessions[0]) == "ok"
    assert src.msg_count == 1 and rows and rows[0]["buyer_id"] == "4764375385604"


def test_js_counters_surface_and_do_not_double_count():
    stats = {"recorded": 1200, "drained": 1000, "dedup_skip": 2, "overflow_drop": 3,
             "exact_repeat": 7,
             "pushed": 900, "push_skip": 4, "push_retry": 5}
    session = make_session(stats=stats)
    src = source_with(session)
    assert src._pump_session(session) == "ok"
    assert (src.drops["buf_overflow"], src.drops["js_would_drop"]) == (3, 2)
    assert (src.drops["push_skip"], src.drops["push_retry"]) == (4, 5)
    assert src.drops["js_exact_repeat"] == 7
    assert session.push_ok is True
    assert src._pump_session(session) == "ok"          # 同一份累计值不重复计入
    assert src.drops["buf_overflow"] == 3


def test_consecutive_same_length_messages_all_emitted():
    """买家连发同长度消息: 一条都不能少。

    旧注入层按「长度+前120字符」判重, 同长度的连续消息只会剩下第 1 条（6→1）。
    """
    rows: list = []
    session = make_session(port=60001, cid=1, url="chat")
    session.account = "cs_12345:678"
    src = source_with(session, on_event=rows.append)
    frames = []
    for index in range(6):
        payload = ('{"response":"push","message":{"from":{"role":"user",'
                   '"uid":"4764375385604","csid":"cs_12345:678"},'
                   '"to":{"role":"mall_cs"},"msg_id":"17896333168%d",'
                   '"content":"消息%d","type":0,"ts":1756000000000}}' % (index + 1, index + 1))
        frames.append({"sid": "60001|1", "dir": "in", "data": payload, "t": 1000 + index})
    assert len({len(f["data"]) for f in frames}) == 1, "测试帧必须等长才复现得出旧 bug"
    for item in frames:
        src._push.queue.put_nowait(item)
    src._pump_push()
    assert len(rows) == 6, [r["msg_id"] for r in rows]
    assert len({r["platform_message_id"] for r in rows}) == 6
    assert not {k: v for k, v in src.drops.items() if v}


def test_byte_identical_frame_twice_is_deduped_downstream():
    """JS 侧不再判重, 真重复必须靠 Python 的 msg_id 去重兑住（不能变成重复上报）。"""
    rows: list = []
    session = make_session(port=60001, cid=1, url="chat")
    session.account = "cs_12345:678"
    src = source_with(session, on_event=rows.append)
    item = {"sid": "60001|1", "dir": "in", "data": PUSH, "t": 1000}
    src._push.queue.put_nowait(dict(item))
    src._push.queue.put_nowait(dict(item))
    src._pump_push()
    assert len(rows) == 1
    assert src.drops["dedup_py"] == 1


def test_frame_error_is_counted_and_does_not_kill_the_pump():
    src = source_with(make_session(frames=["not-a-dict"]))
    assert src._pump_session(src.sessions[0]) == "ok"
    assert src.drops["frame_error"] == 1


def test_missing_buyer_id_is_reported_not_dropped():
    """全部上报: 取不到买家 uid 的帧也报, 带 skipped_reason 由大脑决定。"""
    rows: list = []
    src = source_with(on_event=rows.append)
    msg = src._normalize_message({"msg_id": "m9", "type": 0, "content": "在吗"})
    assert msg is not None and msg["skipped_reason"] == "no_buyer" and msg["is_diagnostic"] is True
    assert src.reported["no_buyer"] == 1
    assert src.drops["dedup_py"] == 0


def test_report_all_off_restores_old_drop():
    src = source_with(cfg={"report_all": False})
    assert src._normalize_message({"msg_id": "m9", "type": 0}) is None
    assert src.report_all is False and src.reported["no_buyer"] == 1


def test_unknown_frame_becomes_diagnostic_event_and_is_archived(tmp_path):
    rows: list = []
    archive = tmp_path / "frames_raw.jsonl"
    src = source_with(on_event=rows.append, cfg={"raw_archive_path": str(archive)})
    src._handle_frame({"dir": "in", "data": "BLOB:4096", "t": 1700})
    assert len(rows) == 1
    assert rows[0]["skipped_reason"] == "unknown_kind"
    assert rows[0]["is_diagnostic"] is True and rows[0]["buyer_id"] == ""
    assert src.reported["unknown_kind"] == 1
    assert src.drops["unparsed_frame"] == 0          # report_all 下不算丢弃
    assert archive.exists() and "BLOB:4096" in archive.read_text(encoding="utf-8")


def test_unknown_frame_with_report_all_off_still_counted_as_drop():
    src = source_with(cfg={"report_all": False})
    src._handle_frame({"dir": "in", "data": "BLOB:4096"})
    assert src.drops["unparsed_frame"] == 1
    assert src.reported["unknown_kind"] == 1


def test_system_frame_is_reported_as_diagnostic():
    rows: list = []
    src = source_with(on_event=rows.append)
    src._handle_frame({"dir": "in", "data": '{"cmd":"mall_system_msg","message":{}}', "t": 5})
    assert len(rows) == 1 and rows[0]["frame_kind"] == "mall_system_msg"
    assert rows[0]["skipped_reason"] == "system_frame"
    assert src.reported["system_frame"] == 1


def test_seat_uid_as_buyer_is_flagged_not_dropped():
    rows: list = []
    session = make_session(port=60001, cid=1, url="chat")
    session.account = "cs_111:999"
    src = source_with(session, on_event=rows.append)
    payload = ('{"response":"push","message":{"from":{"role":"user","uid":"999"},'
               '"to":{"role":"mall_cs","uid":"111"},"msg_id":"m7","content":"x",'
               '"type":0,"ts":1756000000000}}')
    src._handle_frame({"dir": "in", "data": payload, "t": 9}, session)
    assert len(rows) == 1
    assert rows[0]["skipped_reason"] == "seat_uid_as_buyer" and rows[0]["is_diagnostic"] is True
    assert src.reported["seat_uid_as_buyer"] == 1


def test_dedup_mode_off_reports_duplicates():
    session = make_session(port=60001, cid=1, url="chat")
    session.account = "cs_12345:678"
    rows: list = []
    src = source_with(session, on_event=rows.append, cfg={"dedup_mode": "off"})
    item = {"sid": "60001|1", "dir": "in", "data": PUSH, "t": 1000}
    src._push.queue.put_nowait(dict(item))
    src._push.queue.put_nowait(dict(item))
    src._pump_push()
    assert len(rows) == 2 and src.drops["dedup_py"] == 0


def test_dedup_mode_default_still_reports_once():
    session = make_session(port=60001, cid=1, url="chat")
    session.account = "cs_12345:678"
    rows: list = []
    src = source_with(session, on_event=rows.append)
    item = {"sid": "60001|1", "dir": "in", "data": PUSH, "t": 1000}
    src._push.queue.put_nowait(dict(item))
    src._push.queue.put_nowait(dict(item))
    src._pump_push()
    assert len(rows) == 1 and src.drops["dedup_py"] == 1
    assert src.status()["dedup_mode"] == "platform_id"


# ---------------------------------------------------------------- 多账号会话
def fake_discovery(monkeypatch, entries):
    """entries: [(port, cid, url, kwargs_for_FakeCdp)] —— 每次扫描都新建连接（真实行为）。"""
    monkeypatch.setattr(pddbridge_source.pdd_cdp, "is_alive", lambda port: True)
    monkeypatch.setattr(pddbridge_source.pdd_cdp, "discover_ports",
                        lambda only_pdd=False: {port: [] for port, *_ in entries})

    def _find(port):
        return [{"cdp": FakeCdp(**kwargs), "cid": cid, "url": url, "title": "t"}
                for p, cid, url, kwargs in entries if p == port]

    monkeypatch.setattr(pddbridge_source.pdd_cdp, "find_socketutil_sessions", _find)


def test_scan_attaches_every_account_across_ports(monkeypatch):
    fake_discovery(monkeypatch, [
        (57165, 1, "https://mms.pinduoduo.com/workbench/notification?mallcsid=cs_111:1",
         {"info": {"csidGuess": "cs_111:1", "globalMallId": "111"}}),
        (57165, 2, "https://mms.pinduoduo.com/workbench/notification?mallcsid=cs_222:2",
         {"info": {"csidGuess": "cs_222:2", "globalMallId": "222"}}),
        (60001, 1, "https://mms.pinduoduo.com/workbench/notification?mallcsid=cs_333:3",
         {"info": {"csidGuess": "cs_333:3", "globalMallId": "333"}}),
    ])
    src = PddbridgeSource({})
    assert src._scan_and_attach() is True
    assert len(src.sessions) == 3
    assert sorted(s.account for s in src.sessions) == ["cs_111:1", "cs_222:2", "cs_333:3"]
    assert sorted(src.status()["accounts"]) == ["cs_111:1", "cs_222:2", "cs_333:3"]
    # 已挂的不会再挂一次（否则每次重扫都多一条连接）
    assert src._scan_and_attach() is True
    assert len(src.sessions) == 3


def test_unattached_context_is_counted(monkeypatch):
    fake_discovery(monkeypatch, [
        (57165, 1, "https://mms.pinduoduo.com/workbench/notification?a",
         {"info": {"csidGuess": "cs_111:1"}}),
        (57165, 2, "https://mms.pinduoduo.com/workbench/notification?b",
         {"inject_ok": False}),
    ])
    src = PddbridgeSource({})
    assert src._scan_and_attach() is True
    assert len(src.sessions) == 1
    assert src.drops["context_unattached"] == 1


def test_one_account_lost_does_not_kill_the_others():
    ok = make_session(port=57165, cid=1, url="a", frames=[{"dir": "in", "data": PUSH, "t": 1}])
    dead = make_session(port=57165, cid=2, url="b", hooked=False)
    rows = []
    src = source_with(ok, dead, on_event=rows.append)
    for session in list(src.sessions):
        if src._pump_session(session) != "ok":
            src._drop_session(session)
    assert [s.cid for s in src.sessions] == [1]
    assert src.drops["hook_lost"] == 1
    assert len(rows) == 1
    assert src._pump_session(src.sessions[0]) == "ok"


def test_multi_account_message_carries_its_own_shop(monkeypatch):
    """帧里不带 csid 时, 必须用该会话自己的账号, 不能全落到同一个店铺。"""
    session = make_session(port=60001, cid=1, url="b", info={"csidGuess": "cs_222:2"})
    session.account = "cs_222:2"
    rows = []
    src = source_with(session, on_event=rows.append)
    src._handle_frame({"dir": "in", "data": PUSH_NO_CSID, "t": 7}, session)
    assert rows and rows[0]["account"] == "cs_222:2", rows


def test_send_routing_picks_the_right_shop():
    a = make_session(port=57165, cid=1, url="a")
    b = make_session(port=57165, cid=2, url="b")
    a.account, a.mall_id = "cs_111:1", "111"
    b.account, b.mall_id = "cs_222:2", "222"
    src = source_with(a, b)
    assert src._session_for_send("cs_222:2") is b
    assert src._session_for_send("cs_111:1") is a
    # 目标 account 带店铺号、但与在线会话不一致 → 必须拦住（串台保护）
    only = source_with(a)
    assert only._session_for_send("cs_427302374:164945148") is None
    # 目标 account 解析不出店铺号且只有一个会话 → 可以用它
    assert only._session_for_send("unknown") is a
    # 多账号且认不出目标 → 宁可不发
    assert src._session_for_send("who-knows") is None


def test_ambiguous_send_is_blocked_not_guessed():
    a = make_session(port=57165, cid=1, url="a")
    b = make_session(port=57165, cid=2, url="b")
    a.account, a.mall_id = "cs_111:1", "111"
    b.account, b.mall_id = "cs_222:2", "222"
    src = source_with(a, b)
    waiter = pddbridge_source._SendWaiter()
    waiter.deadline = 9e9
    src._do_send_action("4764375385604", "hi", "unknown-account", waiter, 5.0, None)
    assert waiter.result["status"] == "blocked"
    assert waiter.result["real_send"] is False


# ---------------------------------------------------------------- 推送通道
def test_pushed_frame_uses_its_session_account():
    session = make_session(port=60001, cid=1, url="b")
    session.account = "cs_222:2"
    rows = []
    src = source_with(session, on_event=rows.append)
    src._push.queue.put_nowait({"sid": "60001|1", "dir": "in", "data": PUSH_NO_CSID, "t": 9})
    src._pump_push()
    assert rows and rows[0]["account"] == "cs_222:2"


def test_pushed_frame_with_unknown_session_is_counted():
    rows = []
    src = source_with(make_session(), on_event=rows.append)
    src._push.queue.put_nowait({"sid": "999|9", "dir": "in", "data": PUSH_NO_CSID, "t": 9})
    src._pump_push()
    assert src.drops["push_unknown_session"] == 1
    assert len(rows) == 1          # 认不出会话也照常处理, 不丢


def test_push_overload_is_visible():
    src = PddbridgeSource({})
    src._push.dropped = 2
    src._pump_push()
    assert src.drops["push_overload"] == 2


def test_push_server_accepts_post_and_rejects_wrong_token():
    src = PddbridgeSource({})
    port = src._push.start()
    try:
        good = "http://127.0.0.1:%d/push/%s" % (port, src._push.token)
        req = urllib.request.Request(
            good, data=json.dumps({"sid": "1|1", "dir": "in", "data": PUSH}).encode(),
            headers={"content-type": "application/json"},
        )
        assert urllib.request.urlopen(req, timeout=5).status == 200
        assert src._push.received == 1
        assert len(src._push.drain()) == 1
        bad = urllib.request.Request("http://127.0.0.1:%d/push/nope" % port, data=b"{}",
                                     headers={"content-type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(bad, timeout=5)
        assert err.value.code == 404
        assert src._push.rejected == 1
    finally:
        src._push.stop()


def test_push_queue_is_bounded():
    push = pddbridge_source._PushServer(maxsize=1)
    push.queue.put_nowait({"a": 1})
    with pytest.raises(queue.Full):
        push.queue.put_nowait({"a": 2})


def test_inject_payload_carries_push_url_and_session_id():
    src = PddbridgeSource({})
    src._push.port = 12345
    session = make_session(port=57165, cid=3)
    payload = src._inject_payload(session)
    assert "127.0.0.1:12345/push/" in payload
    assert "'57165|3'" in payload
    assert "__pddBridge_hooked = 1" in payload     # 注入脚本本体确实在


# ---------------------------------------------------------------- 状态与配置
def test_status_and_diagnostics_expose_sessions_and_drops():
    src = source_with(make_session())
    src.sessions[0].account = "cs_111:1"
    src.drops["hook_lost"] = 4
    st = src.status()
    assert st["drops"]["hook_lost"] == 4
    assert st["injected"] is True and st["sessions"][0]["account"] == "cs_111:1"
    assert st["accounts"] == ["cs_111:1"]
    assert src.diagnostics()["drops"]["hook_lost"] == 4
    assert src.diagnostics()["sessions"] == 1


def test_close_sessions_clears_state():
    src = source_with(make_session(), make_session(cid=2, url="b"))
    src._close_sessions()
    assert src.sessions == [] and src.status()["injected"] is False


def test_state_after_lost_hook_is_rescanned_by_loop():
    src = source_with(make_session(hooked=False))
    assert src._pump_session(src.sessions[0]) == "lost"
    src._drop_session(src.sessions[0])
    src._set_state(SCANNING)
    assert src.state == SCANNING and src.sessions == []


def test_stale_tanyu_log_dir_falls_back_to_newest_version(tmp_path):
    (tmp_path / "tanyu2.9.1").mkdir()                       # 配置里指向的旧版本已不存在
    logs = tmp_path / "tanyu3.0.2" / "logs"
    logs.mkdir(parents=True)
    stale = str(tmp_path / "tanyu2.9.1" / "logs")
    assert resolve_log_dir(stale) == str(logs)


def test_existing_tanyu_log_dir_is_kept(tmp_path):
    logs = tmp_path / "tanyu3.0.2" / "logs"
    logs.mkdir(parents=True)
    assert resolve_log_dir(str(logs)) == str(logs)
    assert resolve_log_dir("") == ""


def test_version_and_gateway_stay_in_lockstep():
    from bridge import __version__
    from run_frontend_service import LOCAL_GATEWAY_VERSION
    assert __version__ == LOCAL_GATEWAY_VERSION == "0.7.0.2"


# ---------------------------------------------------------------- 注入脚本本体
def test_inject_js_has_dedup_guard_and_no_prefix_dedup():
    js = (ROOT / "bridge" / "pddbridge" / "inject.js").read_text(encoding="utf-8")
    assert "__pddBridge_stats = function" in js
    for key in ("overflow_drop", "dedup_skip", "exact_repeat", "pushed", "push_skip", "push_retry"):
        assert key in js
    assert "installDedupGuard" in js and "__pddBridgeGuard" in js
    assert "slice(0, 120)" not in js      # 前缀截断判重会吃掉连续同长消息


def test_inject_js_push_and_buffer_paths_in_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node 未安装")
    proc = subprocess.run([node, str(ROOT / "tests" / "inject_push_check.js")],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OK" in proc.stdout



def test_fallback_msg_id_is_never_used_for_dedup():
    """没有平台 msg_id 时用 content+time 哈希兜底；同秒同内容会撞哈希，
    拿它去重会误杀真实消息（不丢消息补丁 0.5.19.15）。"""
    from bridge.pddbridge_source import _is_fallback_id

    assert _is_fallback_id("a1b2c3d4e5f60718293a4b5c") is True      # 24 位 hex
    assert _is_fallback_id("1789636966238") is False                # 平台毫秒 id
    assert _is_fallback_id("frame-ad9de76e426a0488378e") is False
    assert _is_fallback_id("") is False

    src = source_with(cfg={})
    fallback = "a1b2c3d4e5f60718293a4b5c"
    assert src._dedup("push", fallback) is False
    assert src._seen_before(fallback) is False
    assert src._dedup("push", fallback) is False, "兜底 id 永远不能判重"
    assert src._seen_before(fallback) is False
    assert src._dedup("push", "1789636966238") is False   # 第一次只登记
    assert src._dedup("push", "1789636966238") is True, "真实平台 id 仍要去重"
    assert src._seen_before("1789636966238") is False
    assert src._seen_before("1789636966238") is True


# ---------------------------------------------------------------- 数据源通道
class _FakeFeed:
    def __init__(self):
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


def _feed_agent(tmp_path, **overrides):
    from bridge.agent import BridgeAgent
    from bridge.config import load_config

    cfg = load_config(tmp_path / "bridge_config.json")
    cfg.update({
        "agent_id": "pdd-test",
        "agent_token": "token",
        "local_queue_path": str(tmp_path / "queue.jsonl"),
        "local_seat_push": "false",
        **overrides,
    })
    agent = BridgeAgent(cfg)
    agent.watcher = _FakeFeed()
    agent.pddbridge_source = _FakeFeed()
    return agent


def test_log_feed_is_not_run_beside_cdp(tmp_path):
    """两条通道同时上报会把同一个买家拆成多张浮窗卡片。

    日志通道里的账号是探域的展示名（主账号 / pdd42730237415），CDP 是 cs_商城:席位，
    中心按 (account, buyer_id) 建会话，于是同一个买家在浮窗上出现两三张卡片。
    """
    agent = _feed_agent(tmp_path, data_source="cdp")
    assert agent._effective_source == "cdp"
    agent._start_feeds()
    assert agent.watcher.started == 0
    assert agent.pddbridge_source.started == 1


def test_log_feed_runs_when_selected_or_after_degrade(tmp_path):
    selected = _feed_agent(tmp_path, data_source="tanyu_logs")
    selected._start_feeds()
    assert selected.watcher.started == 1
    assert selected.pddbridge_source.started == 0

    degraded = _feed_agent(tmp_path, data_source="cdp")
    degraded._start_feeds()
    assert degraded.watcher.started == 0
    degraded._fallback_to_tanyu_logs()          # CDP 15 秒没挂上时走这里
    assert degraded._effective_source == "tanyu_logs"
    assert degraded.watcher.started == 1


def test_degraded_cdp_recovers_without_restart(monkeypatch):
    """降级不能是单行道。

    现场事故：客户机 PDD 工作台版本装错 → 桥接 15 秒后永久降级到日志通道；
    装回正确版本后桥接仍收不到消息，必须重装桥接才能恢复。
    修复：降级后每 30s 重试 CDP，挂上自动切回并停掉日志通道。
    """
    import bridge.pddbridge_source as pbs

    src = PddbridgeSource({})
    recovered = []
    src.on_recover = lambda: recovered.append(1)
    src._degraded = True
    src._next_retry = 0.0

    calls = {"n": 0}
    def _scan():
        calls["n"] += 1
        return calls["n"] >= 2          # 第一次重试失败, 第二次成功
    monkeypatch.setattr(src, "_scan_and_attach", _scan)

    assert src._try_recover() is False   # 未就绪: 保持降级, 不误切
    assert src._degraded is True and not recovered
    src._next_retry = 0.0                # 模拟 30 秒后下一次重试
    assert src._try_recover() is True    # 工作台修好后: 自动切回
    assert src._degraded is False
    assert src.state == pbs.LISTENING
    assert recovered == [1]
    # 已恢复后不再继续重试
    assert src._try_recover() is False
    assert calls["n"] == 2


def test_agent_recovers_to_cdp_and_stops_log_feed(tmp_path):
    agent = _feed_agent(tmp_path, data_source="cdp")
    agent._fallback_to_tanyu_logs()
    assert agent._effective_source == "tanyu_logs"
    agent._recover_to_cdp()
    assert agent._effective_source == "cdp"
    assert agent.watcher.stopped == 1    # 日志通道必须停, 否则回到双通道拆卡片的旧 bug
    # 幂等：已恢复后再调不重复停
    agent._recover_to_cdp()
    assert agent.watcher.stopped == 1
