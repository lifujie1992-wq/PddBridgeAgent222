# -*- coding: utf-8 -*-
"""拼多多商家端(90科技) 启动器。

作用：把原本那个黑窗口的 .cmd 换成带拼多多客户端图标的 exe。
后台调用同目录的 start_pdd_portable.ps1，不弹控制台；失败时弹消息框说明原因，
而不是一闪而过（原来 .cmd 的 `pause` 在小窗口里很难看清）。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys

TITLE = "拼多多商家端(90科技)"
SCRIPT_NAME = "start_pdd_portable.ps1"
CREATE_NO_WINDOW = 0x08000000


def message(text: str, icon: int = 0x10) -> None:
    ctypes.windll.user32.MessageBoxW(None, text, TITLE, icon)


def base_dir() -> str:
    """exe 所在目录（onefile 模式下 sys.executable 就是真实路径）。"""
    exe = sys.executable if getattr(sys, "frozen", False) else sys.argv[0]
    return os.path.dirname(os.path.abspath(exe))


def find_script() -> str | None:
    here = base_dir()
    candidates = [
        os.path.join(here, SCRIPT_NAME),
        os.path.join(os.path.dirname(here), SCRIPT_NAME),
        os.path.join(here, "dd", SCRIPT_NAME),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def main() -> int:
    script = find_script()
    if not script:
        message("找不到启动脚本 %s。\n\n请确认本程序和 dd 文件夹放在一起（不要单独拷走）。"
                % SCRIPT_NAME)
        return 2
    workdir = os.path.dirname(script)
    cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script]
    try:
        proc = subprocess.run(cmd, cwd=workdir, creationflags=CREATE_NO_WINDOW,
                              capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return 0                      # 客户端已拉起，脚本只是还在等，不算失败
    except OSError as exc:
        message("启动失败：%s" % exc)
        return 3
    if proc.returncode != 0:
        detail = ((proc.stdout or "") + (proc.stderr or "")).strip()
        message("拼多多商家端启动失败（代码 %d）。\n\n%s\n\n请联系技术支持。"
                % (proc.returncode, detail[-1500:] or "（无输出）"))
        return proc.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
