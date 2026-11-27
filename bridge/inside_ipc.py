# -*- coding: utf-8 -*-
"""桥接扮演探域 Injector_pdd 的角色，让便携包工作台里的 PddInsidePlugin 正常工作（1.0.12）。

背景：0.7.6 的「秒跳 / 网关跳转 / 原生发送」都靠 PddInsidePlugin 在工作台里开的 HTTP 端口
（/tanyu/client/pdd/dll/httpTestApi）。这个插件 90 科技便携包里也有（和探域那份同一个文件），
但它只有连上探域的注入器 Injector_pdd.exe 之后才会开端口；便携包没有探域，于是端口永远不开。

插件找注入器的方式（本机实测）：
  1. 每 2 秒读 <插件目录>/temp/InsideStartup_<工作台进程号>.ini，节名就是工作台进程号；
     键：serverPort / platformPid / platformType / identify / platformVersion / targetProcessId ...
     读完就删。
  2. 连 ws://127.0.0.1:<serverPort>/platformType=1&platformPid=..&insidePid=..&account=&identify=workbench
  3. 连上后注入器发 {"cmd":"init",...}（插件加载 PddMsgPlugin）和 {"cmd":"start_deal_msg"}，
     插件随即打开 HTTP 端口，并把所有会话的买家消息以 chat_message 实时推过来。
  4. targetProcessId 指向的进程退出后插件停止重连（所以填桥接自己的进程号）。

探域在跑（或工作台加载的是探域目录里的插件）时不碰，避免抢探域的连接。
"""
from __future__ import annotations

import asyncio
import configparser
import json
import logging
import os
import threading
import time
from typing import Callable, Dict, Optional
from urllib.parse import parse_qs

log = logging.getLogger("pdd.bridge")

TANYU_PROCESS_NAMES = {"injector_pdd.exe", "smartrobot.exe"}


