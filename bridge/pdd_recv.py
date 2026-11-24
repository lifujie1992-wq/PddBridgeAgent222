"""原生接收通道（v0.7+，对接注入 DLL 的接收管道协议 v4）。

注入 DLL 在工作台进程内通过原生回调收 push 帧，经两条单向命名管道与本模块交互：

    帧流  DLL --> Python : \\\\.\\pipe\\pdd_recv_bridge_v4<suffix>
           每行 "FRAME|<seq>|<json>\\n"（<seq> 是 DLL 侧持久序号，断线重放靠它）
    ACK   Python --> DLL : \\\\.\\pipe\\pdd_recv_ack_v4<suffix>
           每行 "ACK|<seq>\\n"；DLL 收到才把 spool 里该帧标记已确认，否则 2s 后重发

历史教训（别再犯）：
  * 老实现只切 ``FRAME|`` 前缀，把 ``<seq>|{...}`` 直接当 JSON 交给 parse_frame，
    结果所有原生帧都判成 ``unparsable``；
  * 老实现从不回 ACK，DLL 的 spool 永远不确认，重连后整段重放。
两条都必须保住，改这里请先看 tests/test_pdd_native_recv.py。

槽位说明：VIMsgCallBack 虚表哪个槽位收买家消息是版本事实。recv_slot=-1 表示
未确认，只允许探针模式（DLL 侧 PROBE|<log> 全槽位记录）；确认后配置 recv_slot
才会真正注册转发。
"""
from __future__ import annotations

import logging
import os
import re
import threading

log = logging.getLogger("pdd_native_recv")

from .pdd_pipes import (PIPE_VERSION, ack_pipe, legacy, recv_pipe, send_candidates)

#: 接收管道协议版本，与 DLL 源码里的 ``_v4`` 后缀一一对应
RECV_PIPE_VERSION = PIPE_VERSION
RECV_PIPE_PREFIX = "pdd_recv_bridge"
ACK_PIPE_PREFIX = "pdd_recv_ack"

#: 兼容老 DLL（无版本后缀、无 seq、无 ACK）
LEGACY_RECV_PIPE = legacy(RECV_PIPE_PREFIX)

FRAME_PREFIX = "FRAME|"
_SUFFIX_RE = re.compile(r"[^0-9A-Za-z_-]")


def pipe_suffix(cfg: dict | None = None) -> str:
    """管道名后缀。

    DLL 侧读工作台进程的 ``PDD_BRIDGE_PIPE_SUFFIX`` 环境变量，用来让同一台机器上
    多个安装/多个工作台实例互不串管。桥接这边必须用同一个值，否则连不上。
    """
    value = str((cfg or {}).get("recv_pipe_suffix") or os.environ.get("PDD_BRIDGE_PIPE_SUFFIX") or "")
    return _SUFFIX_RE.sub("_", value)


def recv_pipe_path(cfg: dict | None = None) -> str:
    return recv_pipe(cfg)


def ack_pipe_path(cfg: dict | None = None) -> str:
    return ack_pipe(cfg)


def send_pipe_path(cfg: dict | None = None) -> str:
    return send_candidates(cfg)[0]


def parse_recv_line_ex(line: bytes | str) -> tuple[int, str] | None:
    """``FRAME|<seq>|<json>`` -> ``(seq, json)``；老格式 ``FRAME|<json>`` -> ``(0, json)``。

    其余行（心跳/空行/半包残留）返回 None。
    """
    if isinstance(line, bytes):
        line = line.decode("utf-8", "replace")
    line = line.rstrip("\r\n")
    if not line.startswith(FRAME_PREFIX):
        return None
    body = line[len(FRAME_PREFIX):]
    if not body:
        return None
    head, sep, rest = body.partition("|")
    if sep and head.isdigit():
        seq = int(head)
        return (seq, rest) if rest else None
    return (0, body)


def parse_recv_line(line: bytes | str) -> str | None:
    """兼容旧接口：只要 JSON 文本。新代码请用 :func:`parse_recv_line_ex`。"""
    parsed = parse_recv_line_ex(line)
    return parsed[1] if parsed else None


