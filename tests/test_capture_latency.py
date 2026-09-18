"""Offline regression checks: no platform send or center requests."""
import io
import json
import threading
import time
from collections import deque
from types import SimpleNamespace

import pytest

from bridge.agent import BridgeAgent
from bridge.client import BridgeClientError
from bridge.parser import parse_line
from bridge.watcher import LogWatcher
from run_pdd_client import _event_from_message

# Capture transport methods before other tests install the entry-point wrappers.
BASE_INGEST = BridgeAgent._on_local_event
BASE_UPLOAD = BridgeAgent._flush_events


def buyer(mid="latency-test", ts=None):
    return {"platform": "pdd", "msg_id": mid, "platform_message_id": mid,
            "account": "cs_123:456", "buyer_id": "test-buyer", "role": "user",
            "content": "offline test", "ts": time.time() if ts is None else ts}


def line(message):
    return "buyer_msg=" + json.dumps(message)


def bare_agent(tmp_path):
    agent = object.__new__(BridgeAgent)
    agent.cfg = {"agent_id": "offline-test", "dry_run": False}
    agent.platform = SimpleNamespace(name="pdd")
    agent._pending = deque()
    agent._pending_lock = threading.Lock()
    agent._local_ingest_queue = None
    agent._on_local_event = BASE_INGEST.__get__(agent, BridgeAgent)
    agent._flush_events = BASE_UPLOAD.__get__(agent, BridgeAgent)
    agent._accounts_seen = {}
    agent._last_error = ""
    agent.queue_path = tmp_path / "events.jsonl"
    return agent


def test_context_rebuild_preserves_capture_time(tmp_path):
    agent = bare_agent(tmp_path)
    message = {**buyer(), "captured_at": 1788674480.509}
    first = _event_from_message(agent, message)
    rebuilt = _event_from_message(agent, {**first, "order_id": "test-order"})
    assert first["captured_at"] == rebuilt["captured_at"] == 1788674480.509


def test_old_log_record_is_history_without_changing_platform_time():
    rows = []
    watcher = LogWatcher(".", rows.append)
    watcher._emit_line(line(buyer(ts=1788674480)), "inside")
    watcher._flush_pending(force=True)
    assert rows[0]["ts"] == 1788674480
    assert rows[0]["source"] == "history"


def test_realtime_dual_log_record_emits_once():
    rows = []
    watcher = LogWatcher(".", rows.append)
    raw = line(buyer())
    watcher._emit_line(raw, "inside")
    watcher._emit_line(raw, "logrus")
    watcher._flush_pending(force=True)
    assert len(rows) == 1
    assert rows[0]["source"] == "inside"
    assert rows[0]["captured_at"] > 0


def test_missing_timestamp_does_not_become_now():
    message = buyer()
    message.pop("ts")
    rows = parse_line(line(message), "inside")
    assert rows[0]["ts"] == 0


def test_bounded_read_catches_up_within_one_round():
    """单轮读取有上限但会循环追赶（0.5.19.15 补丁）:
    老行为一轮只读 256KiB, 日志一忙就落后几分钟; 现在一轮最多追赶 8MiB/0.5s。"""
    watcher = LogWatcher(".", lambda _: None)
    handle = io.BytesIO(b"unmatched\n" * 150000)      # 1.35MB, 比旧的一轮上限大得多
    watcher._handles["INSIDE"] = handle
    lines = watcher._read_new("INSIDE")
    assert lines, "一轮就该有产出"
    # 追赶量明显高于旧的 256KiB 单步, 但仍受预算限制
    assert handle.tell() > 256 * 1024, "一轮应该追赶多步, 而不是只读一个 256KiB"

    # 预算耗尽时下一轮继续读, 不会丢
    watcher._handles["INSIDE"] = io.BytesIO(b"unmatched\n" * 150000)
    watcher._buffers.pop("INSIDE", None)
    handle = watcher._handles["INSIDE"]
    read = 0
    for _ in range(20):
        before = handle.tell()
        watcher._read_new("INSIDE")
        read += handle.tell() - before
        if handle.tell() >= len(handle.getvalue()):
            break
    assert read == len(handle.getvalue()), "循环读取必须把日志读完"


