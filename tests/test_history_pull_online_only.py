"""缓存了很多店、只登录一家：未登录店铺的买家不占回扫名额。"""
import threading
import time

from bridge.agent import BridgeAgent


def test_history_pull_skips_offline_shops():
    agent = object.__new__(BridgeAgent)
    agent._stop = threading.Event()
    agent.cfg = {"history_pull_seconds": 0.01, "history_pull_size": 5,
                 "history_pull_max_buyers": 2, "history_pull_gap_seconds": 0}
    calls = []

    class Src:
        def has_online_session(self, account):
            return account == "cs_1:2"

        def pull_history(self, uid, account="", **kwargs):
            calls.append((account, uid))
            return {"ok": True}

    agent.pddbridge_source = Src()
    # 离线店的买家时间戳更新：旧实现 top-2 会被它们占满
    agent._recent_buyers = {("cs_9:9", "x1"): 9.0, ("cs_9:9", "x2"): 8.0,
                            ("cs_1:2", "b1"): 3.0, ("cs_1:2", "b2"): 2.0}
    thread = threading.Thread(target=agent._history_pull_loop, daemon=True)
    thread.start()
    deadline = time.time() + 3
    while len(calls) < 2 and time.time() < deadline:
        time.sleep(0.02)
    agent._stop.set()
    thread.join(timeout=2)
    assert ("cs_1:2", "b1") in calls and ("cs_1:2", "b2") in calls
    assert not [c for c in calls if c[0] == "cs_9:9"]
