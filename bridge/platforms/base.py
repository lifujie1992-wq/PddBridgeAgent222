# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any, Dict, List, Tuple


class PlatformBase:
    name = "unknown"
    label = "未知平台"
    header_color = "#6e7781"
    default_config_name = "bridge_config.json"
    exe_product_name = "PddBridgeAgent"

    def log_attach_specs(self) -> List[Tuple[str, str, bool]]:
        """Return [(handle_name, glob_pattern, prefer_nonempty), ...]"""
        return []

    def parse_line(self, line: str, source: str = "") -> List[Dict[str, Any]]:
        return []

    def channel_status(self, cfg: dict) -> Dict[str, Any]:
        return {
            "dll_ready": False,
            "dll_port": None,
            "workbench_pid": None,
            "port_discovery": "not_found",
            "platform": self.name,
        }

    def send_text(
        self,
        buyer_id: str,
        content: str,
        account: str,
        *,
        cfg: dict,
        dry_run: bool = False,
    ) -> dict:
        return {
            "ok": False,
            "status": "failed",
            "error": f"send not implemented for platform={self.name}",
            "error_user": f"{self.label}发送尚未实现",
        }

    def open_chat(
        self,
        buyer_id: str,
        account: str,
        *,
        buyer_nick: str = "",
        cfg: dict | None = None,
    ) -> dict:
        """Focus official client conversation for production adsorb jump (no send)."""
        return {
            "ok": False,
            "status": "unsupported",
            "error": f"open_chat not implemented for platform={self.name}",
            "error_user": f"{self.label}暂不支持一键跳转会话，请在官方客户端手动打开",
            "real_send": False,
            "via": "open_chat",
            "buyer_id": buyer_id,
            "account": account,
            "buyer_nick": buyer_nick,
        }
