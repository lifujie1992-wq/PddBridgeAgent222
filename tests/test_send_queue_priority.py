"""发送不能排在补拉历史后面；排队超时没执行的要报"未发出"。

90科技 2026-11-25 DESKTOP-R45KXY7：AI 回复在买家发言后 ~5s 下发，正撞上同会话补拉，
5 秒内没轮到执行就被撤销，却被报成"已提交工作台但未收到发送回执"（实际根本没发）。
"""
import threading
import time
from types import SimpleNamespace

from bridge.pddbridge_source import PddbridgeSource, _SendWaiter, _Session, LISTENING


def _source_with_session():
    source = PddbridgeSource({})
    source.state = LISTENING
    session = _Session(57165, "u", SimpleNamespace(eval=lambda *a, **k: None), 1)
    session.mall_id = "123"
    source.sessions = [session]
    return source, session


def test_send_runs_before_queued_history_pulls():
    source, session = _source_with_session()
    order = []
    source._do_pull_history = lambda *a: order.append("pull")
    source._do_send_action = lambda *a: order.append("send")
    for i in range(3):
        source._act.append(("pull_history", ("b%d" % i, "cs_123:456", 10, "", 0, "", None)))
    source._act.append(("send", ("buyer", "hi", "cs_123:456", _SendWaiter(), 5, None)))
    source._handle_actions()
    assert session.send_actions.qsize() == 1
    assert session.actions.qsize() == 3
    source._run_session_actions(session)
    assert order[0] == "send", order
    assert order.count("pull") == 3


def test_send_arriving_between_pulls_jumps_ahead():
    source, session = _source_with_session()
    order = []

    def pull(*args):
        order.append("pull")
        if len(order) == 1:   # 第一个补拉执行期间来了一条发送
            session.send_actions.put(("send", ("buyer", "hi", "cs_123:456", _SendWaiter(), 5, None)))

    source._do_pull_history = pull
    source._do_send_action = lambda *a: order.append("send")
    for i in range(3):
        session.actions.put(("pull_history", ("b%d" % i, "cs_123:456", 10, "", 0, "", None)))
    source._run_session_actions(session)
    assert order[:2] == ["pull", "send"], order


def test_never_executed_send_reports_not_sent():
    source, session = _source_with_session()
    source._thread = SimpleNamespace(is_alive=lambda: True)

    def dispatch_but_never_run():
        time.sleep(0.05)
        source._handle_actions()          # 派发到店铺发送队列，但 worker 一直没轮到

    threading.Thread(target=dispatch_but_never_run, daemon=True).start()
    result = source.send_and_wait("buyer", "hi", "cs_123:456", timeout=0.3)
    assert result["status"] == "failed"
    assert result["not_sent"] is True
    assert result["retryable"] is True
    assert result["real_send"] is False
    assert "未发出" in result["error_user"]
    assert session.send_actions.qsize() == 0, "超时的发送必须撤回，不能之后再发出去"
    assert source.status()["send_queue_timeouts"] == 1


def test_undispatched_send_reports_not_sent():
    source, _ = _source_with_session()
    source._thread = SimpleNamespace(is_alive=lambda: True)
    result = source.send_and_wait("buyer", "hi", "cs_123:456", timeout=0.1)
    assert result["status"] == "failed"
    assert result["not_sent"] is True
    assert source._act == []


def test_executed_send_without_receipt_stays_indeterminate():
    source, session = _source_with_session()
    source._thread = SimpleNamespace(is_alive=lambda: True)

    def dispatch_and_execute():
        time.sleep(0.05)
        source._handle_actions()
        # worker 取走并执行了发送（页面已受理），但一直没等到回执
        session.send_actions.get_nowait()

    threading.Thread(target=dispatch_and_execute, daemon=True).start()
    result = source.send_and_wait("buyer", "hi", "cs_123:456", timeout=0.3)
    assert result["status"] == "indeterminate"
    assert result.get("not_sent") is None
