# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .. import channel as pdd_channel
from .. import parser as pdd_parser
from .base import PlatformBase


class PddPlatform(PlatformBase):
    name = "pdd"
    label = "拼多多"
    header_color = "#1f6feb"
    default_config_name = "bridge_config.json"
    exe_product_name = "PddBridgeAgent"

    @staticmethod
    def _cdp_active(cfg: dict) -> bool:
        """分发按「活动数据源」: _effective_source(降级后=tanyu_logs) 优先于配置意图。"""
        return str(
            cfg.get("_effective_source") or cfg.get("data_source") or "cdp"
        ) == "cdp"

    def log_attach_specs(self) -> List[Tuple[str, str, bool]]:
        return [
            ("INSIDE", "inside_*.log", False),
            ("INJECT", "Injector_cnpdd*.log", False),
            ("PLUGIN", "*PddMsgPlugin*.log", False),
            ("LOGRUS", "logrus.log*", True),
        ]

    def is_candidate_line(self, line: str) -> bool:
        return pdd_parser.is_candidate_line(line)

    def parse_line(self, line: str, source: str = "") -> List[Dict[str, Any]]:
        rows = pdd_parser.parse_line(line, source)
        for row in rows:
            row["platform"] = self.name
        return rows

    def is_failure_line(self, line: str) -> bool:
        return pdd_parser.is_seller_failure_line(line)

    def channel_status(self, cfg: dict) -> Dict[str, Any]:
        if self._cdp_active(cfg):
            from ..pddbridge_source import channel_status_cdp

            st = channel_status_cdp(cfg)
            st["platform"] = self.name
            return st
        st = pdd_channel.channel_status(
            configured_port=cfg.get("dll_port"),
            configured_pid=cfg.get("workbench_pid"),
        )
        st["platform"] = self.name
        return st

    def open_chat(
        self,
        buyer_id: str,
        account: str,
        *,
        buyer_nick: str = "",
        cfg: dict | None = None,
    ) -> dict:
        """Try to focus PDD workbench conversation (CDP 注入引导 / 探域 DLL)."""
        cfg = cfg or {}
        if self._cdp_active(cfg):
            from ..pddbridge_source import open_chat_pdd_cdp

            return open_chat_pdd_cdp(buyer_id, account, buyer_nick=buyer_nick, cfg=cfg)
        return pdd_channel.open_chat_pdd(
            buyer_id,
            account,
            buyer_nick=buyer_nick,
            platform_version=str(cfg.get("platform_version") or "3.5.0.40"),
            configured_port=cfg.get("dll_port"),
            configured_pid=cfg.get("workbench_pid"),
            tanyu_log_dir=str(cfg.get("tanyu_log_dir") or ""),
        )

    def send_text(
        self,
        buyer_id: str,
        content: str,
        account: str,
        *,
        cfg: dict,
        dry_run: bool = False,
    ) -> dict:
        # CDP is suitable for realtime intake, but its send receipt has proven
        # unreliable on the local PDD workbench. Prefer Tanyu's sender whenever
        # its log directory is configured, because it can verify delivery.
        if self._cdp_active(cfg) and not cfg.get("tanyu_log_dir"):
            from ..pddbridge_source import send_text_cdp

            return send_text_cdp(buyer_id, content, account, cfg=cfg, dry_run=dry_run)
        return pdd_channel.send_text(
            buyer_id,
            content,
            account,
            platform_version=str(cfg.get("platform_version") or "3.5.0.40"),
            configured_port=cfg.get("dll_port"),
            configured_pid=cfg.get("workbench_pid"),
            dry_run=dry_run,
            buyer_nick=str(cfg.get("_buyer_nick") or ""),
            tanyu_log_dir=str(cfg.get("tanyu_log_dir") or ""),
            is_expired=cfg.get("_command_is_expired"),
        )
