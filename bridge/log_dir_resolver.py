# -*- coding: utf-8 -*-
"""找出拼多多插件「现在真正在写」的日志目录（1.0.6）。

现场事故：探域修插件时把运行中工作台的 PDDData.ini 里 LogDir 改到了
D:/kefuAgent/tanyu3.2.2/logs，而桥接配置里写死的是
C:\\Users\\Public\\PddPortableRuntime\\3.5.0.40\\plugins\\logs —— 那个目录从此没人写，
inside/logrus/plugin 三路实时源同时归零，只剩两分钟一轮的 CDP 补拉。

解析顺序：
  1. 正在运行的 PddWorkbench.exe 同目录 PDDData.ini 的 LogDir（权威）；
  2. 配置目录还在写 → 不动；
  3. 配置目录超过 idle_seconds 没写入，而某个候选目录 recent_seconds 内写过 → 换过去。
"""
from __future__ import annotations

import configparser
import glob
import logging
import os
import time
from typing import Iterable, List, Optional, Tuple

log = logging.getLogger("pdd.bridge")

LOG_PATTERNS = ("inside_*.log", "*PddMsgPlugin*.log", "logrus.log*", "Injector_cnpdd*.log")
WORKBENCH_NAMES = {"pddworkbench.exe"}


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(str(path or "").strip())) if path else ""


def _read_ini_logdir(ini_path: str) -> str:
    raw = None
    for enc in ("utf-8-sig", "gbk", "utf-16"):
        try:
            with open(ini_path, "r", encoding=enc) as fh:
                raw = fh.read()
            break
        except (OSError, UnicodeError):
            continue
    if not raw:
        return ""
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        parser.read_string(raw)
    except configparser.Error:
        return ""
    for section in parser.sections():
        value = parser.get(section, "LogDir", fallback="")
        if value:
            return os.path.normpath(value.strip().strip('"'))
    return ""


def workbench_exe_dirs() -> List[str]:
    try:
        import psutil  # type: ignore
    except Exception:
        return []
    dirs: List[str] = []
    try:
        for proc in psutil.process_iter(["name", "exe"]):
            name = str(proc.info.get("name") or "").lower()
            if name not in WORKBENCH_NAMES:
                continue
            exe = proc.info.get("exe") or ""
            if exe:
                d = os.path.dirname(exe)
                if d and d not in dirs:
                    dirs.append(d)
    except Exception:
        pass
    return dirs


def ini_log_dirs(exe_dirs: Optional[Iterable[str]] = None) -> List[str]:
    out: List[str] = []
    for d in (workbench_exe_dirs() if exe_dirs is None else exe_dirs):
        value = _read_ini_logdir(os.path.join(d, "PDDData.ini"))
        if value and os.path.isdir(value) and value not in out:
            out.append(value)
    return out


def candidate_log_dirs(extra: Iterable[str] = ()) -> List[str]:
    public = os.environ.get("PUBLIC") or r"C:\Users\Public"
    patterns = [
        os.path.join(public, "PddPortableRuntime", "*", "plugins", "logs"),
        r"D:\kefuAgent\*\logs",
        r"D:\tanyuAgent\data\platformSoftware\cnpdd\*\logs",
    ]
    out: List[str] = []
    for d in extra:
        if d and os.path.isdir(d) and d not in out:
            out.append(d)
    for pattern in patterns:
        try:
            for d in glob.glob(pattern):
                if os.path.isdir(d) and d not in out:
                    out.append(d)
        except OSError:
            continue
    return out


def newest_write(log_dir: str) -> Tuple[float, List[str]]:
    """目录里插件日志文件的最新修改时间 + 文件名列表。"""
    newest = 0.0
    names: List[str] = []
    if not log_dir or not os.path.isdir(log_dir):
        return 0.0, names
    for pattern in LOG_PATTERNS:
        try:
            for path in glob.glob(os.path.join(log_dir, pattern)):
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                names.append(os.path.basename(path))
                newest = max(newest, mtime)
        except OSError:
            continue
    return newest, names


def ini_derived_dirs(ini_dirs: Iterable[str]) -> List[str]:
    """PDDData.ini 的 LogDir 不一定就是插件写日志的目录。

    现场（90 科技便携包）：LogDir=.../3.5.0.40/logs，插件实际写在 .../3.5.0.40/plugins/logs。
    所以把 LogDir 本身、其下 plugins/logs、同级 plugins/logs 都列为候选。
    """
    out: List[str] = []
    for d in ini_dirs:
        for cand in (d, os.path.join(d, "plugins", "logs"),
                     os.path.join(os.path.dirname(os.path.normpath(d)), "plugins", "logs")):
            if cand and os.path.isdir(cand) and not any(same_dir(cand, x) for x in out):
                out.append(cand)
    return out


def resolve_active_log_dir(current: str, *, idle_seconds: float = 120.0,
                           recent_seconds: float = 600.0, now: Optional[float] = None,
                           ini_dirs: Optional[List[str]] = None,
                           candidates: Optional[List[str]] = None) -> Tuple[str, str]:
    """返回 (目录, 来源)。来源：pddini / config / recent_write。

    1.0.10：只会换到「里面真有插件日志」的目录。1.0.6–1.0.9 无条件信 LogDir，
    现场被换到一个空目录，inside/plugin 通道整个停掉。
    """
    now = time.time() if now is None else now
    ini = ini_log_dirs() if ini_dirs is None else [d for d in ini_dirs if os.path.isdir(d)]
    cur_mtime = newest_write(current)[0] if current else 0.0
    derived = ini_derived_dirs(ini)
    scored = [(newest_write(d)[0], d) for d in derived]
    scored = [item for item in scored if item[0] > 0]
    if scored:
        best_mtime, best = max(scored)
        if same_dir(best, current):
            return current, "pddini"
        if best_mtime >= cur_mtime:
            return best, "pddini"
    if current and os.path.isdir(current) and cur_mtime and now - cur_mtime <= idle_seconds:
        return current, "config"
    pool = (candidate_log_dirs() if candidates is None else list(candidates)) + derived
    best_dir, best_mtime = "", 0.0
    for d in pool:
        if same_dir(d, current):
            continue
        mtime = newest_write(d)[0]
        if mtime > best_mtime:
            best_dir, best_mtime = d, mtime
    if best_dir and now - best_mtime <= recent_seconds and best_mtime > cur_mtime:
        return best_dir, "recent_write"
    return current, "config"


def describe(current: str, now: Optional[float] = None) -> dict:
    """诊断用：当前目录、工作台 ini、各候选目录的文件数与最后写入距今秒数。"""
    now = time.time() if now is None else now

    def row(d: str) -> dict:
        mtime, names = newest_write(d)
        return {"dir": d, "files": len(names),
                "last_write_age_s": round(now - mtime, 1) if mtime else None}

    exe_dirs = workbench_exe_dirs()
    ini_rows = []
    for d in exe_dirs[:4]:
        ini_rows.append({"exe_dir": d, "log_dir": _read_ini_logdir(os.path.join(d, "PDDData.ini"))})
    seen: List[str] = []
    rows = []
    for d in [current] + ini_derived_dirs([r["log_dir"] for r in ini_rows if r["log_dir"]]) + candidate_log_dirs():
        if d and not any(same_dir(d, x) for x in seen):
            seen.append(d)
            rows.append(row(d))
    return {"current": row(current) if current else {}, "workbench": ini_rows, "candidates": rows[:10]}


def same_dir(a: str, b: str) -> bool:
    return _norm(a) == _norm(b)
