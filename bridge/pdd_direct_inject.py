"""PDD 直发通道：自动注入管理。

参照探域 Injector 与本地千牛 qn_inject 的做法 —— **在工作台进程出现的第一时间注入**，
这样 DLL 里的构造函数钩子能在工作台初始化阶段捕获 CMChatImpl 实例，从而提供：
  - 直发：进程内调用工作台自己的 SendTextMsg（毫秒级、同步返回成功与否）
  - 不再需要模拟打字 + 事后对账

注入需要管理员权限（工作台是 High 完整性）。若本进程已是管理员则静默注入；
否则走 ShellExecuteW("runas") 弹一次 UAC；同一工作台进程只注入一次。
"""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import threading
import time
from ctypes import wintypes
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_PIPE = r"\\.\pipe\pdd_send_bridge"
DEFAULT_DLL = r"D:\temp\pdd-send-hook\out\pdd_send_v3.dll"
DEFAULT_INJECTOR = r"D:\temp\pdd-send-hook\out\injector.exe"
WORKBENCH_EXE = "pddworkbench.exe"


# ---------- 进程发现（纯 stdlib，不依赖 psutil） ----------
class _ME32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("th32ModuleID", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("GlblcntUsage", wintypes.DWORD),
                ("ProccntUsage", wintypes.DWORD), ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
                ("modBaseSize", wintypes.DWORD), ("hModule", wintypes.HMODULE),
                ("szModule", ctypes.c_char * 256), ("szExePath", ctypes.c_char * 260)]


def workbench_pids() -> list[int]:
    """当前所有 PDD 工作台进程 PID。"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    TH32CS_SNAPPROCESS = 0x2
    class _PE32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_char * 260)]
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        return []
    out: list[int] = []
    try:
        pe = _PE32(); pe.dwSize = ctypes.sizeof(_PE32)
        ok = k32.Process32First(snap, ctypes.byref(pe))
        while ok:
            if pe.szExeFile.decode("mbcs", "replace").lower() == WORKBENCH_EXE:
                out.append(int(pe.th32ProcessID))
            ok = k32.Process32Next(snap, ctypes.byref(pe))
    finally:
        k32.CloseHandle(snap)
    return out


def module_loaded(pid: int, dll_name: str) -> bool:
    """目标进程里是否已加载指定模块（注入判定的权威依据）。"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    TH32CS_SNAPMODULE, TH32CS_SNAPMODULE32 = 0x8, 0x10
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if snap == -1:
        return False
    try:
        me = _ME32(); me.dwSize = ctypes.sizeof(_ME32)
        ok = k32.Module32First(snap, ctypes.byref(me))
        while ok:
            if me.szModule.decode("mbcs", "replace").lower() == dll_name.lower():
                return True
            ok = k32.Module32Next(snap, ctypes.byref(me))
    finally:
        k32.CloseHandle(snap)
    return False


# ---------- 权限 ----------
def _is_elevated() -> bool:
    try:
        adv = ctypes.WinDLL("advapi32")
        k32 = ctypes.WinDLL("kernel32")
        ht = wintypes.HANDLE()
        if not adv.OpenProcessToken(k32.GetCurrentProcess(), 0x8, ctypes.byref(ht)):
            return False
        val = wintypes.DWORD(); ret = wintypes.DWORD()
        ok = adv.GetTokenInformation(ht, 20, ctypes.byref(val), ctypes.sizeof(val), ctypes.byref(ret))
        k32.CloseHandle(ht)
        return bool(ok and val.value)
    except Exception:
        return False


# ---------- 注入 ----------
def inject(pid: int, dll: str, injector: str, *, wait_s: float = 20.0) -> tuple[bool, str]:
    """把 DLL 注入指定进程。返回 (是否成功, 说明)。"""
    if not Path(dll).is_file():
        return False, "找不到 DLL: %s" % dll
    if not Path(injector).is_file():
        return False, "找不到注入器: %s" % injector
    args = [str(pid), str(dll)]
    try:
        if _is_elevated():
            p = subprocess.run([injector, *args], capture_output=True, text=True,
                               timeout=wait_s, creationflags=0x08000000)  # CREATE_NO_WINDOW
            ok = p.returncode == 0
            return ok, (p.stdout or p.stderr or "").strip()[-400:]
        # 非管理员：弹一次 UAC（用户点“是”即可）
        shell32 = ctypes.WinDLL("shell32")
        ret = shell32.ShellExecuteW(None, "runas", injector, " ".join(args), None, 0)
        if int(ret) <= 32:
            return False, "提权被拒绝或失败(ShellExecute=%d)，请以管理员运行桥接" % int(ret)
        return True, "已请求提权注入（UAC 确认后生效）"
    except Exception as exc:
        return False, "注入异常: %s" % exc


