# -*- coding: utf-8 -*-
"""CDP 客户端: 端口发现 / 聊天页面定位 / execution-context 扫描 / 求值

独立 pddbridge 搬入，`discover_ports` 由 PowerShell 实现换成 psutil 版
（对齐 bridge/pdd_context.py 的进程扫描，channel.py 明确不用 PowerShell）。
"""
import json
import logging
import re
import time
import urllib.request

import psutil
import websocket  # websocket-client

log = logging.getLogger("pddbridge.cdp")

CHAT_HINTS = ("middle_panel", "workbench/notification", "tab=conversation", "chat")

# PDD 客户端自带浏览器/壳的进程名 (PddBrowser = pddwebworkbench.exe, 壳 = PddWorkbench.exe)
PDD_PROC = ("pddwebworkbench", "pddworkbench", "pddshell", "pddbrowser")

_DEBUG_PORT_RE = re.compile(r"^--remote-debugging-port(?:=(\d+))?$")

# 求值默认超时。原实现 Cdp.eval 用 8s —— 而 recv 的超时其实是**连接超时**（同样 8s），
# 于是 eval(timeout=4) 实际能阻塞 8 秒，调用方给的预算形同虚设。
DEFAULT_EVAL_TIMEOUT = 4.0


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
        self.timeout = timeout
        self._seq = 0
        self._contexts = {}  # id -> info

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass

    def _recv_until(self, rid, timeout, stop_on_id=True):
        """读到 id==rid 的应答为止（stop_on_id=False 时改为收满整个窗口）。

        - 超时返回 None；连接已断/协议错**直接抛**，由调用方按会话故障处理。
        - 顺手收集 Runtime.executionContextCreated 事件（两个调用方都靠它）。
        - 每次 recv 前按剩余预算重设 socket 超时，调用方给的 timeout 才算数。

        原来的写法是 `except Exception: continue`：连接断掉时 recv 会**立刻**抛，
        于是变成全速空转把 CPU 烧满，直到 deadline 才罢休。10 个店铺同时断线时
        这一条就能把工作台拖卡。
        """
        deadline = time.monotonic() + max(0.0, timeout)
        answer = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return answer
            budget = max(0.02, min(remaining, self.timeout))
            try:
                self.ws.settimeout(budget)
            except Exception:
                pass
            started = time.monotonic()
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                # 不空转的前提是"超时真的等满了 budget"。等不满就说明 socket 超时没设上
                # （settimeout 抛了）或者对端已经关掉而库仍报超时 —— 这种情况下
                # continue 会变成全速自旋，正是这次要修掉的那类 bug。宁可提前交还。
                waited = time.monotonic() - started
                if budget >= remaining or waited < budget * 0.5:
                    return answer
                continue
            except Exception:
                # 连接已断 / 协议错 / 握手失败：重试只会立刻再抛一次，别在这烧 CPU。
                raise
            if not raw:
                raise websocket.WebSocketConnectionClosedException("recv 返回空：流已结束")
            try:
                m = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(m, dict):
                continue
            if m.get("method") == "Runtime.executionContextCreated":
                try:
                    c = m["params"]["context"]
                    self._contexts[c["id"]] = c
                except (KeyError, TypeError):
                    pass
            if m.get("id") == rid:
                answer = m
                if stop_on_id:
                    return m

    def send(self, method, params=None, wait=True, timeout=DEFAULT_EVAL_TIMEOUT):
        self._seq += 1
        rid = self._seq
        self.ws.send(json.dumps({"id": rid, "method": method, "params": params or {}}))
        if not wait:
            return rid
        return self._recv_until(rid, timeout)

    def enable_runtime(self, deadline_seconds=3.0):
        """启用 Runtime 并收集 executionContextCreated 事件。

        **收到 Runtime.enable 的应答就返回**，不等满窗口。Chrome 会先把已有的
        executionContextCreated 全部推出来、最后才回命令结果，所以"收到应答"
        正意味着上下文已经收齐；而收满 3 秒的写法会让重扫阶段每个标签页白等 3 秒
        ——10 个店铺就是 30 秒把主循环堵死（这个回归是 v0.9.1 开发中引入又抓回来的，
        见 tests 里 test_enable_runtime_returns_on_reply_not_full_window）。
        deadline_seconds 只是**坏页面**的上限：一直不应答也不会超过它。
        """
        rid = self.send("Runtime.enable", wait=False)
        self._recv_until(rid, deadline_seconds)
        return self._contexts

    def contexts(self):
        return self._contexts

    def eval(self, expression, context_id=None, return_by_value=True,
             timeout=DEFAULT_EVAL_TIMEOUT):
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


def find_socketutil_contexts(port, eval_timeout=DEFAULT_EVAL_TIMEOUT):
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
            try:
                v = cdp.eval("!!window.socketUtil", cid, timeout=eval_timeout)
            except Exception:
                continue
            if v is True:
                hits.append((cdp, cid))
                break
        else:
            cdp.close()
    return hits


def find_socketutil_sessions(port, eval_timeout=DEFAULT_EVAL_TIMEOUT, collect_seconds=3.0):
    """同 find_socketutil_contexts, 但带上 target url —— 多店铺必须能区分是哪个页面。

    返回 [{"cdp": Cdp, "cid": int, "url": str, "title": str}, ...]（未命中的连接已关闭）。

    这个函数跑在主循环线程里，而且是**串行**扫每个 target 的每个 context。默认
    8s/次求值意味着一个卡住的标签页能吃掉几十秒，期间其余 9 个店铺的帧全都堵着。
    所以两次阻塞（enable_runtime 收 context、逐 context 求值）都由调用方给预算。
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
        except Exception as exc:
            log.debug("CDP 连接失败 (%s): %s", (t.get("url") or "")[:60], exc)
            continue
        try:
            cdp.enable_runtime(deadline_seconds=collect_seconds)
        except (TypeError, AttributeError):
            # 签名/参数名不对是**编程错误**，不是"这个 target 用不了"。
            # 原来这里是个笼统的 `except Exception: continue`，把 TypeError 吞成了
            # 一个空的 hits 列表 —— 最后表现成"CDP 未就绪、自动降级"，日志上一个字
            # 都没有（实测 v0.9.2 就这么挂的：改了参数名没改这个调用点）。
            raise
        except Exception as exc:
            log.debug("enable_runtime 失败 (%s): %s", (t.get("url") or "")[:60], exc)
            cdp.close()
            continue
        found = None
        for cid in list(cdp.contexts().keys()):
            try:
                if cdp.eval("!!window.socketUtil", cid, timeout=eval_timeout) is True:
                    found = cid
                    break
            except (TypeError, AttributeError):
                raise
            except Exception:
                continue
        if found is None:
            cdp.close()
            continue
        hits.append({"cdp": cdp, "cid": found, "url": t.get("url") or "",
                     "id": t.get("id") or "", "title": t.get("title") or ""})
    return hits
