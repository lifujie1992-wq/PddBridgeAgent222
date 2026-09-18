# -*- coding: utf-8 -*-
"""CDP 客户端: 端口发现 / 聊天页面定位 / execution-context 扫描 / 求值

独立 pddbridge 搬入，`discover_ports` 由 PowerShell 实现换成 psutil 版
（对齐 bridge/pdd_context.py 的进程扫描，channel.py 明确不用 PowerShell）。
"""
import json
import re
import time
import urllib.request

import psutil
import websocket  # websocket-client

CHAT_HINTS = ("middle_panel", "workbench/notification", "tab=conversation", "chat")

# PDD 客户端自带浏览器/壳的进程名 (PddBrowser = pddwebworkbench.exe, 壳 = PddWorkbench.exe)
PDD_PROC = ("pddwebworkbench", "pddworkbench", "pddshell", "pddbrowser")

_DEBUG_PORT_RE = re.compile(r"^--remote-debugging-port(?:=(\d+))?$")


def _is_pdd_proc(name):
    n = (name or "").lower()
    return any(k in n for k in PDD_PROC)


def discover_ports(only_pdd=False, extra_ports=()):
    """扫描所有进程命令行里的 --remote-debugging-port, 返回 {port: [(pid, name), ...]}

    only_pdd=True 时只保留 PDD 客户端进程 (PddBrowser/PddWorkbench), 避免误接管 Chrome/Edge。
    psutil 版: 进程名过滤 + cmdline 正则 (兼容 `--remote-debugging-port=57165` 与
    `--remote-debugging-port 57165` 两种写法)。
    """
    ports = {}
    pdd_pids = set()
    for pid in extra_ports:
        ports[int(pid)] = [(0, "extra")]
    try:
        try:
            processes = psutil.process_iter(["name", "cmdline"])
        except TypeError:
            processes = psutil.process_iter()
        for process in processes:
            try:
                info = getattr(process, "info", {}) or {}
                name = str(info.get("name") or process.name() or "").lower()
                if only_pdd and not _is_pdd_proc(name):
                    continue
                if only_pdd:
                    pdd_pids.add(process.pid)
                cmdline = info.get("cmdline") or process.cmdline() or []
                for index, argument in enumerate(cmdline):
                    text = str(argument or "")
                    match = _DEBUG_PORT_RE.match(text)
                    raw_port = match.group(1) if match else ""
                    if match and not raw_port and index + 1 < len(cmdline):
                        raw_port = str(cmdline[index + 1] or "")
                    if raw_port.isdigit() and 1024 <= int(raw_port) <= 65535:
                        ports.setdefault(int(raw_port), []).append((process.pid, name))
            except (psutil.Error, OSError, ValueError, AttributeError):
                continue

        # Windows may allow the process name but deny its command line. In that
        # case the CDP listener is still visible through the owning PID.
        if only_pdd and pdd_pids and not ports:
            try:
                for conn in psutil.net_connections(kind="tcp"):
                    if (conn.pid in pdd_pids and conn.status == psutil.CONN_LISTEN
                            and conn.laddr and 1024 <= conn.laddr.port <= 65535):
                        ports.setdefault(conn.laddr.port, []).append((conn.pid, "pdd-listener"))
            except (psutil.Error, OSError, ValueError):
                pass
    except Exception:
        pass
    return ports


def _http_json(port, path, timeout=5):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=timeout) as r:
        return json.loads(r.read().decode())


def list_targets(port):
    try:
        return _http_json(port, "/json/list")
    except Exception:
        return []


def is_alive(port):
    # 必须探测真实响应：/json/list 对「连接被拒」也返回 []，len([])>=0 会把死端口误判成活。
    # /json/version 在活的 CDP 上必回 JSON dict, 拒绝/超时则抛异常 → False。
    try:
        return isinstance(_http_json(port, "/json/version"), dict)
    except Exception:
        return False


