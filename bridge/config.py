# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import hashlib
import logging
import os
import socket
import uuid
from pathlib import Path
from typing import Any


log = logging.getLogger("pdd.bridge")

DEFAULT_LOG_DIR = r"D:\kefuAgent\探域\tanyu2.9.1\logs"
DEFAULT_SERVER_URL = "http://47.107.138.228:18765"


def resolve_log_dir(path: str) -> str:
    """探域升级会换版本目录（2.9.1 -> 3.0.2），配置里的旧路径会失效。

    路径不存在时取同级最新的 tanyu*/logs。回退链路指向不存在的目录就是静默无源，
    比直接报错难查得多。
    """
    if not path:
        return path
    if Path(path).is_dir():
        return path
    root = Path(path).parent.parent
    if not root.is_dir():
        return path
    candidates = []
    for platform_dir in root.glob("tanyu*"):
        logs = platform_dir / "logs"
        try:
            if logs.is_dir():
                candidates.append((logs.stat().st_mtime, logs))
        except OSError:
            continue
    if not candidates:
        return path
    resolved = str(max(candidates)[1])
    log.warning("tanyu_log_dir 不存在, 自动改用 %s (配置值: %s)", resolved, path)
    return resolved


def machine_device_id() -> str:
    """Return one stable, non-secret identity for this Windows computer."""
    machine_guid = ""
    if os.name == "nt":
        try:
            import winreg

            access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
                0,
                access,
            ) as key:
                machine_guid = str(winreg.QueryValueEx(key, "MachineGuid")[0] or "").strip()
        except (OSError, ImportError):
            machine_guid = ""
    if machine_guid:
        source = "windows-machine-guid\0" + machine_guid.lower()
    else:
        source = "host-mac\0%s\0%012x" % (socket.gethostname().lower(), uuid.getnode())
    digest = hashlib.sha256(
        ("pdd-bridge-device-v1\0" + source).encode("utf-8", "surrogatepass")
    ).hexdigest()
    return "device-" + digest


def default_config_path(platform: str = "pdd") -> Path:
    env = os.environ.get("PDD_BRIDGE_CONFIG", "").strip()
    if env:
        return Path(env)
    from .platforms import get_platform
    plat = get_platform(platform)
    name = plat.default_config_name
    if getattr(os.sys, "frozen", False):
        return Path(os.sys.executable).resolve().parent / name
    return Path(__file__).resolve().parent.parent / name


