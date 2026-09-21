"""原生接收通道（v0.7 复刻探域接收能力）。

注入 DLL 在工作台进程内通过 CMChatImpl::Register(VIMsgCallBack) 收 push 帧，
经 \\.\pipe\pdd_recv_bridge 以 "FRAME|<json>\n" 逐帧推给本模块；本模块喂给
PddbridgeSource.feed_native_frame，复用与 CDP 相同的帧处理管线。

槽位说明：VIMsgCallBack 虚表哪个槽位收买家消息是版本事实。recv_slot=-1 表示
未确认，只允许探针模式（DLL 侧 PROBE|<log> 全槽位记录）；确认后配置 recv_slot
才会真正注册转发。
"""
from __future__ import annotations

import logging
import threading

log = logging.getLogger("pdd_native_recv")

SEND_PIPE = r"\\.\pipe\pdd_send_bridge"
RECV_PIPE = r"\\.\pipe\pdd_recv_bridge"


def parse_recv_line(line: bytes | str) -> str | None:
    """FRAME|<json> -> json 文本；其余行（心跳/空行）忽略。"""
    if isinstance(line, bytes):
        line = line.decode("utf-8", "replace")
    line = line.rstrip("\r\n")
    if not line.startswith("FRAME|"):
        return None
    raw = line[len("FRAME|"):]
    return raw or None


class NativeReceiver:
    """常驻线程：连接收管道 → 逐帧回调 on_frame(raw_json)。断线自动重连。"""

    def __init__(self, on_frame, cfg: dict | None = None):
        self.on_frame = on_frame
        self.cfg = cfg or {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error = ""
        self.frame_count = 0

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

    def probe(self, log_path: str) -> str:
        """探针模式：注册回调并全槽位记录触发日志，不转发。"""
        return self._command(f"PROBE|{log_path}")

    def recv_state(self) -> str:
        return self._command("RECV_STATE")

    @staticmethod
    def _command(text: str, timeout: float = 3.0) -> str:
        import os
        import time as _t
        deadline = _t.time() + timeout
        while _t.time() < deadline:
            try:
                with open(SEND_PIPE, "r+b", buffering=0) as handle:
                    handle.write(text.encode("utf-8"))
                    handle.flush()
                    return handle.read(256).decode("utf-8", "replace").strip()
            except FileNotFoundError:
                return "pipe_not_found"
            except OSError:
                _t.sleep(0.2)
        return "pipe_timeout"

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

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._pump()
            except Exception as exc:  # 管道不存在/断开: 稍后重连
                self.last_error = str(exc)
            self._stop.wait(2.0)

    def _pump(self) -> None:
        with open(RECV_PIPE, "rb", buffering=0) as handle:
            buf = b""
            while not self._stop.is_set():
                chunk = handle.read(8192)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    raw = parse_recv_line(line)
                    if not raw:
                        continue
                    self.frame_count += 1
                    self.on_frame(raw)
