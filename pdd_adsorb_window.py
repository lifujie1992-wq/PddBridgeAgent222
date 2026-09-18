from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil


APP_NAME = "PddAdsorbWindow"
APP_TITLE = "拼多多聚合接待"
DEFAULT_PORTS = tuple(range(18767, 18777))
DEFAULT_TARGET_NAMES = {"pddworkbench.exe", "pddwebworkbench.exe"}


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = app_root()
STATE_DIR = ROOT / "state"
PROFILE_DIR = STATE_DIR / "pdd-adsorb-edge-profile"
PID_PATH = STATE_DIR / "pdd-adsorb.pid"
LOG_PATH = ROOT / "logs" / "pdd_adsorb.log"
CONTROL_PATH = ROOT / "data" / "pdd_adsorb_control.json"
WAKE_PATH = STATE_DIR / "pdd-dock-wake"


def log(message: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 1_000_000:
            backup = LOG_PATH.with_suffix(".log.1")
            if backup.exists():
                backup.unlink()
            os.replace(LOG_PATH, backup)
        with LOG_PATH.open("a", encoding="utf-8") as stream:
            stream.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")
    except OSError:
        pass


def request_wake(seconds: float = 10.0) -> None:
    """Ask the running controller to keep the dock visible for a grace period."""
    try:
        WAKE_PATH.parent.mkdir(parents=True, exist_ok=True)
        WAKE_PATH.write_text(str(time.time() + max(1.0, seconds)), encoding="ascii")
    except OSError as exc:
        log(f"wake request failed: {exc}")


def wake_pending() -> bool:
    try:
        return float(WAKE_PATH.read_text(encoding="ascii").strip()) > time.time()
    except (OSError, ValueError):
        return False


def load_dock_control() -> dict[str, bool]:
    try:
        payload = json.loads(CONTROL_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "pin": payload.get("pin") if isinstance(payload.get("pin"), bool) else True,
        "adsorb": payload.get("adsorb") if isinstance(payload.get("adsorb"), bool) else True,
    }


def notify_error(message: str) -> None:
    log("ERROR " + message)
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, APP_TITLE, 0x10)
    elif sys.stderr is not None:
        print(message, file=sys.stderr)


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    pid: int
    title: str
    class_name: str
    rect: Rect
    visible: bool
    minimized: bool


@dataclass(frozen=True)
class ProcessInfo:
    name: str
    path: str


def choose_dock_rect(target: Rect, work_area: Rect, width: int, gap: int = 4) -> Rect:
    width = min(max(240, width), max(240, work_area.width))
    height = min(max(320, target.height), work_area.height)
    top = max(work_area.top, min(target.top, work_area.bottom - height))
    if work_area.right - target.right >= width + gap:
        left = target.right + gap
    elif target.left - work_area.left >= width + gap:
        left = target.left - width - gap
    else:
        left = max(work_area.left, min(target.right - width, work_area.right - width))
    return Rect(left, top, left + width, top + height)


def choose_fallback_rect(work_area: Rect, width: int) -> Rect:
    width = min(max(240, width), work_area.width)
    height = min(760, work_area.height)
    return Rect(work_area.right - width, work_area.top, work_area.right, work_area.top + height)


def select_target_window(
    windows: list[WindowInfo],
    processes: dict[int, ProcessInfo],
    configured_names: set[str] | None = None,
    title_keywords: tuple[str, ...] = ("拼多多", "接待中心", "商家工作台"),
) -> WindowInfo | None:
    names = {item.lower() for item in (configured_names or DEFAULT_TARGET_NAMES)}
    candidates: list[tuple[int, WindowInfo]] = []
    for item in windows:
        if not item.visible or item.rect.width < 650 or item.rect.height < 420:
            continue
        process = processes.get(item.pid, ProcessInfo("", ""))
        name = process.name.lower()
        path = process.path.lower()
        if name in {"aliworkbench.exe", "msedge.exe", "pddadsorbwindow.exe"}:
            continue
        exact_process = name in names
        pdd_process = "pdd" in name or "pdd" in path
        title_match = any(keyword and keyword in item.title for keyword in title_keywords)
        qt_reception = item.class_name.lower().startswith("qt") and title_match
        if not exact_process and not (pdd_process and (title_match or qt_reception)):
            continue
        score = 100 if exact_process else 50
        if "接待中心" in item.title:
            score += 20
        if "拼多多" in item.title:
            score += 10
        candidates.append((score, item))
    if not candidates:
        return None
    return max(candidates, key=lambda row: (row[0], row[1].rect.width * row[1].rect.height))[1]


