"""Isolated file/queue/command recovery tests; sends are always mocked."""
import json
import os
import threading
import time
from types import SimpleNamespace

from bridge.agent import BridgeAgent
from bridge.command_journal import CommandJournal
from bridge.message_timing import (command_expired, history_ready, stamp_message,
                                   TAKEOVER_PARENT_MAX_AGE_SECONDS)
from bridge.pddbridge_source import PddbridgeSource, _SendWaiter, _Session, LISTENING
from bridge.watcher import LogWatcher


def raw(mid, timestamp):
    return 'buyer_msg=' + json.dumps({"msg_id": mid, "role": "user", "buyer_id": "test-buyer",
        "account": "cs_123:456", "content": "offline", "ts": timestamp}) + '\n'


def drain(watcher):
    for name in list(watcher._handles):
        for line in watcher._read_new(name):
            watcher._emit_line(line, name.lower())
    watcher._flush_pending(force=True)
    watcher._save_checkpoints()


def test_resume_cursor_backfills_without_replaying_committed_record(tmp_path):
    logfile = tmp_path / 'inside.log'
    cursor = tmp_path / 'cursor.json'
    logfile.write_text(raw('pre-start', time.time()), encoding='utf-8')
    rows = []
    w = LogWatcher(str(tmp_path), rows.append, checkpoint_path=cursor)
    w._attach('INSIDE', 'inside.log')
    drain(w)
    assert rows == []
    with logfile.open('a', encoding='utf-8') as handle:
        handle.write(raw('live', time.time()))
    drain(w)
    assert rows[0]['msg_id'] == 'live'
    assert not rows[0].get('is_history')
    w.stop()
    with logfile.open('a', encoding='utf-8') as handle:
        handle.write(raw('during-outage', time.time()))
    restarted = LogWatcher(str(tmp_path), rows.append, checkpoint_path=cursor)
    try:
        restarted._attach('INSIDE', 'inside.log')
        drain(restarted)
        assert [row['msg_id'] for row in rows] == ['live', 'during-outage']
        assert rows[-1]['is_history']
        assert not history_ready(rows[-1], time.time())
        assert history_ready(rows[-1], rows[-1]['ts'] + 91)
    finally:
        restarted.stop()


def test_rotation_drains_old_file_and_marks_new_backfill(tmp_path):
    old = tmp_path / 'inside-old.log'
    old.write_text('', encoding='utf-8')
    rows = []
    w = LogWatcher(str(tmp_path), rows.append)
    w._attach('INSIDE', old.name)
    old.write_text(raw('unread-old', time.time()), encoding='utf-8')
    new = tmp_path / 'inside-new.log'
    new.write_text(raw('rotated-history', time.time() - 500), encoding='utf-8')
    try:
        w._attach('INSIDE', new.name)
        assert w.attached['INSIDE'] == old.name
        drain(w)
        w._attach('INSIDE', new.name)
        drain(w)
        assert [row['msg_id'] for row in rows] == ['unread-old', 'rotated-history']
        assert rows[-1]['is_history']
    finally:
        w.stop()


def test_truncate_reopens_from_start(tmp_path):
    path = tmp_path / 'inside.log'
    path.write_text('x' * 2000 + '\n', encoding='utf-8')
    rows = []
    watcher = LogWatcher(str(tmp_path), rows.append)
    try:
        watcher._attach('INSIDE', path.name)
        path.write_text(raw('truncated-history', time.time() - 500), encoding='utf-8')
        watcher._attach('INSIDE', path.name)
        drain(watcher)
        assert rows[0]['msg_id'] == 'truncated-history'
        assert rows[0]['is_history']
    finally:
        watcher.stop()


def test_auto_command_expires_while_previous_command_is_executing(tmp_path):
    now = [1000.0]
    sent, reported = [], []
    commands = [dict(id='first', type='send_text', created_at=999, buyer_id='test',
                     account='cs_123:456', content='mock', meta={}),
                dict(id='second', type='send_text', created_at=999, buyer_id='test',
                     account='cs_123:456', content='mock', meta={})]
    def send(*args, **kwargs):
        sent.append(args)
        now[0] += 91
        return {'ok': True, 'status': 'confirmed', 'real_send': True}
    agent = object.__new__(BridgeAgent)
    agent.cfg = {'dry_run': False}
    agent.client = SimpleNamespace(pull_commands=lambda **kwargs: commands, server_now=lambda: now[0])
    agent.command_journal = CommandJournal(tmp_path / 'commands.json')
    agent._retry_command_results = lambda: None
    agent._report_command_result = lambda cid, result: reported.append((cid, result))
    agent._commands_done = set()
    agent._effective_source = 'tanyu_logs'
    agent.pddbridge_source = None
    agent.platform = SimpleNamespace(send_text=send)
    agent._handle_commands()
    assert len(sent) == 1
    assert reported[-1][1]['status'] == 'expired'
    assert reported[-1][1]['real_send'] is False
    assert command_expired({'type': 'send_text', 'created_at': 0}, 1000)
    assert not command_expired({'type': 'send_text', 'created_at': 0,
                                'meta': {'manual_direct': True}}, 1000)
    assert command_expired({'type': 'send_text', 'created_at': 0,
                            'meta': {'manual_direct': 'true'}}, 1000)


