"""Isolated file/queue/command recovery tests; sends are always mocked."""
import json
import os
import threading
import time
from types import SimpleNamespace

from bridge.agent import BridgeAgent
from bridge.command_journal import CommandJournal
from bridge.message_timing import command_expired, history_ready, stamp_message
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
    def evaluate(expression, cid):
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
    assert agent._command_expired(command)  # Cannot verify an unknown parent.
    agent._remember_source_time({'account': 'cs_123:456', 'buyer_id': 'test-buyer',
                                 'msg_id': 'parent', 'ts': 800})
    assert agent._command_expired(command)
    agent._remember_source_time({'account': 'cs_123:456', 'buyer_id': 'test-buyer',
                                 'msg_id': 'parent', 'ts': 999})
    assert agent._command_expired(command)  # A duplicate cannot refresh platform time.
    command['meta']['takeover_parent_msg_id'] = 'fresh-parent'
    agent._remember_source_time({'account': 'cs_123:456', 'buyer_id': 'test-buyer',
                                 'msg_id': 'fresh-parent', 'ts': 999})
    assert not agent._command_expired(command)


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