def select_dock_window(
    windows: list[WindowInfo],
    edge_pids: set[int],
) -> WindowInfo | None:
    candidates = [
        item for item in windows
        if item.pid in edge_pids and item.class_name.startswith("Chrome_WidgetWin")
    ]
    if not candidates:
        return None
    titled = [item for item in candidates if item.title.strip()]
    preferred = [
        item for item in titled
        if "聚合接待" in item.title or "拼多多" in item.title or APP_TITLE in item.title
    ]
    if preferred or titled:
        # Minimized Edge app windows use a 160x28 system rectangle. The title
        # still identifies the real content window over the blank black host.
        return max(preferred or titled, key=lambda item: item.rect.width * item.rect.height)
    visible_sized = [
        item for item in candidates
        if item.visible and item.rect.width >= 200 and item.rect.height >= 250
    ]
    return max(visible_sized, key=lambda item: item.rect.width * item.rect.height) if visible_sized else None


class Win32:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    enum_proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    user32.EnumWindows.argtypes = (enum_proc_type, wintypes.LPARAM)
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = (wintypes.HWND,)
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = ()
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.MonitorFromWindow.argtypes = (wintypes.HWND, wintypes.DWORD)
    user32.MonitorFromWindow.restype = wintypes.HANDLE
    user32.GetMonitorInfoW.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    user32.SystemParametersInfoW.argtypes = (
        wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT
    )
    user32.SystemParametersInfoW.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = (
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    )
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.ShowWindow.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = (
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )
    user32.PostMessageW.restype = wintypes.BOOL
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    SW_HIDE = 0
    SW_SHOWNOACTIVATE = 4
    SW_SHOW = 5
    SW_RESTORE = 9
    SWP_NOACTIVATE = 0x0010
    SWP_SHOWWINDOW = 0x0040
    HWND_TOPMOST = wintypes.HWND(-1)
    HWND_NOTOPMOST = wintypes.HWND(-2)
    WM_CLOSE = 0x0010
    MONITOR_DEFAULTTONEAREST = 2
    ERROR_ALREADY_EXISTS = 183

    @classmethod
    def windows(cls) -> list[WindowInfo]:
        rows: list[WindowInfo] = []

        @cls.enum_proc_type
        def visit(hwnd: int, _lparam: int) -> bool:
            length = cls.user32.GetWindowTextLengthW(hwnd)
            title_buffer = ctypes.create_unicode_buffer(max(1, length + 1))
            cls.user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))
            class_buffer = ctypes.create_unicode_buffer(256)
            cls.user32.GetClassNameW(hwnd, class_buffer, len(class_buffer))
            process_id = wintypes.DWORD()
            cls.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            rect = wintypes.RECT()
            cls.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            rows.append(WindowInfo(
                hwnd=int(hwnd),
                pid=int(process_id.value),
                title=title_buffer.value,
                class_name=class_buffer.value,
                rect=Rect(rect.left, rect.top, rect.right, rect.bottom),
                visible=bool(cls.user32.IsWindowVisible(hwnd)),
                minimized=bool(cls.user32.IsIconic(hwnd)),
            ))
            return True

        cls.user32.EnumWindows(visit, 0)
        return rows

    @classmethod
    def work_area(cls, hwnd: int | None = None) -> Rect:
        class MonitorInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        if hwnd:
            monitor = cls.user32.MonitorFromWindow(hwnd, cls.MONITOR_DEFAULTTONEAREST)
            info = MonitorInfo()
            info.cbSize = ctypes.sizeof(info)
            if cls.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                rect = info.rcWork
                return Rect(rect.left, rect.top, rect.right, rect.bottom)
        rect = wintypes.RECT()
        if cls.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            return Rect(rect.left, rect.top, rect.right, rect.bottom)
        return Rect(0, 0, 1920, 1080)

    @classmethod
    def foreground_pid(cls) -> int:
        hwnd = cls.user32.GetForegroundWindow()
        process_id = wintypes.DWORD()
        cls.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        return int(process_id.value)

    @classmethod
    def acquire_mutex(cls) -> int | None:
        handle = cls.kernel32.CreateMutexW(None, False, "Local\\PddAdsorbWindow-v1")
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == cls.ERROR_ALREADY_EXISTS:
            cls.kernel32.CloseHandle(handle)
            return None
        return int(handle)


