"""这台机器上，哪种浏览器窗口会让桌面图标反复重画？

现场排查工具：在客户机上跑一遍，看「图标重画」那一列。
基线应该接近 0；几十次/15秒 就说明这台机器的该浏览器会让桌面闪烁。

用法：
    python browser_flicker_check.py            # 检测已安装的浏览器
    python browser_flicker_check.py --seconds 15
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CROP = (0, 0, 170, 940)  # 桌面最左侧图标列；若你的图标不在这里，改成图标所在区域
WARMUP = 10.0


def installed_browsers() -> list[tuple[str, Path]]:
    roots = [os.environ.get("PROGRAMFILES", ""), os.environ.get("PROGRAMFILES(X86)", ""),
             os.environ.get("LOCALAPPDATA", "")]
    found: list[tuple[str, Path]] = []
    for label, relative in (
        ("chrome", r"Google\Chrome\Application\chrome.exe"),
        ("edge", r"Microsoft\Edge\Application\msedge.exe"),
    ):
        for root in roots:
            if not root:
                continue
            candidate = Path(root) / relative
            if candidate.is_file():
                found.append((label, candidate))
                break
    return found


def run_check(label: str, browser: Path, seconds: float) -> int | None:
    try:
        import numpy as np
        from PIL import ImageGrab
    except ImportError:
        print("需要 pillow 和 numpy：python -m pip install pillow numpy")
        return None

    profile = Path(tempfile.gettempdir()) / f"pdd-flicker-check-{label}"
    profile.mkdir(parents=True, exist_ok=True)
    subprocess.run(["taskkill", "/IM", browser.name, "/F"], capture_output=True)
    time.sleep(2)
    subprocess.Popen(
        [
            str(browser), "--app=about:blank", f"--user-data-dir={profile}",
            "--no-first-run", "--disable-default-apps", "--disable-sync",
            "--disable-background-mode", "--disable-component-update",
            "--window-position=1521,86", "--window-size=320,864",
        ],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        time.sleep(WARMUP)
        prev = np.asarray(ImageGrab.grab(bbox=CROP).convert("L"), dtype=np.int16)
        hits = 0
        t0 = time.time()
        while time.time() - t0 < seconds:
            time.sleep(0.08)
            cur = np.asarray(ImageGrab.grab(bbox=CROP).convert("L"), dtype=np.int16)
            if float((np.abs(cur - prev) > 12).mean()) > 0.003:
                hits += 1
            prev = cur
        return hits
    finally:
        subprocess.run(["taskkill", "/IM", browser.name, "/F"], capture_output=True)
        time.sleep(2)


def main() -> int:
    parser = argparse.ArgumentParser(description="检测浏览器窗口是否导致桌面图标反复重画")
    parser.add_argument("--seconds", type=float, default=15.0, help="每个浏览器观测时长")
    args = parser.parse_args()

    browsers = installed_browsers()
    if not browsers:
        print("没找到 Chrome 或 Edge。")
        return 1

    print(f"每个浏览器开一个 320x864 的 --app 窗口，观测 {args.seconds:.0f} 秒\n")
    results: list[tuple[str, int]] = []
    for label, path in browsers:
        print(f"  {label:8s} {path}")
        hits = run_check(label, path, args.seconds)
        if hits is not None:
            results.append((label, hits))

    print("\n结果（图标重画次数，越低越好；接近 0 = 正常）：")
    for label, hits in results:
        verdict = "正常" if hits <= 2 else "会让桌面闪烁，别用这个"
        print(f"  {label:8s} {hits:4d} 次   {verdict}")
    good = [label for label, hits in results if hits <= 2]
    print()
    if good:
        print(f"建议这台机器用：{', '.join(good)}")
    else:
        print("这台机器上 Chrome 和 Edge 都会闪 —— 需要把 Chromium 打进安装包。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
