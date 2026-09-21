# -*- coding: utf-8 -*-
"""10 店铺共用一个 CDP 线程时的公平性与隔离。

实测场景（用户环境）：1 台机器、1 个工作台、10 个店铺标签页、峰值每秒好几条消息。
原来 LISTENING 段是：

    for session in list(self.sessions):
        if self._pump_session(session) != "ok":
            self._drop_session(session)

三个问题，全都是**结构性**的、不是偶发超时：
  1. 单次 eval 吃 8s（用的是连接超时，不是调用方给的预算），没有总预算；
  2. 每轮都从 sessions[0] 开始 —— 前面卡住时尾部会话**每轮**都轮不到，稳定饿死；
  3. 一次失败就丢会话（页面偶发卡顿 = 该店铺静默丢消息 ~20s 直到重扫挂回）。
"""
import time

from bridge.pddbridge_source import PddbridgeSource, _Session


class FakeCdp:
    """按表达式返回，并可按会话记账 / 控制耗时。"""

    def __init__(self, source, name, cost=0.0, fail=False):
        self.source = source
        self.name = name
        self.cost = cost
        self.fail = fail
        self.drains = 0

    def eval(self, expression, context_id=None, **_kwargs):
        if "__pddBridge_hooked" in expression:          # drain
            self.drains += 1
            if self.fail:
                raise RuntimeError("eval 卡死/超时")
            if self.cost:
                time.sleep(self.cost)
            return {"hooked": True, "frames": [], "stats": None,
                    "attached": True, "lossless": True}
        if "__pddBridge_info" in expression:            # 账号信息
            return {"csidGuess": "cs_%s:1" % self.name, "globalMallId": self.name}
        if "__pddBridge_ack" in expression:
            return 1
        return None

    def close(self):
        pass


def make_source(names, *, history_only=True, idle=5.0, poll=0.2,
                budget=6.0, fail_limit=3, costs=None):
    cfg = {
        "history_only_idle_poll_seconds": idle,
        "cdp_poll_interval": poll,
        "cdp_round_budget_seconds": budget,
        "cdp_session_fail_limit": fail_limit,
        "pull_drain_window_seconds": 6.0,
        "_history_only": history_only,
    }
    src = PddbridgeSource(cfg, on_event=lambda m: None)
    for index, name in enumerate(names):
        cost = (costs or {}).get(index, 0.0)
        src.sessions.append(
            _Session(57165, "u-%s" % name, FakeCdp(src, name, cost=cost), index + 1, name))
    return src


# ---------------------------------------------------------------- 1. 按需轮询
def test_history_only_idles_when_no_pull_in_flight(monkeypatch):
    """没有 pull 在飞时不该按 poll 频率空转 10 个会话（每秒几十次 eval 全是空结果）。"""
    src = make_source(["a", "b", "c"], idle=5.0, poll=0.2)

    assert src._round_interval() == 5.0

    # 刚发过 pull：窗口内按 poll 快轮，好尽快把 list 应答收回来
    src._last_pull_at = time.monotonic()
    assert src._round_interval() == 0.2

    # 窗口过了再退回 idle
    src._last_pull_at = time.monotonic() - 99
    assert src._round_interval() == 5.0


def test_pull_window_clock_is_monotonic(monkeypatch):
    """量间隔必须用 monotonic。

    墙钟被 NTP 回拨一下，`time.time()` 的差值就变成负数 → 快轮窗口永远关着，
    pull 的应答再也收不回来；拨快则是窗口永远开着、又回到空转。
    """
    src = make_source(["a"])
    src._last_pull_at = 0.0

    # 直接跑一遍动作派发（会话选不中也没关系，时间戳在选会话之前就记了）
    from bridge.pddbridge_source import _SendWaiter
    waiter = _SendWaiter()
    waiter.deadline = time.monotonic() + 1.0
    src._act.append(("pull_history", ("buyer-1", "cs_1:1", 10, "0", 0, "0", waiter)))
    src._handle_actions()

    assert src._last_pull_at != 0.0, "动作应该已经把时间戳记上了"
    assert abs(time.monotonic() - src._last_pull_at) < 60, \
        "_last_pull_at 不是 monotonic 时钟（值 %r；time.time() 会差 10^9 量级）" \
        % src._last_pull_at
    # 而且必须落在单调时钟的尺度上，不是纪元秒
    assert src._last_pull_at < time.time() - 86400