def load_config() -> dict[str, Any]:
    config: dict[str, Any] = {
        "dock_width": 320,
        "poll_interval_seconds": 0.6,
        "auto_start_bridge": True,
        "hide_when_inactive": False,
        "target_process_names": sorted(DEFAULT_TARGET_NAMES),
        "target_title_keywords": ["拼多多", "接待中心", "商家工作台"],
    }
    dock_path = ROOT / "pdd_adsorb_config.json"
    if dock_path.is_file():
        config.update(json.loads(dock_path.read_text(encoding="utf-8-sig")))
    bridge_path = ROOT / "bridge_config.json"
    if bridge_path.is_file():
        bridge = json.loads(bridge_path.read_text(encoding="utf-8-sig"))
        if bridge.get("local_workbench_url"):
            config["local_workbench_url"] = bridge["local_workbench_url"]
    return config


HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def loopback_base(value: str) -> str | None:
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in {"http", "https"}:
            return None
        if (parsed.hostname or "").lower() not in {"127.0.0.1", "localhost", "::1"}:
            return None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return f"{parsed.scheme}://127.0.0.1:{port}"
    except (TypeError, ValueError):
        return None


def candidate_workbench_bases(config: dict[str, Any]) -> list[str]:
    values: list[str] = []
    configured = loopback_base(str(config.get("local_workbench_url") or ""))
    if configured:
        values.append(configured)
    for port in DEFAULT_PORTS:
        value = f"http://127.0.0.1:{port}"
        if value not in values:
            values.append(value)
    return values


