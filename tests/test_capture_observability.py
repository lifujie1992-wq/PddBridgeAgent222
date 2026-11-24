"""0.7.11：采集可观测性 + 回扫目标表兜底。

现场事故：注入成功、探域没跑、页面 socket=none → 一帧都没收到，
但界面/心跳全显示"正常"，客户只能说"我发了消息本地不显示"。
这里守住三件事：目标表不依赖实时帧、落盘可恢复、零帧能被判定出来。
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from bridge.pddbridge_source import PddbridgeSource


class StubPlatform:
    name = "pdd"


def _agent_stub(tmp: Path):
    """只借 BridgeAgent 的目标表方法，不构造完整 agent（会连中心）。"""
    from bridge.agent import BridgeAgent

    stub = object.__new__(BridgeAgent)
    stub.cfg = {"local_queue_path": str(tmp / "bridge_queue_pdd.jsonl"),
                "history_target_ttl_seconds": 86400}
    stub.platform = StubPlatform()
    stub._recent_buyers = {}
    stub._history_targets_path = None
    stub._history_targets_saved_at = 0.0
    return stub


def test_history_targets_recorded_from_history_frames_too():
    """回扫回来的消息也要进候选表（旧实现只认实时帧 → socket 一死就空表）。"""
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent_stub(Path(tmp))
        agent._note_history_target("cs_1:100", "buyerA", is_history=True)
        agent._note_history_target("cs_2:200", "buyerB", is_history=False)
        assert set(agent._recent_buyers) == {("cs_1:100", "buyerA"), ("cs_2:200", "buyerB")}


def test_history_targets_survive_restart():
    """目标表落盘：重启后回扫还能继续，不会因为冷启动空表而完全不扫。"""
    with tempfile.TemporaryDirectory() as tmp:
        first = _agent_stub(Path(tmp))
        first._note_history_target("cs_1:100", "buyerA")
        first._save_history_targets(force=True)
        path = first._history_targets_file()
        assert path.is_file()
        assert json.loads(path.read_text(encoding="utf-8"))["targets"]

        second = _agent_stub(Path(tmp))
        second._ensure_history_targets()
        assert ("cs_1:100", "buyerA") in second._recent_buyers


def test_history_targets_drop_expired_entries():
    """超过 TTL 的目标不再参与回扫（避免一张表越滚越脏）。"""
    with tempfile.TemporaryDirectory() as tmp:
        agent = _agent_stub(Path(tmp))
        agent.cfg["history_target_ttl_seconds"] = 60
        path = agent._history_targets_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1, "targets": [
            ["cs_1:100", "old", 1.0],            # 早就过期
        ]}), encoding="utf-8")
        agent._ensure_history_targets()
        assert agent._recent_buyers == {}


def test_capture_status_flags_zero_frames():
    """注入了但零帧 → capture.ok=False，reason=no_frames（界面不能再显示正常）。"""
    from bridge.agent import BridgeAgent

    stub = object.__new__(BridgeAgent)
    stub._effective_source = "cdp"
    stat = stub._capture_status({"injected": True, "frames_last_5m": 0,
                                 "last_frame_at": 0,
                                 "sessions": [{"socket_state": "none"}]})
    assert stat["ok"] is False
    assert stat["reason"] == "no_frames"
    assert stat["socket_states"] == ["none"]

    ok = stub._capture_status({"injected": True, "frames_last_5m": 12, "sessions": []})
    assert ok == {"ok": True, "reason": "frames", "frames_last_5m": 12}

    not_inj = stub._capture_status({"injected": False, "frames_last_5m": 0})
    assert not_inj["ok"] is False and not_inj["reason"] == "not_injected"


def test_source_status_exposes_frame_counters():
    """数据源 status 要能回答“最近有没有帧”，而不是只有 injected=True。"""
    src = PddbridgeSource({}, on_event=lambda m: None)
    stat = src.status()
    assert stat["frames_last_5m"] == 0 and stat["frames_total"] == 0
    assert stat["capture_ok"] is False
    assert stat["sessions"] == []


def _log_agent(tmp=None):
    from bridge.agent import BridgeAgent

    stub = object.__new__(BridgeAgent)
    stub._effective_source = "tanyu_logs"
    stub.cfg = {"capture_warn_seconds": 600}
    return stub


def test_log_source_without_files_is_not_ok():
    """探域没装/没跑 → 日志通道连文件都没有，不能再报“正常”。"""
    class W:
        def diagnostics(self):
            return {"files": {}, "totals": {}, "last_line_at": 0.0}

    agent = _log_agent()
    agent.watcher = W()
    stat = agent._capture_status()
    assert stat["ok"] is False and stat["reason"] == "no_log_files"


def test_log_source_idle_is_not_ok():
    """日志文件在、但很久没有新行（探域卡死）→ 同样要告警。"""
    class W:
        def diagnostics(self):
            return {"files": {"a": {}}, "totals": {}, "last_line_at": 1.0}

    agent = _log_agent()
    agent.watcher = W()
    stat = agent._capture_status()
    assert stat["ok"] is False and stat["reason"] == "log_idle"


def test_log_source_recent_activity_is_ok():
    import time as _t

    class W:
        def diagnostics(self):
            return {"files": {"a": {}}, "totals": {}, "last_line_at": _t.time() - 5}

    agent = _log_agent()
    agent.watcher = W()
    stat = agent._capture_status()
    assert stat["ok"] is True and stat["reason"] == "log_source"


def test_dedup_window_covers_history_window():
    """去重窗口必须覆盖回扫窗口，否则回扫会把已上报的消息当新的再传一遍。

    现场实证：18:31:02 一条 8 分钟前的 AI 回复被回扫重新上报（旧窗口写死 120 秒，
    回扫窗口 1800 秒）。
    """
    src = PddbridgeSource({"history_report_max_age_seconds": 1800}, on_event=lambda m: None)
    assert src._dedup_window() == 1800.0

    msg_id = "1795515751927"
    assert src._seen_before(msg_id) is False          # 第一次见
    assert src._seen_before(msg_id) is True           # 立刻重复 → 拦
    # 模拟 3 分钟后再来（旧实现这里会放行）
    src._seen_any[msg_id] = time.time() - 180
    assert src._seen_before(msg_id) is True
    # 超过窗口才允许再报
    src._seen_any[msg_id] = time.time() - 1900
    assert src._seen_before(msg_id) is False

    # 显式配置优先
    src.cfg["dedup_window_seconds"] = 60
    assert src._dedup_window() == 60.0


def test_send_ready_counts_dll_direct_channel():
    """页面没有 JS socket 时 send_capable 恒 False，但 DLL 直发可用 → 不能报"未就绪"。"""
    src = PddbridgeSource({"send_via_dll": True}, on_event=lambda m: None)
    src.state = "listening"
    src._direct_ready = True
    src._direct_probe_at = time.time()      # 命中缓存，不真的去敲管道
    stat = src.status()
    assert stat["dll_send_ready"] is True
    assert stat["send_ready"] is True
    assert stat["page_send_ready"] is False   # 页面确实不可用

    src.cfg["send_via_dll"] = False           # 关掉直发 → 页面不可用就是不可用
    src._direct_probe_at = 0.0
    assert src.status()["send_ready"] is False