def load_config(path: Path | None = None, *, platform: str = "") -> dict[str, Any]:
    # Detect platform from filename when path given.
    cfg_path = path
    if cfg_path is None:
        plat_hint = platform or os.environ.get("BRIDGE_PLATFORM") or "pdd"
        cfg_path = default_config_path(plat_hint)
    data: dict[str, Any] = {}
    if cfg_path.is_file():
        # utf-8-sig: tolerate BOM from editors / PowerShell Set-Content -Encoding UTF8
        data = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError(f"config must be a JSON object: {cfg_path}")
    from .platforms import normalize_platform
    plat = normalize_platform(platform or data.get("platform") or (
        "taobao" if "taobao" in cfg_path.name.lower() or "qianniu" in cfg_path.name.lower() else "pdd"
    ))
    default_ver = "9.77.01N" if plat == "taobao" else "3.5.0.40"
    out = {
        "platform": plat,
        "server_url": str(data.get("server_url") or DEFAULT_SERVER_URL).rstrip("/"),
        "agent_token": str(
            data.get("agent_token")
            or os.environ.get("PDD_BRIDGE_TOKEN")
            or os.environ.get("BRIDGE_AGENT_TOKEN")
            or ""
        ),
        "agent_id": str(data.get("agent_id") or ""),
        "device_id": str(data.get("device_id") or machine_device_id()),
        "agent_name": str(data.get("agent_name") or socket.gethostname()),
        # Legacy keys remain in the normalized shape so old wrappers do not
        # break, but authorization is exclusively enforced by the center.
        "allowed_shop_ids": [],
        "enforce_allowed_shop_ids": False,
        "tanyu_log_dir": resolve_log_dir(str(data.get("tanyu_log_dir") or DEFAULT_LOG_DIR)),
        "platform_version": str(data.get("platform_version") or default_ver),
        "poll_interval_ms": int(data.get("poll_interval_ms") or 200),
        "heartbeat_seconds": float(data.get("heartbeat_seconds") or 5.0),
        "command_poll_seconds": float(data.get("command_poll_seconds") or 1.5),
        "dll_port": data.get("dll_port"),
        "workbench_pid": data.get("workbench_pid"),
        "config_path": str(cfg_path),
        "local_queue_path": str(
            data.get("local_queue_path")
            or (cfg_path.parent / f"bridge_queue_{plat}.jsonl")
        ),
        "command_journal_path": str(
            data.get("command_journal_path")
            or (cfg_path.parent / f"bridge_commands_{plat}.json")
        ),
        "local_workbench_url": str(
            data.get("local_workbench_url")
            or data.get("local_seat_url")
            or os.environ.get("KEFU_LOCAL_SEAT_URL")
            or "http://127.0.0.1:18767"
        ).rstrip("/"),
        "manage_local_workbench": bool(data.get("manage_local_workbench", True)),
        "dual_write_local_workbench": bool(data.get("dual_write_local_workbench", True)),
        "local_gateway_exe": str(data.get("local_gateway_exe") or ""),
        "parser_profile": data.get("parser_profile") if isinstance(data.get("parser_profile"), dict) else None,
        "parser_profile_path": str(data.get("parser_profile_path") or ""),
        "parser_profile_cache_path": str(
            data.get("parser_profile_cache_path")
            or (cfg_path.parent / "parser_profile_pdd.last_good.json")
        ),
        # Taobao: dry_run default off if key present; ui paste default OFF (串台风险).
        "dry_run": bool(data.get("dry_run") if "dry_run" in data else (plat == "taobao")),
        "allow_ui_fallback": bool(
            data.get("allow_ui_fallback")
            if "allow_ui_fallback" in data
            else False  # never default-on for taobao/pdd — paste can 串台
        ),
        # CDP 实时数据源 (PDD): data_source="cdp" 脱离探域日志, "tanyu_logs" 回退旧链路
        "data_source": str(data.get("data_source") or "cdp"),
        "cdp_auto_fallback": bool(data.get("cdp_auto_fallback", True)),
        "cdp_fallback_after_seconds": float(data.get("cdp_fallback_after_seconds") or 60),
        "cdp_port": data.get("cdp_port"),
        "cdp_poll_interval": float(data.get("cdp_poll_interval") or 0.2),
        "cdp_rescan_seconds": float(data.get("cdp_rescan_seconds") or 20),
        "cdp_emit_history": bool(data.get("cdp_emit_history", False)),
        # 可选：走注入 DLL 直发（需先注入, 未就绪自动回退 CDP）。默认关。
        "send_via_dll": bool(data.get("send_via_dll", False)),
        "send_via_dll_pipe": str(data.get("send_via_dll_pipe") or r"\\.\pipe\pdd_send_bridge"),
        "pdd_dll_path": str(data.get("pdd_dll_path") or r"D:\temp\pdd-send-hook\out\pdd_send_v3.dll"),
        "pdd_injector_path": str(data.get("pdd_injector_path") or r"D:\temp\pdd-send-hook\out\injector.exe"),
        # 原生接收（v0.7）: recv_via_dll 开启后从注入 DLL 收 push 帧；
        # recv_slot=-1 表示虚表槽位未确认（只允许探针模式），确认后填真实槽位号。
        "recv_via_dll": bool(data.get("recv_via_dll", False)),
        "recv_slot": int(data.get("recv_slot") or -1),
        "recv_mode": int(data.get("recv_mode") or 0),
    }
    if not out["agent_id"]:
        out["agent_id"] = (
            f"{plat}-"
            f"{uuid.uuid5(uuid.NAMESPACE_DNS, socket.gethostname() + '|' + str(cfg_path.resolve())).hex[:12]}"
        )
    return out