class NativeReceiver:
    """常驻线程：连接接收管道 → 逐帧回调 on_frame(raw_json) → 回 ACK。断线自动重连。"""

    def __init__(self, on_frame, cfg: dict | None = None):
        self.on_frame = on_frame
        self.cfg = cfg or {}
        self.recv_pipe = recv_pipe_path(self.cfg)
        self.ack_pipe = ack_pipe_path(self.cfg)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ack_handle = None
        self.last_error = ""
        self.frame_count = 0
        self.acked = 0
        self.ack_errors = 0
        self.connected_once = False
        self._warned_missing = False

    # ---- 配置 DLL 侧（经发送管道下发命令）----
    def configure(self) -> str:
        """按配置启用转发。recv_slot<0 表示槽位未确认，不启用。"""
        slot = int(self.cfg.get("recv_slot") or -1)
        mode = int(self.cfg.get("recv_mode") or 0)
        if slot < 0:
            return "slot_not_configured"
        self._command(f"RECV_SLOT|{slot}")
        self._command(f"RECV_MODE|{mode}")
        return self._command("RECV_ON")

    def configure_auto(self) -> str:
        """全槽位嗅探模式：不需要知道具体槽位号。

        实测（PDD 3.5.0.40）序列必须是这样：
          1. ``RECV_UNHOOK`` + ``RECV_MSGSdk_ON|0`` —— 否则 ``RECV_ON`` 里
             导出钩子会成功并**直接跳过** vtable 注册（`hrc==0 → 不调 recv_register`）；
          2. ``RECV_SLOT|-1`` + ``RECV_AUTO|1`` —— 32 个槽位都嗅探，
             只放行形似 push 的 JSON（`recv_looks_like_push`）；
          3. ``RECV_ON`` —— 注册假 VIMsgCallBack。
        这条路径不写 probe 日志，所以不会踩 ”g_log_lock 未初始化“ 那个崩溃。
        """
        self._command("RECV_UNHOOK")
        self._command("RECV_MSGSdk_ON|0")
        self._command("RECV_SLOT|-1")
        self._command(f"RECV_MODE|{int(self.cfg.get('recv_mode') or 0)}")
        self._command("RECV_AUTO|1")
        return self._command("RECV_ON")

    def hook_install(self) -> str:
        """安装导出 detour（`NotifyWSMessage` / `CheckWSMsg`）。

        这是不靠虚表槽位的接收路径：按 MsgSdk 导出名定位，跨版本稳定，
        也不需要 PROBE（探针在 3.5.0.40 上会因未初始化临界区崩客户端）。
        DLL 默认拒这条（g_msgsdk_recv_optin），要先显式 opt-in。
        """
        self._command("RECV_MSGSdk_ON|1")
        return self._command("RECV_HOOK")

    def probe(self, log_path: str) -> str:
        """探针模式：注册回调并全槽位记录触发日志，不转发。

        ⚠️ 3.5.0.40 实测：PROBE 会在 `recv_write` 里进未初始化的 CRITICAL_SECTION，
        导致 PddWorkbench 崩在 ntdll+0x1A12D（写 0x24）。仅供副本客户端排障。
        """
        return self._command(f"PROBE|{log_path}")

    def recv_state(self) -> str:
        return self._command("RECV_STATE")
    def _command(self, text: str, timeout: float = 3.0) -> str:
        """向 DLL 下发一条命令。候选管道：带版本号的优先，老名字兜底。"""
        import time as _t
        deadline = _t.time() + timeout
        while _t.time() < deadline:
            missing = True
            for pipe in send_candidates(self.cfg):
                try:
                    with open(pipe, "r+b", buffering=0) as handle:
                        handle.write(text.encode("utf-8"))
                        handle.flush()
                        return handle.read(256).decode("utf-8", "replace").strip()
                except FileNotFoundError:
                    continue
                except OSError:
                    missing = False
                    _t.sleep(0.2)
                    break
            if missing:
                return "pipe_not_found"
        return "pipe_timeout"

    # ---- ACK 通道 ----
    def _ack(self, seq: int) -> None:
        """回 ``ACK|<seq>``。失败不算错：DLL 2s 后会重发该帧（下游按 msg_id 幂等）。"""
        if seq <= 0:
            return
        handle = self._ack_handle
        if handle is None:
            try:
                handle = self._ack_handle = open(self.ack_pipe, "wb", buffering=0)
            except OSError as exc:
                self.ack_errors += 1
                if self.ack_errors == 1:
                    log.warning("原生接收 ACK 管道打不开(%s): %s —— DLL 会重发未确认帧",
                                self.ack_pipe, exc)
                return
        try:
            handle.write(("ACK|%d\n" % seq).encode("ascii"))
            self.acked += 1
        except OSError as exc:
            self.ack_errors += 1
            self.last_error = "ack: %s" % exc
            self._close_ack()

    def _close_ack(self) -> None:
        handle, self._ack_handle = self._ack_handle, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    # ---- 生命周期 ----
    def start(self) -> "NativeReceiver":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="pdd-native-recv")
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._close_ack()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._pump()
            except Exception as exc:  # 管道不存在/断开: 稍后重连
                self.last_error = str(exc)
                if isinstance(exc, FileNotFoundError) and not self._warned_missing:
                    self._warned_missing = True
                    log.warning("原生接收管道不存在: %s（DLL 未注入或未开接收；期望后缀=%r）",
                                self.recv_pipe, pipe_suffix(self.cfg))
            self._stop.wait(2.0)

    def _pump(self) -> None:
        with open(self.recv_pipe, "rb", buffering=0) as handle:
            self.connected_once = True
            self._warned_missing = False
            buf = b""
            while not self._stop.is_set():
                chunk = handle.read(8192)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    parsed = parse_recv_line_ex(line)
                    if not parsed:
                        continue
                    seq, raw = parsed
                    self.frame_count += 1
                    try:
                        self.on_frame(raw)
                    except Exception as exc:  # 不 ACK，让 DLL 重发；不中断管道
                        self.last_error = "on_frame: %r" % exc
                        log.warning("原生帧处理失败(不回 ACK, 等 DLL 重发): %s", exc)
                        continue
                    self._ack(seq)
        self._close_ack()