def find_chat_targets(port):
    """返回 URL 命中聊天线索的 target 列表 (OOPIF 场景下 middle_panel 是独立 target)"""
    out = []
    for t in list_targets(port):
        u = t.get("url") or ""
        title = t.get("title") or ""
        if any(h in u for h in CHAT_HINTS) or any(h in title for h in CHAT_HINTS):
            out.append(t)
    return out


class Cdp:
    """对一个 CDP target 的连接, 提供执行上下文扫描与求值。"""

    def __init__(self, ws_url, timeout=8):
        self.ws = websocket.create_connection(ws_url, timeout=timeout)
        self._seq = 0
        self._contexts = {}  # id -> info

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass

    def send(self, method, params=None, wait=True, timeout=8):
        self._seq += 1
        rid = self._seq
        self.ws.send(json.dumps({"id": rid, "method": method, "params": params or {}}))
        if not wait:
            return rid
        end = time.time() + timeout
        while time.time() < end:
            try:
                m = json.loads(self.ws.recv())
            except Exception:
                continue
            if m.get("id") == rid:
                return m
            if m.get("method") == "Runtime.executionContextCreated":
                c = m["params"]["context"]
                self._contexts[c["id"]] = c
        return None

    def enable_runtime(self):
        rid = self.send("Runtime.enable", wait=False)
        # 收一段时间 context 事件
        end = time.time() + 3
        while time.time() < end:
            try:
                m = json.loads(self.ws.recv())
            except Exception:
                continue
            if m.get("method") == "Runtime.executionContextCreated":
                c = m["params"]["context"]
                self._contexts[c["id"]] = c
            elif m.get("id") == rid:
                break
        return self._contexts

    def contexts(self):
        return self._contexts

    def eval(self, expression, context_id=None, return_by_value=True, timeout=8):
        params = {"expression": expression, "returnByValue": return_by_value}
        if context_id:
            params["contextId"] = context_id
        r = self.send("Runtime.evaluate", params, timeout=timeout)
        if r is None:
            return None
        res = r.get("result", {})
        if "exceptionDetails" in res:
            return {"__exception__": str(res["exceptionDetails"].get("text"))}
        return res.get("result", {}).get("value")


def find_socketutil_contexts(port):
    """对每个聊天 target 连接, 启用 Runtime, 扫描所有 context, 返回 [(cdp, ctx_id), ...] 含 socketUtil 的"""
    hits = []
    targets = find_chat_targets(port)
    if not targets:
        targets = list_targets(port)  # 兜底: 全扫
    for t in targets:
        ws_url = t.get("webSocketDebuggerUrl")
        if not ws_url:
            continue
        try:
            cdp = Cdp(ws_url)
        except Exception:
            continue
        cdp.enable_runtime()
        for cid in list(cdp.contexts().keys()):
            v = cdp.eval("!!window.socketUtil", cid)
            if v is True:
                hits.append((cdp, cid))
                break
        else:
            cdp.close()
    return hits


def find_socketutil_sessions(port):
    """同 find_socketutil_contexts, 但带上 target url —— 多店铺必须能区分是哪个页面。

    返回 [{"cdp": Cdp, "cid": int, "url": str, "title": str}, ...]（未命中的连接已关闭）。
    """
    hits = []
    targets = find_chat_targets(port)
    if not targets:
        targets = list_targets(port)
    for t in targets:
        ws_url = t.get("webSocketDebuggerUrl")
        if not ws_url:
            continue
        try:
            cdp = Cdp(ws_url)
        except Exception:
            continue
        try:
            cdp.enable_runtime()
        except Exception:
            cdp.close()
            continue
        found = None
        for cid in list(cdp.contexts().keys()):
            try:
                if cdp.eval("!!window.socketUtil", cid) is True:
                    found = cid
                    break
            except Exception:
                continue
        if found is None:
            cdp.close()
            continue
        hits.append({"cdp": cdp, "cid": found, "url": t.get("url") or "",
                     "title": t.get("title") or ""})
    return hits