def test_cdp_realtime_mode_always_uses_poll(monkeypatch):
    """CDP 当实时主源时不能被 idle 间隔拖慢 —— 那是主力数据通道。"""
    src = make_source(["a"], history_only=False, idle=5.0, poll=0.2)
    assert src._round_interval() == 0.2


def test_pull_history_wakes_the_pump(monkeypatch):
    """主循环可能正睡在 idle 间隔上；入队不叫醒的话 pull 会一直等到超时。

    idle 间隔默认 5s、pull 的 timeout 默认 5s —— 不叫醒就是必然超时。
    """
    src = make_source(["a"])

    class AliveThread:
        def is_alive(self):
            return True

    src._thread = AliveThread()          # 没有线程时 pull_history 会提前返回，不进队
    assert not src._wake.is_set()

    # 无人执行动作，等一小会就返回 timeout —— 重点看 _wake 有没有被打开
    result = src.pull_history("buyer-1", "cs_1:1", timeout=0.05)

    assert src._wake.is_set(), "动作入队后必须唤醒主循环"
    assert len(src._act) == 1, "动作应已入队"
    assert result["status"] == "timeout"


# ---------------------------------------------------------------- 2. eval 预算
def test_eval_budget_never_exceeds_the_waiters_remaining_time(monkeypatch):
    """动作在主循环线程里同步执行；eval 不能比调用方还在等的时间更长。"""
    from bridge.pddbridge_source import _SendWaiter

    src = make_source(["a"])            # cdp_eval_timeout 缺省 4.0
    waiter = _SendWaiter()

    waiter.deadline = time.monotonic() + 1.0
    assert src._eval_budget(waiter) <= 1.0

    waiter.deadline = time.monotonic() + 30.0
    assert src._eval_budget(waiter) == 4.0       # 被配置预算封顶


def test_eval_budget_treats_missing_deadline_as_no_deadline(monkeypatch):
    """deadline 的类默认是 0.0 = "没设过期"。不能当成"已过期"，
    否则一次发送只拿到 50ms 求值预算，页面稍慢就误报发送失败。"""
    from bridge.pddbridge_source import _SendWaiter

    src = make_source(["a"])
    waiter = _SendWaiter()                        # deadline 保持 0.0

    assert src._eval_budget(waiter) == 4.0


def test_eval_budget_shrinks_when_the_waiter_already_gave_up(monkeypatch):
    """调用方已经不等了：只做最后一次尽力而为，不能继续占着共享线程。"""
    from bridge.pddbridge_source import _SendWaiter

    src = make_source(["a"])
    waiter = _SendWaiter()
    waiter.deadline = time.monotonic() - 5.0

    assert src._eval_budget(waiter) <= 0.1


# ---------------------------------------------------------------- 3. 预算 + 断点
def test_round_stops_at_budget_and_keeps_a_cursor(monkeypatch):
    """预算跑满就停手，并记下断点（而不是下轮再从 0 开始）。"""
    # 每个 drain 花 0.4s，预算 1.0s → 一轮大约只能轮 2~3 个
    src = make_source(["a", "b", "c", "d", "e"], budget=1.0,
                      costs={i: 0.4 for i in range(5)})

    src._pump_round()

    pumped = sum(s.cdp.drains for s in src.sessions)
    assert 0 < pumped < 5, "预算是硬性的，不该一轮把 5 个全做完（实测 %d）" % pumped
    assert src._pump_cursor == pumped % 5, "断点应停在下一次该轮到的那个会话"


