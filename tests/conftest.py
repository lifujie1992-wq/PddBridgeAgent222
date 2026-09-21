# -*- coding: utf-8 -*-
"""测试环境兜底：Linux 测试机上没有 Windows 侧依赖 `psutil`。

只在**真实 psutil 缺席**时注入一个最小替身（现有测试用 `object.__new__` 造 agent，
不会真的去枚举进程）。Windows 打包/生产环境永远用真包，行为不受影响。
"""
import sys
import types

try:  # pragma: no cover - 环境有关
    import psutil  # noqa: F401
except ImportError:  # pragma: no cover - 只在缺依赖的测试机上走到
    shim = types.ModuleType("psutil")

    class Error(Exception):
        pass

    class NoSuchProcess(Error):
        pass

    class AccessDenied(Error):
        pass

    class TimeoutExpired(Error):
        pass

    class ZombieProcess(NoSuchProcess):
        pass

    class Process:  # noqa: D401 - 最小替身
        def __init__(self, pid=None):
            self.pid = pid
            self.info = {}

        def status(self):
            return "running"

        def connections(self, kind=None):
            return []

        def net_connections(self, kind=None):
            return []

        def children(self, recursive=False):
            return []

        def cmdline(self):
            return []

        def name(self):
            return ""

        def exe(self):
            return ""

        def cwd(self):
            return ""

        def parent(self):
            return None

        def is_running(self):
            return False

        def terminate(self):
            return None

        def kill(self):
            return None

        def wait(self, timeout=None):
            return 0

    shim.Error = Error
    shim.NoSuchProcess = NoSuchProcess
    shim.AccessDenied = AccessDenied
    shim.TimeoutExpired = TimeoutExpired
    shim.ZombieProcess = ZombieProcess
    shim.Process = Process
    shim.process_iter = lambda attrs=None: iter(())
    shim.wait_procs = lambda procs, timeout=None: ([], list(procs or []))
    shim.net_connections = lambda kind=None: []
    shim.pids = lambda: []
    shim.CONN_LISTEN = "LISTEN"
    shim.CONN_ESTABLISHED = "ESTABLISHED"
    sys.modules["psutil"] = shim
