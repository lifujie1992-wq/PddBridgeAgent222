# -*- coding: utf-8 -*-
"""拼多多工作台看护（1.0.7，交接单 D5）。

现场：21:31 工作台卡死被系统关掉、23:58 被探域 restart.bat 的 taskkill 杀掉，
桥接都不管，补拉整链停摆 26 分钟，期间零消息。

规则（都可配）：
  * 每 5s 看一次 PddWorkbench.exe；
  * 只有「本次桥接运行期间见过它在跑」才会拉起 —— 开机时客户自己没开工作台，不替他开；
  * 消失后等 grace（默认 8s，给探域自己拉起的机会），还没回来就用启动器拉起；
  * 主窗口连续无响应 hang_kill 秒（默认 90s）→ 结束进程，下一轮按「消失」处理；
  * 两次拉起至少间隔 min_interval（默认 60s），每小时最多 6 次，防止拉起风暴；
  * 启动器必须是 90 科技的 start_pdd_portable.cmd（内含 MD5 校验回滚 + 只读锁），
    绝不直接 Start-Process PddWorkbench.exe；找不到启动器就只告警、不拉起。
"""
from __future__ import annotations

import glob
import logging
import os
import subprocess
import threading
import time
from typing import Callable, List, Optional

log = logging.getLogger("pdd.bridge")

WORKBENCH = "pddworkbench.exe"
LAUNCHER_NAME = "start_pdd_portable.cmd"


def find_launcher(configured: str = "") -> str:
    if configured:
        return configured if os.path.isfile(configured) else ""
    roots: List[str] = []
    for env in ("USERPROFILE", "PUBLIC"):
        base = os.environ.get(env)
        if base:
            roots.append(os.path.join(base, "Desktop"))
    users = os.path.join(os.environ.get("SystemDrive", "C:") + os.sep, "Users")
    found: List[str] = []
    for root in roots:
        found += glob.glob(os.path.join(root, LAUNCHER_NAME))
        found += glob.glob(os.path.join(root, "*", LAUNCHER_NAME))
    if not found:
        found += glob.glob(os.path.join(users, "*", "Desktop", "*", LAUNCHER_NAME))
    found = [f for f in found if os.path.isfile(f)]
    if not found:
        return ""
    found.sort(key=os.path.getmtime, reverse=True)
    return found[0]


def workbench_pids() -> List[int]:
    try:
        import psutil  # type: ignore
    except Exception:
        return []
    pids = []
    try:
        for proc in psutil.process_iter(["name", "pid"]):
            if str(proc.info.get("name") or "").lower() == WORKBENCH:
                pids.append(int(proc.info["pid"]))
    except Exception:
        pass
    return pids


def is_hung(pid: int) -> Optional[bool]:
    """pid 的可见顶层窗口是否全部无响应（IsHungAppWindow）。没窗口/判断不了 → None。"""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None
    user32 = ctypes.windll.user32
    windows: List[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4):
            windows.append(hwnd)
        return True

    try:
        user32.EnumWindows(_cb, 0)
    except Exception:
        return None
    if not windows:
        return None
    return all(bool(user32.IsHungAppWindow(h)) for h in windows)


def launch(launcher: str) -> bool:
    try:
        subprocess.Popen(
            ["cmd.exe", "/c", launcher],
            cwd=os.path.dirname(launcher) or None,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
            close_fds=True,
        )
        return True
    except Exception as exc:
        log.warning("工作台看护：启动器运行失败 %s: %s", launcher, exc)
        return False


def kill(pid: int) -> None:
    try:
        import psutil  # type: ignore
        psutil.Process(pid).kill()
    except Exception as exc:
        log.warning("工作台看护：结束卡死进程失败 pid=%s: %s", pid, exc)