def test_cdp_timeout_cancels_unexecuted_action():
    source = PddbridgeSource({})
    source._thread = SimpleNamespace(is_alive=lambda: True)
    result = source.send_and_wait('test', 'mock', 'cs_123:456', timeout=0.001)
    assert result['real_send'] is False
    assert source._act == []


def test_cdp_checks_expiry_again_after_account_lookup():
    source = PddbridgeSource({})
    source.state = LISTENING
    calls = []
    waiter = _SendWaiter()
    waiter.deadline = time.monotonic() + 5
    def evaluate(expression, cid, **_kwargs):   # 真实签名带 timeout 预算
        calls.append(expression)
        waiter.cancelled = True
        return {'globalMallId': '123'}
    session = _Session(57165, 'u', SimpleNamespace(eval=evaluate), 1)
    source.sessions = [session]
    source._do_send(session, 'test', 'mock', 'cs_123:456', waiter, 5, None)
    assert len(calls) == 1
    assert waiter.result['status'] == 'expired'


def test_invalid_platform_time_stays_local_and_capture_time_is_immutable():
    missing = stamp_message({'ts': 0, 'captured_at': 1000}, now=2000)
    assert missing['captured_at'] == 1000
    assert missing['is_history']
    assert not history_ready(missing, 2000)
    future = stamp_message({'ts': 3000, 'captured_at': 1000}, now=1000)
    assert not history_ready(future, 4000)


def test_small_positive_clock_skew_stays_live_and_old_future_rows_recover():
    # 平台时间戳是秒级的，平台时钟常比本机快几百毫秒；容差内必须仍按实时消息处理。
    skewed = stamp_message({'ts': 1000.4, 'captured_at': 1000.0}, now=1000.0)
    assert not skewed.get('is_history')
    assert history_ready(skewed, 1000.0)

    # 旧队列里已写入的 future_platform_time（平台时间追上后）必须能恢复为可上传的历史帧。
    stale_future = {'ts': 1000, 'captured_at': 1000, 'is_history': True,
                    'history_reason': 'future_platform_time', 'source': 'history'}
    recovered = stamp_message(dict(stale_future), historical=True, now=1000 + 91)
    assert recovered['is_history']
    assert recovered['history_reason'] != 'future_platform_time'
    assert history_ready(recovered, 1000 + 91)

    # 真正超前的帧（远超容差）仍然不允许上传。
    far_future = stamp_message({'ts': 3000, 'captured_at': 1000}, now=1000)
    assert not history_ready(far_future, 4000)


def test_restart_after_rotation_reads_old_remainder_then_new_file(tmp_path):
    old = tmp_path / 'inside-old.log'
    old.write_text('', encoding='utf-8')
    cursor = tmp_path / 'cursor.json'
    w = LogWatcher(str(tmp_path), lambda _: None, checkpoint_path=cursor)
    w._attach('INSIDE', 'inside-*.log')
    drain(w)
    w.stop()
    old.write_text(raw('old-remainder', time.time() - 100), encoding='utf-8')
    middle = tmp_path / 'inside-middle.log'
    middle.write_text(raw('middle-backfill', time.time() - 100), encoding='utf-8')
    os.utime(middle, (1, 1))  # An archived file can keep its original modification time.
    new = tmp_path / 'inside-new.log'
    new.write_text(raw('new-backfill', time.time() - 100), encoding='utf-8')
    rows = []
    restarted = LogWatcher(str(tmp_path), rows.append, checkpoint_path=cursor)
    try:
        restarted._pick = lambda *args, **kwargs: new
        restarted._attach('INSIDE', 'inside-*.log')
        assert restarted.attached['INSIDE'] == old.name
        drain(restarted)
        restarted._attach('INSIDE', 'inside-*.log')
        drain(restarted)
        restarted._attach('INSIDE', 'inside-*.log')
        drain(restarted)
        assert [e['msg_id'] for e in rows] == ['old-remainder', 'middle-backfill', 'new-backfill']
        assert all(e['is_history'] for e in rows)
    finally:
        restarted.stop()