def pipe_state(pipe: str = DEFAULT_PIPE, timeout_ms: int = 500) -> str | None:
    """读直发管道状态（None = 不可用）。"""
    try:
        from .pdd_send_direct import DirectSender
        return DirectSender(pipe)._exchange("STATE")
    except Exception:
        return None


def ensure_injected(cfg: dict | None = None, *, wait_ready_s: float = 12.0) -> bool:
    """确保工作台已注入且直发就绪。返回是否就绪。"""
    cfg = cfg or {}
    pipe = str(cfg.get("send_via_dll_pipe") or DEFAULT_PIPE)
    dll = str(cfg.get("pdd_dll_path") or DEFAULT_DLL)
    injector = str(cfg.get("pdd_injector_path") or DEFAULT_INJECTOR)
    dll_name = Path(dll).name

    st = pipe_state(pipe)
    if st and st.startswith("READY 1"):
        return True                          # 已注入且已捕获实例
    pids = workbench_pids()
    if not pids:
        log.warning("直发注入：未发现 PDD 工作台进程")
        return False
    pid = pids[0]
    if module_loaded(pid, dll_name):
        log.info("直发注入：DLL 已在工作台 pid=%s 内（等待实例捕获）", pid)
    else:
        ok, msg = inject(pid, dll, injector)
        log.info("直发注入 pid=%s ok=%s %s", pid, ok, msg)
        if not ok:
            return False
    deadline = time.time() + wait_ready_s
    while time.time() < deadline:
        st = pipe_state(pipe)
        if st and st.startswith("READY 1"):
            log.info("直发通道就绪：%s", st)
            return True
        time.sleep(0.5)
    st = pipe_state(pipe)
    log.warning("直发通道未就绪（%s）。工作台若早已启动，构造函数已执行过，"
                "重启一次工作台即可让注入在启动阶段生效", st)
    return False


class InjectWatcher(threading.Thread):
    """盯着工作台进程：一出现就注入（探域 Injector 的同款做法）。"""

    def __init__(self, cfg: dict | None = None, interval: float = 2.0):
        super().__init__(daemon=True, name="pdd-inject-watcher")
        self.cfg = cfg or {}
        self.interval = interval
        self._stop_evt = threading.Event()
        self._last_pid = 0

    def stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:
        pipe = str(self.cfg.get("send_via_dll_pipe") or DEFAULT_PIPE)
        dll = str(self.cfg.get("pdd_dll_path") or DEFAULT_DLL)
        injector = str(self.cfg.get("pdd_injector_path") or DEFAULT_INJECTOR)
        dll_name = Path(dll).name
        while not self._stop_evt.wait(self.interval):
            try:
                st = pipe_state(pipe)
                if st and st.startswith("READY 1"):
                    continue
                for pid in workbench_pids():
                    if pid == self._last_pid and module_loaded(pid, dll_name):
                        continue
                    if module_loaded(pid, dll_name):
                        self._last_pid = pid
                        continue
                    ok, msg = inject(pid, dll, injector)
                    log.info("直发注入（监视器）pid=%s ok=%s %s", pid, ok, msg)
                    self._last_pid = pid
            except Exception as exc:
                log.debug("注入监视器异常: %s", exc)


def status(cfg: dict | None = None) -> dict:
    cfg = cfg or {}
    pipe = str(cfg.get("send_via_dll_pipe") or DEFAULT_PIPE)
    dll = str(cfg.get("pdd_dll_path") or DEFAULT_DLL)
    pids = workbench_pids()
    injected = {pid: module_loaded(pid, Path(dll).name) for pid in pids}
    return {"workbench_pids": pids, "dll": dll, "injected": injected,
            "pipe_state": pipe_state(pipe), "elevated": _is_elevated()}