def probe_workbench(base: str, timeout: float = 0.5) -> bool:
    parsed = urllib.parse.urlsplit(base)
    try:
        with socket.create_connection((parsed.hostname or "127.0.0.1", parsed.port or 80), timeout=timeout):
            pass
        request = urllib.request.Request(base + "/api/runtime-config", method="GET")
        with HTTP.open(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        return bool(
            payload.get("ok")
            and str(payload.get("platform") or "").lower() == "pdd"
            and (payload.get("ui_role") == "seat" or payload.get("seat_mode"))
        )
    except (OSError, ValueError, urllib.error.URLError):
        return False


def discover_workbench(config: dict[str, Any]) -> str | None:
    for base in candidate_workbench_bases(config):
        if probe_workbench(base):
            return base
    return None


def start_bridge_if_needed(config: dict[str, Any]) -> bool:
    if not config.get("auto_start_bridge", True):
        return False
    executable = ROOT / "PddBridgeAgent.exe"
    if not executable.is_file():
        return False
    for process in psutil.process_iter(["name", "exe"]):
        try:
            if str(process.info.get("name") or "").lower() != "pddbridgeagent.exe":
                continue
            if Path(str(process.info.get("exe") or "")).resolve().parent == ROOT:
                return False
        except (OSError, psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    startupinfo = None
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 6  # SW_MINIMIZE
    subprocess.Popen(
        [str(executable)],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        startupinfo=startupinfo,
    )
    log("started existing PddBridgeAgent.exe")
    return True


def find_edge() -> Path:
    candidates = [
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("未找到 Microsoft Edge，请先安装或修复 Edge。")


def profile_edge_processes(profile: Path) -> list[psutil.Process]:
    expected = str(profile.resolve()).lower()
    result: list[psutil.Process] = []
    for process in psutil.process_iter(["name", "cmdline"]):
        try:
            if str(process.info.get("name") or "").lower() != "msedge.exe":
                continue
            command = " ".join(process.info.get("cmdline") or []).lower()
            if "--user-data-dir" in command and expected in command:
                result.append(process)
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    return result


def stop_profile_edge(profile: Path, timeout: float = 2.5) -> None:
    processes = profile_edge_processes(profile)
    for process in reversed(processes):
        try:
            process.terminate()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
    _gone, alive = psutil.wait_procs(processes, timeout=timeout)
    for process in alive:
        try:
            process.kill()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
    if alive:
        psutil.wait_procs(alive, timeout=timeout)


def stop_all_adsorb_edge(timeout: float = 2.5) -> None:
    """停掉本产品所有浮窗 Edge，包括其他安装根目录留下的残留窗口。

    每个安装根目录有自己的 profile 目录，只停自己的 PROFILE_DIR 会漏掉
    升级/重装前旧实例拉起的窗口，于是同一个网关出现两个一模一样的浮窗。"""
    profiles: set[Path] = {PROFILE_DIR}
    for process in psutil.process_iter(["name", "cmdline"]):
        try:
            if str(process.info.get("name") or "").lower() != "msedge.exe":
                continue
            for arg in process.info.get("cmdline") or []:
                if "pdd-adsorb-edge-profile" in str(arg).lower():
                    value = str(arg).split("=", 1)[-1].strip().strip('"')
                    if value:
                        profiles.add(Path(value))
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    for profile in profiles:
        stop_profile_edge(profile, timeout)


def trim_profile_cache(profile: Path) -> None:
    for relative in (
        "Default/Cache",
        "Default/Code Cache",
        "Default/GPUCache",
        "Default/Service Worker/CacheStorage",
    ):
        target = (profile / relative).resolve()
        try:
            target.relative_to(profile.resolve())
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
        except (OSError, ValueError):
            continue


def write_pid() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = PID_PATH.with_suffix(".tmp")
    temporary.write_text(str(os.getpid()), encoding="ascii")
    os.replace(temporary, PID_PATH)


def read_pid() -> int | None:
    try:
        return int(PID_PATH.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def running_controller_pid() -> int | None:
    pid = read_pid()
    if not pid:
        return None
    try:
        process = psutil.Process(pid)
        name = process.name().lower()
        command = " ".join(process.cmdline()).lower()
        if name == "pddadsorbwindow.exe" or "pdd_adsorb_window.py" in command:
            return pid
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        pass
    return None


def stop_existing() -> int:
    stopped = 0
    pid = running_controller_pid()
    if pid and pid != os.getpid():
        try:
            process = psutil.Process(pid)
            process.terminate()
            try:
                process.wait(timeout=3.0)
            except psutil.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)
            stopped += 1
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            pass
    stop_all_adsorb_edge()
    try:
        PID_PATH.unlink()
    except FileNotFoundError:
        pass
    log(f"stop requested; controllers={stopped}")
    return stopped


class DockedWindow:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.stop_event = threading.Event()
        self.edge: subprocess.Popen[bytes] | None = None
        self.dock_hwnd = 0
        self._dock_visible = False
        self._dock_rect: Rect | None = None
        self._dock_pin_on: bool | None = None
        self._process_cache: dict[int, ProcessInfo] = {}
        self._process_cache_at = 0.0

    def process_snapshot(self) -> dict[int, ProcessInfo]:
        now = time.monotonic()
        if now - self._process_cache_at < 2.0:
            return dict(self._process_cache)
        snapshot: dict[int, ProcessInfo] = {}
        for process in psutil.process_iter(["pid", "name", "exe"]):
            try:
                snapshot[int(process.info["pid"])] = ProcessInfo(
                    str(process.info.get("name") or ""),
                    str(process.info.get("exe") or ""),
                )
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
        self._process_cache = snapshot
        self._process_cache_at = now
        return dict(snapshot)

    def target(self, windows: list[WindowInfo]) -> WindowInfo | None:
        names = {
            str(item).lower()
            for item in self.config.get("target_process_names", DEFAULT_TARGET_NAMES)
            if str(item).strip()
        }
        keywords = tuple(
            str(item)
            for item in self.config.get("target_title_keywords", ["拼多多", "接待中心", "商家工作台"])
            if str(item).strip()
        )
        return select_target_window(windows, self.process_snapshot(), names, keywords)

    def edge_pids(self) -> set[int]:
        # Edge may hand the --app window to another process using the same
        # user-data-dir. That process is not always a child of Popen's PID.
        result = {process.pid for process in profile_edge_processes(PROFILE_DIR)}
        if self.edge is None:
            return result
        result.add(int(self.edge.pid))
        try:
            result.update(child.pid for child in psutil.Process(self.edge.pid).children(recursive=True))
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
        return result

    def launch(self, base: str, initial: Rect) -> None:
        edge = find_edge()
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        stop_all_adsorb_edge()
        trim_profile_cache(PROFILE_DIR)
        # Use the complete workbench's dock layout so the standalone window has
        # the same session filters, search, badges and jump behavior as the
        # embedded workbench.
        url = base + "/?dock=1&standalone=1"
        self.edge = subprocess.Popen(
            [
                str(edge),
                f"--app={url}",
                f"--user-data-dir={PROFILE_DIR}",
                "--no-first-run",
                "--disable-default-apps",
                "--disable-sync",
                "--disable-background-mode",
                "--disable-background-networking",
                "--disable-component-update",
                "--disk-cache-size=52428800",
                "--media-cache-size=10485760",
                f"--window-position={initial.left},{initial.top}",
                f"--window-size={initial.width},{initial.height}",
            ],
            cwd=str(ROOT),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        log(f"launched Edge app {url}")

    def find_dock_window(self, windows: list[WindowInfo]) -> WindowInfo | None:
        return select_dock_window(windows, self.edge_pids())

    def hide_black_host_windows(self, windows: list[WindowInfo]) -> None:
        if not self.dock_hwnd:
            return
        edge_pids = self.edge_pids()
        for item in windows:
            if (
                item.hwnd != self.dock_hwnd
                and item.pid in edge_pids
                and item.class_name == "Chrome_WidgetWin_0"
                and not item.title.strip()
                and item.visible
                and item.rect.width >= 200
                and item.rect.height >= 250
            ):
                Win32.user32.ShowWindow(item.hwnd, Win32.SW_HIDE)

    def set_visible(self, visible: bool) -> None:
        if not self.dock_hwnd:
            return
        actual_visible = bool(Win32.user32.IsWindowVisible(self.dock_hwnd))
        iconic = bool(Win32.user32.IsIconic(self.dock_hwnd))
        if visible and iconic and not wake_pending():
            # Keep an operator-minimized dock in the taskbar. The titled window
            # remains tracked, so the native taskbar button can restore it.
            # An explicit wake request restores it instead.
            return
        if wake_pending() and iconic:
            Win32.user32.ShowWindow(self.dock_hwnd, Win32.SW_RESTORE)
            self._dock_visible = True
            return
        if actual_visible != visible:
            Win32.user32.ShowWindow(
                self.dock_hwnd,
                Win32.SW_SHOWNOACTIVATE if visible else Win32.SW_HIDE,
            )
        self._dock_visible = visible

    def apply_window_state(self, dock: WindowInfo, desired: Rect, control: dict[str, bool]) -> None:
        pin_on = bool(control.get("pin", True))
        adsorb_on = bool(control.get("adsorb", True))
        should_move = adsorb_on and (dock.rect != desired or desired != self._dock_rect)
        pin_changed = pin_on != self._dock_pin_on
        if not should_move and not pin_changed:
            return
        rect = desired if adsorb_on else dock.rect
        if not Win32.user32.SetWindowPos(
            self.dock_hwnd,
            Win32.HWND_TOPMOST if pin_on else Win32.HWND_NOTOPMOST,
            rect.left,
            rect.top,
            rect.width,
            rect.height,
            Win32.SWP_NOACTIVATE,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        self._dock_rect = desired if adsorb_on else None
        self._dock_pin_on = pin_on

    def close(self, *, preserve_live_dock: bool = False) -> None:
        if preserve_live_dock and self.find_dock_window(Win32.windows()):
            log("controller failed but live dock was preserved")
            self.edge = None
            return
        if self.dock_hwnd:
            Win32.user32.PostMessageW(self.dock_hwnd, Win32.WM_CLOSE, 0, 0)
        stop_profile_edge(PROFILE_DIR)
        trim_profile_cache(PROFILE_DIR)
        self.edge = None

    def run(self) -> int:
        width = max(240, min(480, int(self.config.get("dock_width", 320))))
        poll_delay = max(0.3, min(2.0, float(self.config.get("poll_interval_seconds", 0.6))))
        base = discover_workbench(self.config)
        if not base:
            start_bridge_if_needed(self.config)
            deadline = time.monotonic() + 45.0
            while not self.stop_event.is_set() and time.monotonic() < deadline:
                base = discover_workbench(self.config)
                if base:
                    break
                self.stop_event.wait(0.5)
        if not base:
            raise RuntimeError("本机拼多多 127 工作台未就绪。请先启动 PddBridgeAgent.exe 并确认工作台可访问。")

        windows = Win32.windows()
        target = self.target(windows)
        work_area = Win32.work_area(target.hwnd if target else None)
        initial = choose_dock_rect(target.rect, work_area, width) if target else choose_fallback_rect(work_area, width)
        self.launch(base, initial)

        dock_seen = False
        dock_missing_since = 0.0
        dock_deadline = time.monotonic() + 45.0
        target_seen = target is not None
        while not self.stop_event.is_set():
            windows = Win32.windows()
            target = self.target(windows)
            dock = self.find_dock_window(windows)
            if dock:
                if dock.hwnd != self.dock_hwnd:
                    self._dock_rect = None
                    self._dock_visible = dock.visible
                self.dock_hwnd = dock.hwnd
                dock_seen = True
                dock_missing_since = 0.0
                self.hide_black_host_windows(windows)
            else:
                self.dock_hwnd = 0
                self._dock_visible = False
                self._dock_rect = None
                if dock_seen:
                    dock_missing_since = dock_missing_since or time.monotonic()
                    if time.monotonic() - dock_missing_since >= 1.0:
                        return 0
                elif time.monotonic() >= dock_deadline:
                    raise RuntimeError("独立吸附窗口启动失败，未找到 Edge 应用窗口。")
                self.stop_event.wait(poll_delay)
                continue

            if target:
                target_seen = True
            if target:
                desired = choose_dock_rect(target.rect, Win32.work_area(target.hwnd), width)
            else:
                desired = choose_fallback_rect(Win32.work_area(), width)
            # Keep the dock aligned while adsorption is enabled. Pin state is
            # independent and can switch between TOPMOST and NOTOPMOST.
            self.apply_window_state(dock, desired, load_dock_control())

            visible = not (target_seen and (target is None or target.minimized))
            if wake_pending():
                visible = True
            elif visible and target and self.config.get("hide_when_inactive", True):
                foreground = Win32.foreground_pid()
                visible = foreground in self.edge_pids() | {target.pid}
            self.set_visible(visible)
            self.stop_event.wait(poll_delay)
        return 0


def diagnose(config: dict[str, Any]) -> int:
    rows = Win32.windows()
    processes: dict[int, ProcessInfo] = {}
    for process in psutil.process_iter(["pid", "name", "exe"]):
        try:
            processes[int(process.info["pid"])] = ProcessInfo(
                str(process.info.get("name") or ""), str(process.info.get("exe") or "")
            )
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    target = select_target_window(rows, processes)
    result = {
        "workbench": discover_workbench(config),
        "target": None if target is None else {
            "pid": target.pid,
            "title": target.title,
            "class": target.class_name,
            "rect": target.rect.__dict__,
            "process": processes.get(target.pid).__dict__ if target.pid in processes else {},
        },
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    diagnostic_path = ROOT / "logs" / "pdd_adsorb_diagnostic.json"
    diagnostic_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = diagnostic_path.with_suffix(".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    os.replace(temporary, diagnostic_path)
    if sys.stdout is not None:
        print(rendered)
    return 0 if result["workbench"] else 1


def wake_dock() -> int:
    """Bring the floating dock back: restore/show it now and hold it visible
    for a short grace period so the controller poll does not re-hide it."""
    request_wake()
    windows = Win32.windows()
    edge_pids = {p.pid for p in profile_edge_processes(PROFILE_DIR)}
    dock = select_dock_window(windows, edge_pids) if edge_pids else None
    if dock is not None:
        if Win32.user32.IsIconic(dock.hwnd):
            Win32.user32.ShowWindow(dock.hwnd, Win32.SW_RESTORE)
        else:
            Win32.user32.ShowWindow(dock.hwnd, Win32.SW_SHOW)
        log("dock woke by request")
        return 0
    if running_controller_pid() is None:
        # No controller alive: start one normally, it launches the dock itself.
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen([str(ROOT / "PddAdsorbWindow.exe")], cwd=str(ROOT), creationflags=creationflags)
        log("no dock found; controller restarted for wake")
    else:
        log("wake requested; controller is still starting the Edge window")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="拼多多独立吸附接待窗口")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--diagnose", action="store_true")
    parser.add_argument("--wake", action="store_true", help="唤醒浮窗并保持可见数秒")
    args = parser.parse_args()
    if args.stop:
        stop_existing()
        return 0
    config = load_config()
    if args.wake:
        return wake_dock()
    if args.diagnose:
        return diagnose(config)

    mutex = Win32.acquire_mutex()
    if mutex is None:
        return 0
    write_pid()
    app = DockedWindow(config)
    signal.signal(signal.SIGINT, lambda *_: app.stop_event.set())
    signal.signal(signal.SIGTERM, lambda *_: app.stop_event.set())
    failed = False
    try:
        return app.run()
    except Exception as exc:
        failed = True
        if app.find_dock_window(Win32.windows()):
            log(f"startup verification failed but dock is live; suppressed error: {exc}")
            return 0
        notify_error(str(exc))
        return 1
    finally:
        app.close(preserve_live_dock=failed)
        try:
            PID_PATH.unlink()
        except FileNotFoundError:
            pass
        try:
            WAKE_PATH.unlink()
        except FileNotFoundError:
            pass
        Win32.kernel32.CloseHandle(mutex)


if __name__ == "__main__":
    raise SystemExit(main())
