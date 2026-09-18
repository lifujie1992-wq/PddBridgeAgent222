"""自动注入模块单测：进程发现 / 模块判定 / 注入决策 / 监视器（全部用假实现，不真注入）。"""
import threading

from bridge import pdd_direct_inject as inj


def test_workbench_pids_returns_list():
    pids = inj.workbench_pids()
    assert isinstance(pids, list)
    assert all(isinstance(p, int) for p in pids)


def test_pipe_state_none_when_unavailable(monkeypatch):
    class Bad:
        def __init__(self, *a, **k):
            pass

        def _exchange(self, line):
            raise OSError("no pipe")

    monkeypatch.setattr(inj, "pipe_state", lambda *a, **k: None)
    assert inj.pipe_state("\\\\.\\pipe\\nope") is None


def test_ensure_injected_short_circuits_when_ready(monkeypatch):
    monkeypatch.setattr(inj, "pipe_state", lambda *a, **k: "READY 1 INSTANCE 0x1234 CAPTURE 0 HOOKS 0")
    called = []
    monkeypatch.setattr(inj, "inject", lambda *a, **k: (called.append(1), (True, ""))[1])
    assert inj.ensure_injected({}) is True
    assert called == []          # 已就绪就不该再注入


def test_ensure_injected_skips_when_no_workbench(monkeypatch):
    monkeypatch.setattr(inj, "pipe_state", lambda *a, **k: None)
    monkeypatch.setattr(inj, "workbench_pids", lambda: [])
    assert inj.ensure_injected({}) is False


def test_ensure_injected_injects_then_waits_ready(monkeypatch):
    seq = {"n": 0}

    def fake_state(*a, **k):
        seq["n"] += 1
        return None if seq["n"] == 1 else "READY 1 INSTANCE 0x1 CAPTURE 0 HOOKS 0"

    monkeypatch.setattr(inj, "pipe_state", fake_state)
    monkeypatch.setattr(inj, "workbench_pids", lambda: [4242])
    monkeypatch.setattr(inj, "module_loaded", lambda pid, name: False)
    seen = {}
    monkeypatch.setattr(inj, "inject", lambda pid, dll, exe: (seen.update(pid=pid, dll=dll), (True, "ok"))[1])
    monkeypatch.setattr(inj.time, "sleep", lambda s: None)
    assert inj.ensure_injected({}) is True
    assert seen["pid"] == 4242


def test_ensure_injected_ignores_missing_dll(monkeypatch):
    monkeypatch.setattr(inj, "pipe_state", lambda *a, **k: None)
    monkeypatch.setattr(inj, "workbench_pids", lambda: [1])
    monkeypatch.setattr(inj, "module_loaded", lambda pid, name: False)
    ok, msg = inj.inject(1, r"Z:\nope\missing.dll", r"Z:\nope\injector.exe")
    assert ok is False and "找不到 DLL" in msg


def test_watcher_stops_cleanly():
    w = inj.InjectWatcher({"send_via_dll": True}, interval=0.01)
    w.start()
    assert w.is_alive()
    w.stop()
    w.join(timeout=2)
    assert not w.is_alive()


def test_status_shape():
    s = inj.status({})
    assert set(s) >= {"workbench_pids", "dll", "injected", "pipe_state", "elevated"}
    assert isinstance(s["injected"], dict)