def _read_ini(path: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    for enc in ("utf-8-sig", "gbk", "utf-16"):
        try:
            with open(path, "r", encoding=enc) as fh:
                parser.read_string(fh.read())
            break
        except (OSError, UnicodeError, configparser.Error):
            continue
    return parser


def plugin_dir_for(exe_dir: str) -> str:
    """工作台 PDDData.ini 里 PddInsidePlugin 的所在目录。"""
    parser = _read_ini(os.path.join(exe_dir, "PDDData.ini"))
    for section in parser.sections():
        value = parser.get(section, "PddInsidePlugin", fallback="")
        if value:
            return os.path.dirname(os.path.normpath(value.strip().strip('"')))
    return ""


def handshake_body(pid: int, server_port: int, host_pid: int, platform_version: str) -> str:
    keys = [("serverPort", server_port), ("platformPid", pid), ("platformType", 1),
            ("identify", "workbench"), ("account", ""), ("realAccountName", ""),
            ("platformVersion", platform_version), ("targetProcessId", host_pid),
            ("accountType", 0), ("winTitle", ""), ("receptionWinTitle", ""),
            ("isQnLite", 0), ("isReady", 1)]
    return "[%d]\r\n" % pid + "".join("%s=%s\r\n" % (k, v) for k, v in keys)


def _workbenches():
    try:
        import psutil  # type: ignore
    except Exception:
        return [], False
    rows, tanyu = [], False
    try:
        for proc in psutil.process_iter(["name", "pid", "exe"]):
            name = str(proc.info.get("name") or "").lower()
            if name in TANYU_PROCESS_NAMES:
                tanyu = True
            elif name == "pddworkbench.exe" and proc.info.get("exe"):
                rows.append((int(proc.info["pid"]), os.path.dirname(proc.info["exe"])))
    except Exception:
        pass
    return rows, tanyu


class InsideIpcHost:
    def __init__(self, cfg: dict, stop: threading.Event,
                 on_message: Optional[Callable[[str, dict], None]] = None):
        self.cfg = cfg
        self.stop = stop
        self.on_message = on_message
        self.port = 0
        self.connected: Dict[int, float] = {}
        self.handshakes: Dict[int, float] = {}
        self.counts: Dict[str, int] = {}
        self.last_error = ""
        self.tanyu_running = False
        self.skipped: Dict[int, str] = {}
        self._loop = None
        self._thread = None

    # ---------------- 对外 ----------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="bridge-inside-ipc", daemon=True)
        self._thread.start()

    def status(self) -> dict:
        now = time.time()
        return {
            "port": self.port,
            "connected_pids": sorted(self.connected),
            "handshake_age_s": {str(p): round(now - t, 1) for p, t in self.handshakes.items()},
            "skipped": {str(p): r for p, r in self.skipped.items()},
            "tanyu_running": self.tanyu_running,
            "msg_counts": dict(self.counts),
            "error": self.last_error[:200],
        }

    # ---------------- 内部 ----------------
    def _platform_version(self, exe_dir: str) -> str:
        configured = str(self.cfg.get("platform_version") or "").strip()
        if configured:
            return configured
        base = os.path.basename(os.path.normpath(exe_dir))
        return base.split("_")[0] or "3.5.0.40"

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as exc:
            self.last_error = "inside ipc stopped: %s" % exc
            log.warning("工作台插件连接服务停止: %s", exc)

    async def _main(self) -> None:
        import websockets  # type: ignore

        server = await websockets.serve(self._handler, "127.0.0.1", 0, max_size=None, ping_interval=None)
        self.port = int(list(server.sockets)[0].getsockname()[1])
        log.info("工作台插件连接服务已启动 127.0.0.1:%d（替代探域注入器，供 0.7.6 跳转/原生发送使用）", self.port)
        try:
            while not self.stop.is_set():
                try:
                    self._handshake_round()
                except Exception as exc:
                    self.last_error = "handshake: %s" % exc
                await asyncio.sleep(5.0)
        finally:
            server.close()

    def _handshake_round(self) -> None:
        rows, tanyu = _workbenches()
        self.tanyu_running = tanyu
        alive = {pid for pid, _ in rows}
        for pid in list(self.connected):
            if pid not in alive:
                self.connected.pop(pid, None)
        for pid, exe_dir in rows:
            if pid in self.connected:
                continue
            plugin_dir = plugin_dir_for(exe_dir)
            if not plugin_dir or not os.path.isdir(plugin_dir):
                self.skipped[pid] = "PDDData.ini 里没有 PddInsidePlugin"
                continue
            low = plugin_dir.lower()
            if tanyu or "kefuagent" in low or "tanyu" in low:
                # 探域自己在管这个工作台：别抢它的连接
                self.skipped[pid] = "探域在运行/插件在探域目录"
                continue
            self.skipped.pop(pid, None)
            temp = os.path.join(plugin_dir, "temp")
            path = os.path.join(temp, "InsideStartup_%d.ini" % pid)
            if os.path.exists(path) and time.time() - self.handshakes.get(pid, 0) < 30:
                continue          # 刚写过、插件还没来得及读
            os.makedirs(temp, exist_ok=True)
            with open(path, "w", encoding="ascii", newline="") as fh:
                fh.write(handshake_body(pid, self.port, os.getpid(), self._platform_version(exe_dir)))
            if pid not in self.handshakes:
                log.info("已给工作台 pid=%d 写插件握手文件 %s", pid, path)
            self.handshakes[pid] = time.time()

    async def _handler(self, ws, path: str = "") -> None:
        path = path or getattr(ws, "path", "") or getattr(getattr(ws, "request", None), "path", "") or ""
        query = parse_qs(path.lstrip("/"))
        try:
            pid = int((query.get("platformPid") or ["0"])[0])
        except ValueError:
            pid = 0
        version = "3.5.0.40"
        for p, exe_dir in _workbenches()[0]:
            if p == pid:
                version = self._platform_version(exe_dir)
        self.connected[pid] = time.time()
        log.info("工作台插件已连上 pid=%s，发送 init/start_deal_msg", pid)
        try:
            await ws.send(json.dumps({"cmd": "init", "data": {"logType": 4, "platformVersion": version}}))
            await asyncio.sleep(0.5)
            await ws.send(json.dumps({"cmd": "start_deal_msg", "data": {}}))
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                cmd = str(msg.get("cmd") or "?")[:40]
                self.counts[cmd] = self.counts.get(cmd, 0) + 1
                if self.on_message is not None:
                    try:
                        self.on_message(cmd, msg)
                    except Exception as exc:
                        log.debug("inside ipc on_message failed: %s", exc)
        except Exception as exc:
            log.info("工作台插件连接断开 pid=%s: %s", pid, exc)
        finally:
            self.connected.pop(pid, None)
