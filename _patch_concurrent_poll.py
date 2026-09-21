# -*- coding: utf-8 -*-
"""并发命令轮询：把"取指令间隔"从 10.7 秒压到 ~3.6 秒。

## 为什么

实测（2026-09-21，直连中心 203.0.113.10:18765）：

    TCP 握手          0.015s          ← 网络很快
    /commands 请求    9.14s / 9.16s / 9.19s   ← 每次固定 ~9.15 秒

`wait_seconds=0` 也是 9.14s，`wait_seconds=1.5` 是 11.6s ——
即 **~9.15 秒固定开销 + wait_seconds**。而中心自己的遥测
（`bridge_command_delivery.longpoll.waited_ms`）报的是 1500ms，**中心和它的遥测对不上**。

后果：桥接**每 10.7 秒才能取一次指令**。大脑生成回复后，如果回复指令的有效期短于
这个间隔，它就在桥接取到之前作废了 —— 表现为"工作台看得到回复内容，但客户没收到"
（实测 02:19:31 的回复就丢了，同一条会话 02:21:52 的正常发出）。

**中心侧要修是根本**（`/commands` 不该有这 9 秒固定开销），但在那之前，
桥接可以并发开多条轮询把取指令间隔等比例压下来。

## 安全前提：原子认领

并发之后，**同一条指令可能被两条轮询同时拿到** —— 不处理就会**重复发给买家**。
所以每条指令在执行前必须先**原子认领**（`_claim_command`），已认领的直接跳过。
另外 `_retry_command_results()` 只由一条轮询线跑，避免同一份结果被重复上报。
"""
import io
import sys

P = "bridge/agent.py"
src = io.open(P, encoding="utf-8").read()


def sub(old, new, label, expected=1):
    global src
    n = src.count(old)
    if n != expected:
        sys.exit("%s: 期望 %d 处，实际 %d 处" % (label, expected, n))
    src = src.replace(old, new)
    print("  ok: %s" % label)


# 1) 原子认领
sub(
    "    def _handle_commands(self, *, wait_seconds: float = 0.0) -> None:\n",
    '''    def _claim_command(self, command_id: str) -> bool:
        """原子认领一条指令。

        **并发轮询的安全前提**：多条轮询可能同时拿到同一条指令，不认领就会
        **把同一条回复发给买家两次**。先到先得，后来的直接跳过。
        """
        lock = getattr(self, "_command_claim_lock", None)
        if lock is None:
            lock = self._command_claim_lock = threading.Lock()
            self._command_claimed = set()
        with lock:
            claimed = self._command_claimed
            if command_id in claimed or command_id in self._commands_done:
                return False
            claimed.add(command_id)
            if len(claimed) > 2000:          # 有界，别无限涨
                self._command_claimed = set(list(claimed)[-1000:])
            return True

    def _handle_commands(self, *, wait_seconds: float = 0.0) -> None:
''',
    "_claim_command",
)

# 2) 处理指令前先认领
sub(
    "            counters[\"received\"] += 1\n"
    "            counters[\"last_received_at\"] = time.time()\n"
    "            existing = self.command_journal.get(command_id)\n",
    "            if not self._claim_command(command_id):\n"
    "                # 并发的另一条轮询已经拿到它了（或本机已处理过）——\n"
    "                # 绝不能执行第二遍，否则买家会收到两条一样的回复。\n"
    "                continue\n"
    "            counters[\"received\"] += 1\n"
    "            counters[\"last_received_at\"] = time.time()\n"
    "            existing = self.command_journal.get(command_id)\n",
    "处理前认领",
)

# 3) 并发轮询
sub(
    '''    def _command_loop(self) -> None:
        # 中心若不在长轮询里提前返回，20s 的等待会让 AI 回复排名延迟几十秒
        # （实测“创建→执行”为 4.6s / 30.8s / 48.7s，也是自动命令被判过期的深层原因）。
        # 用配置的 command_poll_seconds（默认 1.5s）并收敛到 0.5–5s，兼顾时延与请求量。
        try:
            poll = float(self.cfg.get("command_poll_seconds") or 1.5)
        except (TypeError, ValueError):
            poll = 1.5
        wait = max(0.5, min(poll, 5.0))
        log.info("命令长轮询已启动（wait_seconds=%.1f）。收到指令会打 "
                 "\\"command <id> type=...\\" 日志；一条都没有 = 指令没下来",
                 wait)
        while not self._stop.is_set():
''',
    '''    def _command_loop(self) -> None:
        # 中心若不在长轮询里提前返回，20s 的等待会让 AI 回复排名延迟几十秒
        # （实测“创建→执行”为 4.6s / 30.8s / 48.7s，也是自动命令被判过期的深层原因）。
        # 用配置的 command_poll_seconds（默认 1.5s）并收敛到 0.5–5s，兼顾时延与请求量。
        try:
            poll = float(self.cfg.get("command_poll_seconds") or 1.5)
        except (TypeError, ValueError):
            poll = 1.5
        wait = max(0.5, min(poll, 5.0))
        # 并发轮询条数。中心那个接口每次固定要 ~9.15 秒（实测，与 wait_seconds 无关），
        # 单线程轮询 = 每 10.7 秒才能取一次指令；开 N 条就把间隔压到约 (9.15+wait)/N。
        # 默认 1（保持原行为）；实测中心有 9 秒固定开销时建议 3。
        try:
            workers = int(self.cfg.get("command_poll_workers") or 1)
        except (TypeError, ValueError):
            workers = 1
        workers = max(1, min(workers, 8))
        log.info("命令长轮询已启动（wait_seconds=%.1f，并发 %d 条）。收到指令会打 "
                 "\\"command <id> type=...\\" 日志；一条都没有 = 指令没下来",
                 wait, workers)
        if workers > 1:
            # 补报结果只由主线程跑，避免同一份结果被 N 条线重复上报。
            self._skip_result_retry = True
            for index in range(workers - 1):
                threading.Thread(target=self._command_poll_worker,
                                 args=(wait,), name="cmd-poll-%d" % index,
                                 daemon=True).start()
        while not self._stop.is_set():
''',
    "并发轮询",
)

# 4) worker + 让 _handle_commands 支持跳过补报
sub(
    "    def _heartbeat(self) -> None:\n",
    '''    def _command_poll_worker(self, wait: float) -> None:
        """并发的取指令线程（只取不补报，补报由主线程负责）。"""
        while not self._stop.is_set():
            try:
                self._handle_commands(wait_seconds=wait)
            except Exception as exc:
                self._last_error = f"handle_commands(worker): {exc}"
                log.exception("命令处理异常（并发轮询线程继续）")
            self._stop.wait(0.2)

    def _heartbeat(self) -> None:
''',
    "_command_poll_worker",
)

sub(
    "        retry_started = time.monotonic()\n"
    "        self._retry_command_results()\n"
    "        retry_elapsed = time.monotonic() - retry_started\n",
    "        retry_started = time.monotonic()\n"
    "        if not getattr(self, \"_skip_result_retry\", False):\n"
    "            self._retry_command_results()\n"
    "        retry_elapsed = time.monotonic() - retry_started\n",
    "并发线跳过补报",
)

io.open(P, "w", encoding="utf-8", newline="").write(src)
print("完成")