def test_recovery_batch_does_not_classify_new_append_as_history(tmp_path):
    path = tmp_path / 'inside.log'
    cursor = tmp_path / 'cursor.json'
    path.write_text('', encoding='utf-8')
    old = LogWatcher(str(tmp_path), lambda _: None, checkpoint_path=cursor)
    old._attach('INSIDE', path.name)
    drain(old)
    old.stop()
    path.write_text(raw('backfill', time.time()), encoding='utf-8')
    rows = []
    w = LogWatcher(str(tmp_path), rows.append, checkpoint_path=cursor)
    try:
        w._attach('INSIDE', path.name)
        with path.open('a', encoding='utf-8') as handle:
            handle.write(raw('new-live', time.time()))
        drain(w)
        drain(w)
        assert rows[0]['is_history'] is True
        assert not rows[1].get('is_history')
    finally:
        w.stop()


def test_empty_and_whitespace_command_type_cannot_bypass_expiry():
    for kind in (None, '', ' ', ' send_text ', 'send_text'):
        assert command_expired({'type': kind, 'created_at': 1}, 1000)


def test_takeover_parent_must_still_be_fresh_even_if_command_is_new():
    agent = object.__new__(BridgeAgent)
    agent.client = SimpleNamespace(server_now=lambda: 1000)
    command = {'type': 'send_text', 'created_at': 999, 'account': 'cs_123:456',
               'buyer_id': 'test-buyer', 'meta': {'takeover_parent_msg_id': 'parent'}}
    # 未知父消息不再直接丢：桥接重启后本地不可能有全部来源时间，改为信任命令 created_at。
    assert not agent._command_expired(command)
    # 明确超出父消息窗口的仍然拦截。
    stale = 1000 - int(TAKEOVER_PARENT_MAX_AGE_SECONDS) - 10
    agent._remember_source_time({'account': 'cs_123:456', 'buyer_id': 'test-buyer',
                                 'msg_id': 'parent', 'ts': stale})
    assert agent._command_expired(command)
    agent._remember_source_time({'account': 'cs_123:456', 'buyer_id': 'test-buyer',
                                 'msg_id': 'parent', 'ts': 999})
    assert agent._command_expired(command)  # A duplicate cannot refresh platform time.
    command['meta']['takeover_parent_msg_id'] = 'fresh-parent'
    agent._remember_source_time({'account': 'cs_123:456', 'buyer_id': 'test-buyer',
                                 'msg_id': 'fresh-parent', 'ts': 999})
    assert not agent._command_expired(command)


def test_slow_center_takeover_reply_is_not_dropped_as_expired():
    """实测回归：中心 45s 后才下发、父消息已 105s，旧逻辑会误判过期丢回复。"""
    agent = object.__new__(BridgeAgent)
    agent.client = SimpleNamespace(server_now=lambda: 1000)
    command = {'type': 'send_text', 'created_at': 955, 'account': 'cs_100000001:200000002',
               'buyer_id': '2308961723581', 'meta': {'takeover_parent_msg_id': '1789729657649'}}
    agent._remember_source_time({'account': 'cs_100000001:200000002', 'buyer_id': '2308961723581',
                                 'msg_id': '1789729657649', 'ts': 895})  # 105s before now
    assert not agent._command_expired(command)


def test_command_loop_uses_configured_poll_interval_and_caps_it():
    def waits_for(poll_config):
        agent = object.__new__(BridgeAgent)
        agent.cfg = {'command_poll_seconds': poll_config}
        calls = []

        class Stop:
            def is_set(self):
                return len(calls) >= 2
            def wait(self, _seconds):
                return None

        agent._stop = Stop()
        agent._handle_commands = lambda **kw: calls.append(kw.get('wait_seconds'))
        agent._command_loop()
        return calls

    assert waits_for(1.5) == [1.5, 1.5]      # 用配置值，而不是硬编码 20s
    assert waits_for(20) == [5.0, 5.0]       # 上限 5s
    assert waits_for(0.01) == [0.5, 0.5]     # 下限 0.5s
    assert waits_for(None) == [1.5, 1.5]     # 默认 1.5s