def test_callback_failure_is_retried_not_remembered():
    rows = []
    def callback(message):
        if not rows:
            rows.append("failed")
            raise OSError("simulated disk failure")
        rows.append(message)
    watcher = LogWatcher(".", callback)
    watcher._emit_line(line(buyer()), "inside")
    with pytest.raises(OSError):
        watcher._flush_pending(force=True)
    watcher._flush_pending(force=True)
    assert len(rows) == 2


def test_retry_keeps_identity_and_partial_ack_only_removes_confirmed(tmp_path):
    agent = bare_agent(tmp_path)
    agent._on_local_event(buyer("first"))
    agent._on_local_event(buyer("second"))
    batches = []
    def upload(events):
        batches.append([dict(e) for e in events])
        if len(batches) == 1:
            raise BridgeClientError("offline")
        return {"ack_version": 1, "event_acks": [
            {"event_id": batches[0][0]["event_id"], "committed": True}]}
    agent.client = SimpleNamespace(upload_events=upload)
    agent._flush_events()
    assert len(agent._pending) == 2
    agent._upload_retry_at = 0
    agent._flush_events()
    assert batches[0] == batches[1]
    assert [e["msg_id"] for e in agent._pending] == ["second"]
    recovered = bare_agent(tmp_path)
    recovered._load_local_queue()
    assert recovered._pending[0]["msg_id"] == agent._pending[0]["msg_id"]
    assert recovered._pending[0]["event_id"] == agent._pending[0]["event_id"]
    assert recovered._pending[0]["captured_at"] == agent._pending[0]["captured_at"]
    assert recovered._pending[0]["is_history"] is True


def test_unacknowledged_success_response_keeps_queue(tmp_path):
    agent = bare_agent(tmp_path)
    agent._on_local_event(buyer())
    agent.client = SimpleNamespace(upload_events=lambda _: {"ok": False})
    agent._flush_events()
    assert len(agent._pending) == 1


def test_queue_rewrite_retries_transient_windows_lock(tmp_path, monkeypatch) -> None:
    """高并发下 os.replace 会偶发 WinError 5；不能让这类瞬时占用污染 last_error。"""
    import pathlib

    agent = bare_agent(tmp_path)
    agent._on_local_event(buyer("rewrite-retry"))
    real_replace = pathlib.Path.replace
    calls = {"n": 0}

    def flaky_replace(self, target):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(5, "Access is denied (simulated WinError 5)")
        return real_replace(self, target)

    monkeypatch.setattr(pathlib.Path, "replace", flaky_replace)
    agent._rewrite_local_queue()

    assert calls["n"] == 3
    assert agent._last_error == ""
    assert "rewrite-retry" in agent.queue_path.read_text(encoding="utf-8")


def test_refused_event_is_dead_lettered_instead_of_silently_dropped(tmp_path):
    """中心明确拒收（unsupported_role 之类）的事件过去被静默剔出队列，
    而本地工作台已经显示过 —— 也就是"本地有、大脑没有"。"""
    agent = bare_agent(tmp_path)
    agent._on_local_event(buyer("refused"))
    event_id = agent._pending[0]["event_id"]

    def upload(_events):
        return {"ack_version": 1, "event_acks": [
            {"event_id": event_id, "status": "ignored", "committed": True,
             "retryable": False, "reason": "unsupported_role"}]}

    agent.client = SimpleNamespace(upload_events=upload)
    agent._flush_events()

    assert len(agent._pending) == 0  # 仍按中心说的终结，不无限重试
    assert "unsupported_role" in agent._last_error  # 但不再无声消失
    dead = tmp_path / "events_refused.jsonl"
    rows = [json.loads(line) for line in dead.read_text(encoding="utf-8").splitlines()]
    assert [row["event_id"] for row in rows] == [event_id]
    assert rows[0]["refused_reason"] == "unsupported_role"


