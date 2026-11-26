# -*- coding: utf-8 -*-
"""1.0.5：快车道回拉 / 补拉回放去重 / 旧 websocket 地址。"""
import json
import threading
import time

from bridge.agent import BridgeAgent
from bridge.config import load_config


def test_hot_lane_pulls_recent_buyers_between_full_rounds():
    agent = object.__new__(BridgeAgent)
    agent._stop = threading.Event()
    agent.cfg = {"history_pull_seconds": 1000, "history_pull_size": 5,
                 "history_pull_max_buyers": 40, "history_pull_gap_seconds": 0,
                 "history_pull_hot_seconds": 1, "history_pull_hot_window_seconds": 600,
                 "history_pull_hot_max": 2}
    calls = []

    class Src:
        def has_online_session(self, account):
            return True

        def pull_history(self, uid, account="", **kw):
            calls.append(uid)
            return {"ok": True}

    agent.pddbridge_source = Src()
    now = time.time()
    agent._recent_buyers = {("a:1", "hot1"): now - 5, ("a:1", "hot2"): now - 9,
                            ("a:1", "hot3"): now - 20, ("a:1", "cold"): now - 3600}
    t = threading.Thread(target=agent._history_pull_loop, daemon=True)
    t.start()
    deadline = time.time() + 4
    while len(calls) < 2 and time.time() < deadline:
        time.sleep(0.02)
    agent._stop.set()
    t.join(timeout=3)
    # 全量一轮要 1000s 才到：这里拉到的一定是快车道，且只有最近 2 个、不含 cold
    assert calls[:2] == ["hot1", "hot2"]
    assert "cold" not in calls and "hot3" not in calls


def test_stale_websocket_host_follows_server_url(tmp_path):
    path = tmp_path / "bridge_config.json"
    (tmp_path / "logs").mkdir()
    path.write_text(json.dumps({
        "platform": "pdd", "config_version": 1,
        "server_url": "http://47.112.219.14:18765",
        "websocket_url": "ws://47.107.138.228:18765/api/bridge/v1/ws",
        "tanyu_log_dir": str(tmp_path / "logs"),
    }), encoding="utf-8")
    assert load_config(path)["websocket_url"] == ""


def test_custom_websocket_url_is_kept(tmp_path):
    path = tmp_path / "bridge_config.json"
    (tmp_path / "logs").mkdir()
    path.write_text(json.dumps({
        "platform": "pdd", "config_version": 1,
        "server_url": "http://47.112.219.14:18765",
        "websocket_url": "ws://example.test:18765/api/bridge/v1/ws",
        "tanyu_log_dir": str(tmp_path / "logs"),
    }), encoding="utf-8")
    assert load_config(path)["websocket_url"] == "ws://example.test:18765/api/bridge/v1/ws"