class WorkbenchWatchdog:
    def __init__(self, cfg: dict, stop: threading.Event, on_recovered: Callable[[], None] = lambda: None,
                 *, pids=workbench_pids, hung=is_hung, launcher_finder=find_launcher,
                 do_launch=launch, do_kill=kill, clock=time.monotonic):
        self.cfg = cfg
        self.stop = stop
        self.on_recovered = on_recovered
        self._pids, self._hung, self._find = pids, hung, launcher_finder
        self._launch, self._kill, self._clock = do_launch, do_kill, clock
        self.seen_running = False
        self.missing_since = 0.0
        self.hung_since: dict = {}
        self.launches: List[float] = []
        self.waiting_recovery = False
        self._warned_no_launcher = False

    def _f(self, key: str, default: float) -> float:
        try:
            value = self.cfg.get(key)
            return float(default if value is None else value)
        except (TypeError, ValueError):
            return default

    def enabled(self) -> bool:
        value = self.cfg.get("workbench_watchdog_enabled", True)
        return str(value).strip().lower() not in {"0", "false", "no", "off", ""}

    def tick(self) -> None:
        now = self._clock()
        pids = self._pids()
        if pids:
            if self.waiting_recovery:
                self.waiting_recovery = False
                log.info("工作台看护：拼多多工作台已回来 pids=%s，立即重连并补拉一轮", pids)
                try:
                    self.on_recovered()
                except Exception as exc:
                    log.debug("on_recovered failed: %s", exc)
            if not self.seen_running:
                log.info("工作台看护：已发现拼多多工作台 pids=%s", pids)
            self.seen_running = True
            self.missing_since = 0.0
            self._check_hang(pids, now)
            return
        self.hung_since.clear()
        if not self.seen_running:
            return
        if not self.missing_since:
            self.missing_since = now
            self.waiting_recovery = True
            log.warning("工作台看护：拼多多工作台不见了（被关闭/被杀/崩溃），%.0fs 后仍未恢复就自动拉起",
                        self._f("workbench_relaunch_grace_seconds", 8.0))
            return
        if now - self.missing_since < self._f("workbench_relaunch_grace_seconds", 8.0):
            return
        self._relaunch(now)

    def _check_hang(self, pids: List[int], now: float) -> None:
        limit = self._f("workbench_hang_kill_seconds", 90.0)
        for pid in list(self.hung_since):
            if pid not in pids:
                self.hung_since.pop(pid, None)
        for pid in pids:
            hung = self._hung(pid)
            if not hung:
                self.hung_since.pop(pid, None)
                continue
            first = self.hung_since.setdefault(pid, now)
            if first == now:
                log.warning("工作台看护：拼多多工作台无响应 pid=%s", pid)
            if limit > 0 and now - first >= limit:
                log.warning("工作台看护：工作台已连续无响应 %.0fs，结束进程 pid=%s 并重新拉起", now - first, pid)
                self.hung_since.pop(pid, None)
                self._kill(pid)

    def _relaunch(self, now: float) -> None:
        min_gap = self._f("workbench_min_relaunch_interval_seconds", 60.0)
        self.launches = [t for t in self.launches if now - t < 3600]
        if self.launches and now - self.launches[-1] < min_gap:
            return
        if len(self.launches) >= 6:
            if len(self.launches) == 6:
                log.warning("工作台看护：一小时内已拉起 6 次仍不稳定，暂停自动拉起，请人工检查")
                self.launches.append(now)
            return
        launcher = self._find(str(self.cfg.get("workbench_launcher") or ""))
        if not launcher:
            if not self._warned_no_launcher:
                self._warned_no_launcher = True
                log.warning("工作台看护：没找到 %s（可在配置 workbench_launcher 指定），不自动拉起，"
                            "请手动打开拼多多工作台", LAUNCHER_NAME)
            return
        self.launches.append(now)
        log.warning("工作台看护：自动拉起拼多多工作台：%s", launcher)
        self._launch(launcher)

    def run(self, interval: float = 5.0) -> None:
        if not self.enabled():
            log.info("工作台看护已关闭（workbench_watchdog_enabled=false）")
            return
        while not self.stop.wait(interval):
            try:
                self.tick()
            except Exception as exc:
                log.debug("workbench watchdog tick failed: %s", exc)
