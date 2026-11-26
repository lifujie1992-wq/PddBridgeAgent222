# -*- coding: utf-8 -*-
"""1.0.7：拼多多工作台看护（D5）+ 可调配置项不再被丢弃。"""
import json
import threading

from bridge.config import load_config, save_config
from bridge.workbench_watchdog import WorkbenchWatchdog


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _dog(cfg=None, launcher="C:/x/start_pdd_portable.cmd"):
    state = {"pids": [11], "hung": {}, "launched": [], "killed": [], "recovered": 0}
    clock = Clock()

    def recovered():
        state["recovered"] += 1

    dog = WorkbenchWatchdog(
        cfg or {}, threading.Event(), recovered,
        pids=lambda: list(state["pids"]),
        hung=lambda pid: state["hung"].get(pid),
        launcher_finder=lambda configured: launcher,
        do_launch=lambda path: state["launched"].append(path) or True,
        do_kill=lambda pid: state["killed"].append(pid),
        clock=clock,
    )
    return dog, state, clock


def test_relaunches_after_grace_and_recovers():
    dog, st, clock = _dog()
    dog.tick()                       # 见过在跑
    st["pids"] = []                  # 被 taskkill
    dog.tick()
    clock.t += 5
    dog.tick()
    assert st["launched"] == []      # grace 8s 内不动，给探域自己拉起的机会
    clock.t += 4
    dog.tick()
    assert st["launched"] == ["C:/x/start_pdd_portable.cmd"]
    st["pids"] = [22]
    dog.tick()
    assert st["recovered"] == 1


def test_never_launches_if_never_seen_running():
    dog, st, clock = _dog()
    st["pids"] = []
    for _ in range(5):
        dog.tick()
        clock.t += 30
    assert st["launched"] == []


def test_no_launcher_only_warns():
    dog, st, clock = _dog(launcher="")
    dog.tick()
    st["pids"] = []
    dog.tick()
    clock.t += 20
    dog.tick()
    assert st["launched"] == []


def test_hung_workbench_is_killed_after_limit():
    dog, st, clock = _dog({"workbench_hang_kill_seconds": 90})
    st["hung"] = {11: True}
    dog.tick()
    clock.t += 60
    dog.tick()
    assert st["killed"] == []
    clock.t += 31
    dog.tick()
    assert st["killed"] == [11]


def test_relaunch_rate_limited():
    dog, st, clock = _dog({"workbench_min_relaunch_interval_seconds": 60})
    dog.tick()
    st["pids"] = []
    dog.tick()
    clock.t += 10
    dog.tick()
    clock.t += 10
    dog.tick()                       # 刚拉过，不再拉
    assert len(st["launched"]) == 1
    clock.t += 60
    dog.tick()
    assert len(st["launched"]) == 2


def test_disabled_by_config():
    dog, _st, _clock = _dog({"workbench_watchdog_enabled": False})
    assert dog.enabled() is False


def test_tunable_keys_survive_load_and_save(tmp_path):
    path = tmp_path / "bridge_config.json"
    (tmp_path / "logs").mkdir()
    path.write_text(json.dumps({
        "platform": "pdd", "config_version": 1, "tanyu_log_dir": str(tmp_path / "logs"),
        "history_pull_hot_seconds": 5, "workbench_watchdog_enabled": False,
        "workbench_launcher": "D:/a/start_pdd_portable.cmd",
    }), encoding="utf-8")
    cfg = load_config(path)
    assert cfg["history_pull_hot_seconds"] == 5
    assert cfg["workbench_watchdog_enabled"] is False
    save_config(cfg, path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["workbench_launcher"] == "D:/a/start_pdd_portable.cmd"
    assert saved["history_pull_hot_seconds"] == 5
