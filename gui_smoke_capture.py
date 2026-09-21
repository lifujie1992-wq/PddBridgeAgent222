from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

from PIL import ImageGrab


user32 = ctypes.windll.user32


def find_window(pid: int, timeout: float = 20.0) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        found: list[int] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def callback(hwnd: int, _lparam: int) -> bool:
            window_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
            if window_pid.value == pid and user32.IsWindowVisible(hwnd):
                found.append(hwnd)
            return True

        user32.EnumWindows(callback, 0)
        if found:
            return found[0]
        time.sleep(0.2)
    raise RuntimeError(f"window not found for pid {pid}")


def window_box(hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise ctypes.WinError()
    return rect.left, rect.top, rect.right, rect.bottom


def window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value


def capture(hwnd: int, output: Path) -> tuple[int, int, int, int]:
    user32.ShowWindow(hwnd, 9)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.8)
    box = window_box(hwnd)
    ImageGrab.grab(bbox=box, all_screens=True).save(output)
    return box


def click(x: int, y: int) -> None:
    user32.SetCursorPos(x, y)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    user32.mouse_event(0x0004, 0, 0, 0, 0)


def main() -> int:
    exe = Path(sys.argv[1]).resolve()
    output_dir = Path(sys.argv[2]).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([str(exe)], cwd=str(exe.parent))
    try:
        hwnd = find_window(proc.pid)
        left, top, right, bottom = capture(hwnd, output_dir / "gui-main.png")
        # The secondary action row is stable within the fixed 540x640 main window.
        click(left + 180, top + 535)
        deadline = time.time() + 5.0
        windows: list[int] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def callback(child: int, _lparam: int) -> bool:
            window_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(child, ctypes.byref(window_pid))
            if window_pid.value == proc.pid and user32.IsWindowVisible(child):
                windows.append(child)
            return True

        dialog = 0
        while time.time() < deadline and not dialog:
            windows.clear()
            user32.EnumWindows(callback, 0)
            dialog = next((item for item in windows if window_title(item) == "编辑桥接配置"), 0)
            if not dialog:
                time.sleep(0.2)
        if not dialog:
            raise RuntimeError(
                "configuration dialog did not open; windows="
                + repr([window_title(item) for item in windows])
            )
        capture(dialog, output_dir / "gui-config.png")
        print(f"main={right-left}x{bottom-top}; windows={len(windows)}; screenshots={output_dir}")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