def test_without_a_cursor_the_tail_starves():
    """把"每轮都从 0 开始"的算法原样跑一遍，证明尾部确实会被饿死。

    这是被修掉的那个 bug 的最小复现：加预算但不加断点，等于把"偶尔慢"变成
    "后面的店铺永远收不到消息"。这里用假耗时算，不真 sleep。
    """
    costs = [0.4, 0.4, 0.4, 0.0, 0.0]     # 前 3 个每次都吃满预算，后 2 个很快
    budget = 0.5
    pumped = [0] * 5

    for _ in range(10):
        spent = 0.0
        for index in range(5):            # ← 旧实现：每轮都从 sessions[0] 开始
            pumped[index] += 1
            spent += costs[index]
            if spent >= budget:
                break

    assert pumped[3] == 0 and pumped[4] == 0, "尾部应当一次都没轮到（实际 %s）" % pumped


def test_tail_sessions_get_their_turn(monkeypatch):
    """关键性质：前面会话一直很慢时，尾部会话仍能拿到轮次。"""
    src = make_source(["a", "b", "c", "d", "e"], budget=0.5,
                      costs={0: 0.4, 1: 0.4, 2: 0.4})
    fast = [s for s in src.sessions if s.cdp.cost == 0.0]
    assert len(fast) == 2

    src._pump_round()
    assert src._pump_cursor > 0, "预算用光后必须留下断点，否则下轮又从 0 开始"

    for _ in range(8):
        src._pump_round()

    for session in fast:
        assert session.cdp.drains > 0, "%s 一次都没轮到，尾部被饿死了" % session.target_id


# ---------------------------------------------------------------- 3. 会话隔离
def test_one_hung_tab_does_not_cost_others_their_round(monkeypatch):
    """一个 tab 一直失败，不能拖住其余会话的 drain。"""
    src = make_source(["bad", "good1", "good2"], fail_limit=3)
    src.sessions[0].cdp.fail = True

    src._pump_round()

    assert src.sessions[1].cdp.drains == 1
    assert src.sessions[2].cdp.drains == 1
    assert len(src.sessions) == 3, "第一次失败不该直接摘掉会话"


def test_session_dropped_only_after_fail_limit(monkeypatch):
    """连续失败到阈值才丢；页面偶发卡一下不该把整个店铺摘掉。"""
    src = make_source(["bad", "good"], fail_limit=3)
    bad, good = src.sessions[0], src.sessions[1]
    bad.cdp.fail = True

    for _ in range(2):
        src._pump_round()
        assert bad in src.sessions, "未达阈值不该丢会话"
        assert bad.cdp.drains == 1 or bad.cdp.drains == 2

    src._pump_round()          # 第 3 次
    assert bad not in src.sessions, "到达阈值应丢弃"
    assert good in src.sessions, "健康的会话必须留下"


def test_success_resets_the_fail_streak(monkeypatch):
    """偶发失败后恢复成功，计数要清零，否则攒够 3 次偶发就被误摘。"""
    src = make_source(["flaky"], fail_limit=3)
    session = src.sessions[0]

    for _ in range(5):
        session.cdp.fail = True
        src._pump_round()
        assert session.fail_streak == 1
        session.cdp.fail = False
        src._pump_round()
        assert session.fail_streak == 0

    assert session in src.sessions


# ---------------------------------------------------------------- 4. 所有会话都丢
def test_all_sessions_lost_leaves_nothing_to_pump(monkeypatch):
    """全部会话丢失后 sessions 清空 —— _run 靠这个空列表判定"重新扫描"。"""
    src = make_source(["a", "b"], fail_limit=1)
    for session in src.sessions:
        session.cdp.fail = True

    src._pump_round()

    assert src.sessions == []