def test_center_content_filter_refusal_is_recorded_but_not_a_client_error(tmp_path):
    """中心按内容规则过滤（invalid_message，例如一长串数字）不能显示成客户端故障，
    但必须进死信文件留痕。"""
    agent = bare_agent(tmp_path)
    agent._on_local_event(buyer("digits"))
    event_id = agent._pending[0]["event_id"]
    agent.client = SimpleNamespace(upload_events=lambda _: {"ack_version": 1, "event_acks": [
        {"event_id": event_id, "status": "rejected", "committed": False,
         "retryable": False, "error": {"code": "invalid_message"}}]})
    agent._flush_events()

    assert agent._last_error == ""
    rows = [json.loads(line)
            for line in (tmp_path / "events_refused.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["refused_reason"] for row in rows] == ["invalid_message"]


def test_benign_plugin_send_echo_is_not_reported_as_loss(tmp_path):
    agent = bare_agent(tmp_path)
    agent._on_local_event(buyer("echo"))
    event_id = agent._pending[0]["event_id"]
    agent.client = SimpleNamespace(upload_events=lambda _: {"ack_version": 1, "event_acks": [
        {"event_id": event_id, "status": "ignored", "committed": True,
         "retryable": False, "reason": "plugin_send_echo"}]})
    agent._flush_events()
    assert agent._last_error == ""
    assert not (tmp_path / "events_refused.jsonl").exists()


def test_unrelated_event_bypasses_blocked_context_and_batches_are_bounded(tmp_path):
    agent = bare_agent(tmp_path)
    for i in range(205):
        agent._on_local_event(buyer(str(i)))
    blocked = {agent._pending[0]["event_id"]}
    batches = []
    def upload(events):
        batches.append(events)
        return {"ack_version": 1, "event_acks": [
            {"event_id": e["event_id"], "committed": True} for e in events]}
    agent.client = SimpleNamespace(upload_events=upload)
    agent._flush_events(blocked_ids=blocked)
    assert 0 < len(batches[0]) <= 100
    assert all(e["event_id"] not in blocked for e in batches[0])
    assert agent._pending[0]["event_id"] in blocked


def test_partial_retry_ack_does_not_starve_tail(tmp_path):
    agent = bare_agent(tmp_path)
    for i in range(205):
        agent._on_local_event(buyer(str(i)))
    batches = []
    def upload(events):
        batches.append([e['msg_id'] for e in events])
        return {'ack_version': 1, 'event_acks': [
            {'event_id': e['event_id'], 'committed': False, 'retryable': True} for e in events]}
    agent.client = SimpleNamespace(upload_events=upload)
    agent._flush_events()
    agent._flush_events()
    assert set(batches[0]).isdisjoint(batches[1])
    assert len(agent._pending) == 205


def test_partial_queue_write_rolls_back_before_retry():
    from bridge.event_queue import append_event
    class ShortWrite(io.BytesIO):
        def write(self, data):
            return super().write(data[:3])
        def __exit__(self, *args):
            return False
    handle = ShortWrite(b'{"old":true}\n')
    handle.seek(0, 2)
    path = SimpleNamespace(parent=SimpleNamespace(mkdir=lambda **kwargs: None),
                           open=lambda *args, **kwargs: handle)
    with pytest.raises(OSError):
        append_event(path, {"event_id": "failed"})
    assert handle.getvalue() == b'{"old":true}\n'


def test_center_ignored_event_is_not_a_client_error_but_is_recorded(tmp_path):
    """大脑主动 ignored（例如历史消息）属于中心决定，不该弹成客户端故障，但要留痕。"""
    agent = bare_agent(tmp_path)
    agent._on_local_event(buyer("ignored-one"))
    event_id = agent._pending[0]["event_id"]
    agent.client = SimpleNamespace(upload_events=lambda _: {"ack_version": 1, "event_acks": [
        {"event_id": event_id, "status": "rejected", "committed": False,
         "retryable": False, "error": {"code": "ignored"}}]})
    agent._flush_events()

    assert agent._last_error == ""
    rows = [json.loads(line)
            for line in (tmp_path / "events_refused.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["refused_reason"] for row in rows] == ["ignored"]


def test_diagnostic_frame_never_enters_center_upload_queue(tmp_path):
    """诊断帧（系统/未知帧）不进中心上传队列: 中心契约只收聊天消息，上送必被拒成死信噪音。
    普通消息不受影响照常进队列。"""
    agent = bare_agent(tmp_path)
    agent._on_local_event({**buyer("diag-one"), "buyer_id": "", "role": "platform",
                           "is_diagnostic": True, "skipped_reason": "system_frame"})
    assert len(agent._pending) == 0, "诊断帧不该进中心上传队列"
    queued = agent.queue_path.read_text(encoding="utf-8").strip() if agent.queue_path.exists() else ""
    assert not queued, "诊断帧不该写入中心队列文件"
    agent._on_local_event(buyer("real-one"))
    assert len(agent._pending) == 1
    assert agent._pending[0]["is_diagnostic"] is False