def save_config(cfg: dict[str, Any], path: Path | None = None) -> Path:
    cfg_path = path or Path(cfg.get("config_path") or default_config_path(str(cfg.get("platform") or "pdd")))
    payload = {
        "platform": cfg.get("platform"),
        "server_url": cfg.get("server_url"),
        "agent_token": cfg.get("agent_token"),
        "agent_id": cfg.get("agent_id"),
        "device_id": cfg.get("device_id") or cfg.get("agent_id"),
        "agent_name": cfg.get("agent_name"),
        "allowed_shop_ids": [],
        "enforce_allowed_shop_ids": False,
        "tanyu_log_dir": cfg.get("tanyu_log_dir"),
        "platform_version": cfg.get("platform_version"),
        "poll_interval_ms": cfg.get("poll_interval_ms"),
        "heartbeat_seconds": cfg.get("heartbeat_seconds"),
        "command_poll_seconds": cfg.get("command_poll_seconds"),
        "local_queue_path": cfg.get("local_queue_path"),
        "command_journal_path": cfg.get("command_journal_path"),
        "local_workbench_url": cfg.get("local_workbench_url"),
        "manage_local_workbench": bool(cfg.get("manage_local_workbench", True)),
        "dual_write_local_workbench": bool(cfg.get("dual_write_local_workbench", True)),
        "local_gateway_exe": cfg.get("local_gateway_exe") or "",
        "parser_profile": cfg.get("parser_profile"),
        "parser_profile_path": cfg.get("parser_profile_path") or "",
        "parser_profile_cache_path": cfg.get("parser_profile_cache_path") or "",
        "dll_port": cfg.get("dll_port"),
        "workbench_pid": cfg.get("workbench_pid"),
        "dry_run": cfg.get("dry_run"),
        "allow_ui_fallback": bool(cfg.get("allow_ui_fallback", False)),
        "data_source": str(cfg.get("data_source") or "cdp"),
        "cdp_auto_fallback": bool(cfg.get("cdp_auto_fallback", True)),
        "cdp_fallback_after_seconds": float(cfg.get("cdp_fallback_after_seconds") or 60),
        "cdp_port": cfg.get("cdp_port"),
        "cdp_poll_interval": float(cfg.get("cdp_poll_interval") or 0.2),
        "cdp_emit_history": bool(cfg.get("cdp_emit_history", False)),
        "send_via_dll": bool(cfg.get("send_via_dll", False)),
        "pdd_dll_path": str(cfg.get("pdd_dll_path") or ""),
        "pdd_injector_path": str(cfg.get("pdd_injector_path") or ""),
        "recv_via_dll": bool(cfg.get("recv_via_dll", False)),
        "recv_slot": int(cfg.get("recv_slot") or -1),
        "recv_mode": int(cfg.get("recv_mode") or 0),
    }
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8 without BOM (PowerShell UTF8 encoding often adds BOM and breaks json.loads)
    cfg_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return cfg_path


def write_example_config(path: Path | None = None, *, platform: str = "pdd") -> Path:
    from .platforms import get_platform
    plat = get_platform(platform)
    cfg_path = path or default_config_path(plat.name)
    example = {
        "platform": plat.name,
        "server_url": DEFAULT_SERVER_URL,
        "agent_token": "change-me-to-center-issued-token",
        "agent_id": "",
        "device_id": "",
        "agent_name": f"{plat.label}-{socket.gethostname()}",
        "allowed_shop_ids": [],
        "enforce_allowed_shop_ids": False,
        "tanyu_log_dir": DEFAULT_LOG_DIR,
        "platform_version": "9.77.01N" if plat.name == "taobao" else "3.5.0.40",
        "poll_interval_ms": 200,
        "heartbeat_seconds": 5,
        "command_poll_seconds": 1.5,
        "local_queue_path": str(cfg_path.parent / f"bridge_queue_{plat.name}.jsonl"),
        "command_journal_path": str(cfg_path.parent / f"bridge_commands_{plat.name}.json"),
        "local_workbench_url": "http://127.0.0.1:18767",
        "manage_local_workbench": True,
        "dual_write_local_workbench": True,
        "local_gateway_exe": "",
        "parser_profile": None,
        "parser_profile_path": "",
        "parser_profile_cache_path": str(cfg_path.parent / "parser_profile_pdd.last_good.json"),
        "dll_port": None,
        "workbench_pid": None,
        "report_all": True,
        "dedup_mode": "platform_id",
        "raw_archive_path": "",
        "delivery_ledger_enabled": True,
        "delivery_ledger_path": "",
        "dry_run": plat.name == "taobao",
        "data_source": "cdp",
        "cdp_auto_fallback": True,
        "cdp_fallback_after_seconds": 60,
        "cdp_port": None,
        "cdp_poll_interval": 0.2,
        "cdp_emit_history": False,
        "cdp_rescan_seconds": 20,
    }
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    if not cfg_path.exists():
        cfg_path.write_text(json.dumps(example, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg_path
