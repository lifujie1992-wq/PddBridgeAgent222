"""20 店场景：每个店铺独立 worker、单店退避、动作派发不互相阻塞。

背景：旧实现是主循环里 `for session: _pump_session(session)` 串行 eval，
任何一家店卡到 8s 就把其余 19 家全部冻住；一次失败还直接 `_drop_session`。
客户现场 15 个账号时连锁 `cdp eval 无响应`、2 分钟掉到 2 个账号，就是这个原因。
"""
from __future__ import annotations

import threading
import time

from bridge.pddbridge_source import PddbridgeSource, _Session


class FakeCdp:
    """记录调用参数；可配置抛异常。"""

    def __init__(self, timeout_seen=None, fail=False):
        self.calls = []
        self.timeout_seen = timeout_seen if timeout_seen is not None else []
        self.fail = fail
        self.closed = False

    def eval(self, expr, cid=None, timeout=None):
        self.calls.append(expr)
        self.timeout_seen.append(timeout)
        if self.fail:
            raise RuntimeError("cdp eval 无响应(连接断开/超时)")
        return {"ok": True}

    def close(self):
        self.closed = True


def make_session(cdp=None, account="cs_1:100", port=57165, cid=1):
    cdp = cdp or FakeCdp()
    session = _Session(port, "https://mms.pinduoduo.com/win-plugin", cdp, cid, "t%d" % cid)
    session.account = account
    return session


def test_eval_uses_configured_timeout_and_serializes():
    """eval 超时必须可配（默认 3s）；锁保证同一连接不被两条线程同时用。"""
    order = []
    src = PddbridgeSource({}, on_event=lambda m: None)
    cdp = FakeCdp()

    class SlowCdp(FakeCdp):
        def eval(self, expr, cid=None, timeout=None):
            order.append("start")
            time.sleep(0.05)
            order.append("end")
            return {"ok": True}

    session = make_session(SlowCdp())
    threads = [threading.Thread(target=src._eval, args=(session, "x")) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 串行：start/end 必须交替出现，不能交错
    assert order == ["start", "end"] * 3
    assert session.lock.acquire(timeout=0.1)
    session.lock.release()

    src._eval(make_session(cdp), "y")
    assert cdp.timeout_seen == [3.0]
    assert src._eval_timeout() == 3.0


def test_actions_are_dispatched_to_the_matching_shop_only():
    """发送/拉历史派发到目标店铺自己的队列，主循环不阻塞其他店铺。"""
    src = PddbridgeSource({}, on_event=lambda m: None)
    a = make_session(account="cs_1:100", cid=1)
    b = make_session(account="cs_2:200", cid=2)
    src.sessions = [a, b]

    class Ev:
        def __init__(self):
            self.result = None
            self.expired = lambda self=None: False

        def set(self):
            pass

    event, event2 = Ev(), Ev()
    event.expired = lambda: False
    event2.expired = lambda: False
    with src._lock:
        src._act.append(("send", ("buyer1", "hi", "cs_2:200", event, 5.0, None)))
        src._act.append(("pull_history", ("buyer2", "cs_1:100", 20, 0, 0, 0, event2)))
    assert src._handle_actions() is False
    # 发送进目标店铺自己的发送队列（优先执行），拉历史进普通队列；互不串店
    assert a.actions.qsize() == 1 and a.send_actions.qsize() == 0
    assert b.send_actions.qsize() == 1 and b.actions.qsize() == 0
    name_a, args_a = a.actions.get_nowait()
    name_b, _ = b.send_actions.get_nowait()
    assert (name_a, args_a[1]) == ("pull_history", "cs_1:100")
    assert name_b == "send"


def test_single_shop_failure_backs_off_and_spares_the_others():
    """一家店连续失败只退避它自己（1→2→4→…→上限），其他店铺不受影响。"""
    src = PddbridgeSource({"cdp_session_backoff_max_seconds": 8}, on_event=lambda m: None)
    broken = make_session(account="cs_9:900", cid=9)
    ok = make_session(account="cs_1:100", cid=1)
    src.sessions = [broken, ok]

    delays = [src._note_session_failure(broken) for _ in range(5)]
    assert delays == [1.0, 2.0, 4.0, 8.0, 8.0]     # 封顶 8s（配置值）
    assert broken.failures == 5
    assert broken.next_attempt > time.time()
    assert ok.failures == 0 and ok.next_attempt == 0.0


def test_drop_session_removes_only_that_shop():
    src = PddbridgeSource({}, on_event=lambda m: None)
    a = make_session(account="cs_1:100", cid=1)
    b = make_session(account="cs_2:200", cid=2)
    src.sessions = [a, b]
    src._drop_session(a)
    assert src.sessions == [b]
    assert a.cdp.closed and not b.cdp.closed


def test_frame_handling_is_serialized_across_shops():
    """多店并行后帧处理必须串行（去重表/待确认表不是线程安全的）。"""
    src = PddbridgeSource({}, on_event=lambda m: None)
    inside = []
    overlap = []

    def slow_handle(e, session=None):
        inside.append(1)
        if len(inside) > 1:
            overlap.append(True)
        time.sleep(0.02)
        inside.pop()

    src._handle_frame_locked = slow_handle
    threads = [threading.Thread(target=src._handle_frame, args=({"dir": "in", "data": "{}"},))
               for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert overlap == []