def test_command_result_reporter_is_async():
    """① 命令结果回报不能再阻塞命令线程（中心一次 ACK ~9s）。"""
    agent = object.__new__(BridgeAgent)
    agent._stop = threading.Event()
    calls = []

    def slow(command_id, result):
        time.sleep(0.4)
        calls.append(command_id)
        return True

    agent._send_command_result = slow
    t0 = time.perf_counter()
    assert agent._report_command_result("cmd-async-1", {"ok": True})
    assert time.perf_counter() - t0 < 0.2          # 立即返回，不等后台
    deadline = time.time() + 3
    while not calls and time.time() < deadline:
        time.sleep(0.05)
    assert calls == ["cmd-async-1"]
    agent._stop.set()


def test_session_state_is_applied_to_local_workbench(monkeypatch):
    """中心下发的 session_state（转人工）必须落到本机工作台，不能再当“不支持类型”丢掉。"""
    import bridge.agent as agent_mod

    captured = {}

    class FakeResp:
        def read(self):
            return b"{}"
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = req.data.decode("utf-8")
        return FakeResp()

    monkeypatch.setattr(agent_mod.urllib.request, "urlopen", fake_urlopen)
    agent = object.__new__(BridgeAgent)
    agent.cfg = {"agent_id": "a", "agent_token": "t",
                 "local_workbench_url": "http://127.0.0.1:18767"}
    result = agent._apply_session_state({"id": "cmd-state-1", "type": "session_state",
                                         "account": "cs_1:2", "buyer_id": "b1",
                                         "handoff": True})
    assert result["ok"]
    assert captured["url"].endswith("/api/local-seat/v1/session-state")
    assert '"handoff": true' in captured["body"]


def test_cdp_session_key_includes_target_id():
    """多个 middle_panel 目标可能共用 executionContextId 和 URL，key 必须靠 target id 区分。"""
    from bridge.pddbridge_source import _Session

    class DummyCdp:
        def close(self):
            return None

    a = _Session(57166, "https://x/middle_panel", DummyCdp(), 11, "TARGET_A")
    b = _Session(57166, "https://x/middle_panel", DummyCdp(), 11, "TARGET_B")
    assert a.key() != b.key()
    c = _Session(57166, "https://x/middle_panel", DummyCdp(), 11, "TARGET_A")
    assert a.key() == c.key()


def test_history_pull_loop_pulls_recent_buyers():
    """tanyu 为主时，CDP 周期补拉只针对“最近出现过的会话”，并受 max_buyers 限制。"""
    agent = object.__new__(BridgeAgent)
    agent._stop = threading.Event()
    agent.cfg = {"history_pull_seconds": 0.01, "history_pull_size": 5,
                 "history_pull_max_buyers": 2, "history_pull_gap_seconds": 0}
    calls = []

    class Src:
        def pull_history(self, uid, account="", **kwargs):
            calls.append((account, uid))
            return {"ok": True}

    agent.pddbridge_source = Src()
    agent._recent_buyers = {("cs_1:2", "b1"): 3.0, ("cs_1:2", "b2"): 2.0, ("cs_1:2", "b3"): 1.0}
    thread = threading.Thread(target=agent._history_pull_loop, daemon=True)
    thread.start()
    deadline = time.time() + 3
    while len(calls) < 2 and time.time() < deadline:
        time.sleep(0.02)
    agent._stop.set()
    thread.join(timeout=2)
    assert ("cs_1:2", "b1") in calls
    assert ("cs_1:2", "b2") in calls
    assert ("cs_1:2", "b3") not in calls          # 受 history_pull_max_buyers=2 限制


def test_order_lookup_uses_one_total_deadline_and_reuses_target(monkeypatch):
    from bridge.pdd_context import PddOrderContextLookup
    lookup = PddOrderContextLookup(timeout=1.8)
    lookup._target_cache['123'] = (time.monotonic() + 10, 'mock-target')
    monkeypatch.setattr(lookup, '_right_panel_targets', lambda deadline: (_ for _ in ()).throw(AssertionError('unneeded discovery')))
    deadlines = []
    def evaluate(target, expression, deadline):
        deadlines.append(deadline)
        return {'ok': True, 'total': 0, 'orders': []}
    monkeypatch.setattr(lookup, '_evaluate', evaluate)
    start = time.monotonic()
    result = lookup.lookup({'account': 'cs_123:456', 'buyer_id': 'test-buyer'})
    assert result['local_context_lookup']['ok']
    assert 0 < deadlines[0] - start <= 1.81


def test_server_clock_uses_http_date_and_monotonic_time(monkeypatch):
    import bridge.client as module
    client = module.BridgeClient('http://unused', 'not-used', 'offline')
    client._server_clock = (1000, 10)
    monkeypatch.setattr(module.time, 'monotonic', lambda: 20)
    monkeypatch.setattr(module.time, 'time', lambda: 9999999)
    assert client.server_now() == 1011
