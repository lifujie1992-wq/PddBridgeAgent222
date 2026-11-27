# -*- coding: utf-8 -*-
"""1.0.10：自带诊断随心跳上报，且不带买家消息内容。"""
import json
import threading
import time
import types

from bridge.agent import BridgeAgent


def _agent(tmp_path):
    agent = object.__new__(BridgeAgent)
    agent.cfg = {"local_queue_path": str(tmp_path / "bridge_queue_pdd.jsonl"), "agent_id": "a"}
    agent.platform = types.SimpleNamespace(name="pdd")
    agent._pending_lock = threading.RLock()
    agent._pending = []
    agent.watcher = types.SimpleNamespace(log_dir=str(tmp_path), _paths={})
    return agent


def test_snapshot_counts_channels_without_content(tmp_path):
    agent = _agent(tmp_path)
    now = time.time()
    agent._diag_note_event({"capture_source": "inside", "ts": now - 0.4, "captured_at": now,
                            "role": "user", "content": "我的手机号13800000000"})
    agent._diag_note_event({"capture_source": "pdd_cdp_history", "ts": now - 40, "captured_at": now,
                            "role": "user", "is_history": True, "history_reason": "replay",
                            "content": "秘密"})
    agent._diag_note_event({"capture_source": "pdd_history_pull", "ts": now - 8, "captured_at": now,
                            "role": "user", "history_reason": "fresh_pull_promoted"})
    snap = agent._diagnostics_snapshot()
    text = json.dumps(snap, ensure_ascii=False)
    assert "13800000000" not in text and "秘密" not in text
    ch = snap["channels_30m"]
    assert ch["inside"]["live"] == 1 and ch["inside"]["delay_p50"] < 1
    assert ch["pdd_cdp_history"]["history"] == 1 and ch["pdd_cdp_history"]["reasons"] == {"replay": 1}
    assert ch["pdd_history_pull"]["promoted"] == 1
    assert len(text) < 8000


def test_diagnostics_file_is_written(tmp_path):
    agent = _agent(tmp_path)
    agent._write_diagnostics_file()
    data = json.loads((tmp_path / "logs" / "diagnostics.json").read_text(encoding="utf-8"))
    assert data["v"] == 1 and "channels_30m" in data
