# -*- coding: utf-8 -*-
"""1.0.6：日志目录自动跟随 / 本地会话不被清空 / 历史时效对所有来源生效。"""
import json
import os
import time
import types

from bridge import log_dir_resolver as r
from bridge.watcher import LogWatcher


def _touch(path, age=0.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x\n", encoding="utf-8")
    t = time.time() - age
    os.utime(path, (t, t))


def test_ini_logdir_wins(tmp_path):
    exe_dir = tmp_path / "3.5.0.40_x64"
    new_logs = tmp_path / "tanyu3.2.2" / "logs"
    _touch(new_logs / "inside_2026-11-27_1.log", age=5)   # 1.0.10：目录里得真有插件日志
    exe_dir.mkdir()
    (exe_dir / "PDDData.ini").write_text(
        "[common]\nPddMsgPlugin=D:/x.dll\nLogDir=%s\n" % str(new_logs).replace("\\", "/"),
        encoding="utf-8")
    dirs = r.ini_log_dirs([str(exe_dir)])
    assert dirs and r.same_dir(dirs[0], str(new_logs))
    resolved, source = r.resolve_active_log_dir(str(tmp_path / "old"), ini_dirs=dirs, candidates=[])
    assert r.same_dir(resolved, str(new_logs)) and source == "pddini"


def test_idle_config_dir_switches_to_recently_written_candidate(tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    _touch(old / "inside_2026-11-26_1.log", age=3600)
    _touch(new / "inside_2026-11-27_2.log", age=5)
    resolved, source = r.resolve_active_log_dir(str(old), ini_dirs=[], candidates=[str(new)])
    assert resolved == str(new) and source == "recent_write"


def test_active_config_dir_is_kept(tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    _touch(old / "logrus.log", age=10)
    _touch(new / "logrus.log", age=1)
    resolved, source = r.resolve_active_log_dir(str(old), ini_dirs=[], candidates=[str(new)])
    assert resolved == str(old) and source == "config"


def test_watcher_switch_starts_at_end_of_new_file(tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (new / "inside_2026-11-27_1.log").write_text("old line\n" * 50, encoding="utf-8")
    seen = []
    w = LogWatcher(str(old), seen.append, checkpoint_path=tmp_path / "c.json")
    w._checkpoints = {"INSIDE": {"path": str(old / "inside_x.log"), "identity": [1, 2], "position": 9}}
    w.request_log_dir(str(new))       # 线程未启动：直接切
    assert w.log_dir == new
    assert "INSIDE" not in w._checkpoints
    w._attach("INSIDE", "inside_*.log")
    assert w._handles["INSIDE"].tell() == os.path.getsize(new / "inside_2026-11-27_1.log")
    for h in w._handles.values():
        h.close()


def _seat_state_cls():
    import run_frontend_service as rfs
    return rfs.LocalSeatState


def test_seat_state_reads_bom_and_keeps_snapshot(tmp_path):
    path = tmp_path / "seat_local_state.json"
    payload = {"version": 1, "sessions": {"a|b": {"account": "a", "buyer_id": "b", "messages": []}}}
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(payload).encode("utf-8"))
    state = _seat_state_cls()(path)
    assert "a|b" in state.sessions
    assert list(tmp_path.glob("seat_local_state.json.bak-*"))


def test_unreadable_seat_state_is_preserved(tmp_path):
    path = tmp_path / "seat_local_state.json"
    path.write_text('{"sessions": {broken', encoding="utf-8")
    state = _seat_state_cls()(path)
    assert state.sessions == {}
    kept = list(tmp_path.glob("seat_local_state.json.unreadable-*"))
    assert kept and "broken" in kept[0].read_text(encoding="utf-8")


def _agent(max_age=600):
    from bridge.agent import BridgeAgent
    agent = object.__new__(BridgeAgent)
    agent.cfg = {"history_report_max_age_seconds": max_age, "agent_id": "a"}
    agent.platform = types.SimpleNamespace(name="pdd")
    return agent


def test_history_max_age_applies_to_log_sources():
    agent = _agent()
    old = {"is_history": True, "ts": time.time() - 3600, "capture_source": "inside", "role": "user"}
    fresh = dict(old, ts=time.time() - 60)
    assert agent._stale_history(old) is True
    assert agent._stale_history(fresh) is False


def test_history_gate_blocks_replays_before_local_workbench():
    agent = _agent()
    now = time.time()
    # 实时先到，之后补拉回放同一条 → 丢
    assert agent.history_gate({"is_history": False, "ts": now}, "e1") is False
    assert agent.history_gate({"is_history": True, "ts": now, "capture_source": "pdd_cdp_history",
                               "role": "user"}, "e1") is True
    # 补拉第一次带回 → 放行并打标；第二轮回放 → 丢
    first = {"is_history": True, "ts": now, "capture_source": "pdd_cdp_history", "role": "user"}
    assert agent.history_gate(first, "e2") is False and first["_history_gate_passed"] is True
    again = {"is_history": True, "ts": now, "capture_source": "pdd_cdp_history", "role": "user"}
    assert agent.history_gate(again, "e2") is True
    # 包装层放行过的，原逻辑里不能再把它判成重复
    assert agent._history_replay_seen("e2", drop_if_seen=False) is False


def test_v1010_empty_ini_logdir_is_not_followed(tmp_path):
    """现场：LogDir=.../3.5.0.40/logs 是空目录，插件写在 plugins/logs。1.0.6–1.0.9 会切到空目录。"""
    runtime = tmp_path / "PddPortableRuntime" / "3.5.0.40"
    ini_logs = runtime / "logs"
    ini_logs.mkdir(parents=True)
    plugin_logs = runtime / "plugins" / "logs"
    _touch(plugin_logs / "3.5.0.40_PddMsgPlugin_x64.dll_2026-11-27.log", age=3)
    resolved, source = r.resolve_active_log_dir(str(plugin_logs), ini_dirs=[str(ini_logs)], candidates=[])
    assert r.same_dir(resolved, str(plugin_logs))
    # 从空目录出发也能找回 plugins/logs
    resolved, source = r.resolve_active_log_dir(str(ini_logs), ini_dirs=[str(ini_logs)], candidates=[])
    assert r.same_dir(resolved, str(plugin_logs)) and source == "pddini"


def test_v1010_describe_reports_candidates(tmp_path):
    d = tmp_path / "logs"
    _touch(d / "logrus.log", age=2)
    info = r.describe(str(d))
    assert info["current"]["files"] == 1 and info["current"]["last_write_age_s"] is not None
