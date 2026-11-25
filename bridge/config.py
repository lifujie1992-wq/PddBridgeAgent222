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

# 配置版本：每次发布里如果有「调优过的默认值」需要覆盖老配置，就 +1。
# 老配置里键都存在时 load_config 不会用新默认值（data.get(key) or default），
# 结果就是同事装上了新版、行为却还是老毛病。这里用版本号做一次性迁移。
CONFIG_VERSION = 1

# v1（0.7.5.0）：全部是实测调优过的行为参数
_MIGRATIONS_V1: dict[str, Any] = {
    "data_source": "cdp",                 # 自有通道：CDP 页面通道 + 自有 DLL（脱离探域）
    "poll_interval_ms": 200,
    "heartbeat_seconds": 5.0,
    "command_poll_seconds": 1.5,          # 中心长轮询 20s 会让 AI 回复排名延迟几十秒
    "cdp_poll_interval": 1.0,
    "cdp_auto_fallback": True,
    "cdp_fallback_after_seconds": 60.0,
    "upload_batch_size": 500,
    "upload_concurrency": 16,
    "history_pull_seconds": 120,
    "history_pull_size": 10,
    "history_report_max_age_seconds": 1800,
    "history_report_user_only": True,
    "dual_write_local_workbench": True,
    "manage_local_workbench": True,
    "local_workbench_url": "http://127.0.0.1:18767",
    "outgoing_dedup_seconds": 60,
    "send_confirm_timeout_seconds": 10.0,
    "self_send_match_seconds": 180,
}

# 这些路径如果指向不存在的目录（换安装目录/换盘），就落回配置文件旁边
_PATH_KEYS_V1 = (
    ("local_queue_path", "bridge_queue_pdd.jsonl"),
    ("command_journal_path", "bridge_commands_pdd.json"),
    ("parser_profile_cache_path", "parser_profile_pdd.last_good.json"),
)


def migrate_config(cfg_path: Path, data: dict[str, Any]) -> dict[str, Any]:
    """把老版本配置文件升级到当前版本（写回前先备份）。绝不影响启动。"""
    if not isinstance(data, dict):
        return data
    try:
        version = int(data.get("config_version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version >= CONFIG_VERSION:
        return data

    changed: list[str] = []
    for key, value in _MIGRATIONS_V1.items():
        # 只在字段缺失时补默认。老实现是“值不等于 v1 默认就覆盖”，
        # 结果客户显式设的 data_source="cdp" 每次启动都被改回 tanyu_logs ——
        # 谁改配置都白改，这是“脱离探域”推不动的根因。
        if key in data:
            continue
        data[key] = value
        changed.append(key)
    for key, filename in _PATH_KEYS_V1:
        current = str(data.get(key) or "")
        if current and not Path(current).parent.is_dir():
            data[key] = str(cfg_path.parent / filename)
            changed.append(key)
    data["config_version"] = CONFIG_VERSION

    try:
        backup = cfg_path.with_suffix(cfg_path.suffix + ".pre-0.7.5.0.bak")
        if cfg_path.is_file() and not backup.exists():
            backup.write_text(
                json.dumps(json.loads(cfg_path.read_text(encoding="utf-8-sig")),
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        cfg_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("配置已升级到 v%s（改动 %d 项）：%s", CONFIG_VERSION, len(changed),
                 ", ".join(changed) or "仅写回版本号")
    except (OSError, ValueError) as exc:
        log.warning("配置升级写回失败（继续用内存值）: %s", exc)
    return data
DEFAULT_SERVER_URL = "http://47.112.219.14:18765"


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
        # 克隆盘会连注册表一起复制, MachineGuid 不再唯一; 追加网卡 MAC
        # （硬件地址, 克隆盘不会复制）保证克隆机身份不同。
        source = ("windows-machine-guid\0" + machine_guid.lower()
                  + "\0mac\0%012x" % uuid.getnode())
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
        # 老配置一次性升级（同事升级安装后不用手改配置就能拿到新版行为）
        data = migrate_config(cfg_path, data)
    from .platforms import normalize_platform
    plat = normalize_platform(platform or data.get("platform") or "pdd")
    default_ver = "3.5.0.40"
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
        # 高并发进线调优：每批上送条数 / 单次唤醒排空预算 / 本地优先的最大宽限
        "upload_batch_size": int(data.get("upload_batch_size") or 500),
        "upload_concurrency": int(data.get("upload_concurrency") or 16),
        "websocket_enabled": bool(data.get("websocket_enabled", False)),
        "websocket_url": str(data.get("websocket_url") or ""),
        "websocket_path": str(data.get("websocket_path") or "/api/bridge/v1/ws"),
        "websocket_max_inflight": int(data.get("websocket_max_inflight") or 64),
        "upload_drain_budget_seconds": float(data.get("upload_drain_budget_seconds") or 3.0),
        "outgoing_dedup_seconds": float(data.get("outgoing_dedup_seconds") or 60.0),
        # tanyu_logs 为主时，用 CDP 周期回拉历史，补上“会话未激活→拼多多不推送”的消息
        "history_pull_seconds": float(data.get("history_pull_seconds") or 0),
        "history_pull_size": int(data.get("history_pull_size") or 20),
        "history_pull_max_buyers": int(data.get("history_pull_max_buyers") or 40),
        "history_pull_gap_seconds": float(data.get("history_pull_gap_seconds") or 0.3),
        "history_pull_timeout_seconds": float(data.get("history_pull_timeout_seconds") or 3.0),
        # 0.7.11：回扫并行度 / 目标表存活时间 / 零帧告警间隔
        "history_pull_workers": int(data.get("history_pull_workers") or 3),
        "history_target_ttl_seconds": float(data.get("history_target_ttl_seconds") or 86400.0),
        "capture_warn_seconds": float(data.get("capture_warn_seconds") or 600.0),
        "history_report_max_age_seconds": float(data.get("history_report_max_age_seconds") or 1800),
        # 历史/回扫事件等多久才上送。legacy=90s（兼容不认 is_history 的老中心）；
        # ack_capable=0s（中心自己按 is_history 决定回不回，不必再压 90 秒）。
        "order_gate_state": str(data.get("order_gate_state") or ""),
        "order_gate_window_seconds": float(data.get("order_gate_window_seconds") or 86400.0),
        "order_gate_fail_open": bool(data.get("order_gate_fail_open", False)),
        # Phase-3：补拉到的新鲜消息按实时上送（消除 90 秒晚到）
        "promote_fresh_pull": bool(data.get("promote_fresh_pull", True)),
        "fresh_pull_max_age_seconds": float(data.get("fresh_pull_max_age_seconds") or 30.0),
        "history_hold_policy": str(data.get("history_hold_policy") or "legacy"),
        "history_live_window_seconds": float(
            data["history_live_window_seconds"]
            if data.get("history_live_window_seconds") is not None else 90.0),
        "history_report_user_only": bool(data.get("history_report_user_only", True)),
        "send_confirm_timeout_seconds": float(data.get("send_confirm_timeout_seconds") or 10.0),
        "local_first_max_wait_seconds": float(data.get("local_first_max_wait_seconds") or 1.0),
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
        # 默认关闭；窗口粘贴有串台风险。
        "dry_run": bool(data.get("dry_run", False)),
        "allow_ui_fallback": bool(
            data.get("allow_ui_fallback")
            if "allow_ui_fallback" in data
            else False  # never default-on — paste can 串台
        ),
        # CDP 实时数据源 (PDD): data_source="cdp" 脱离探域日志, "tanyu_logs" 回退旧链路
        "data_source": str(data.get("data_source") or "cdp"),
        "cdp_auto_fallback": bool(data.get("cdp_auto_fallback", True)),
        "cdp_fallback_after_seconds": float(data.get("cdp_fallback_after_seconds") or 60),
        "cdp_port": data.get("cdp_port"),
        "cdp_poll_interval": float(data.get("cdp_poll_interval") or 0.2),
        # 单次 CDP eval 超时（默认 3s）与每店健康探测间隔（默认 30s）。
        # 20 店时 8s 超时会连锁拖死整片；探测也不能和取帧抢连接。
        "cdp_eval_timeout_seconds": float(data.get("cdp_eval_timeout_seconds") or 3.0),
        "cdp_health_interval_seconds": float(data.get("cdp_health_interval_seconds") or 30.0),
        "cdp_session_backoff_max_seconds": float(data.get("cdp_session_backoff_max_seconds") or 30.0),
        "cdp_rescan_seconds": float(data.get("cdp_rescan_seconds") or 20),
        "cdp_emit_history": bool(data.get("cdp_emit_history", False)),
        # 可选：走注入 DLL 直发（需先注入, 未就绪自动回退 CDP）。默认关。
        # 3.5.0.40 页面没有 JS socket，CDP 页面发送基本用不了；DLL 直发是唯一稳定通道，
        # 默认打开（已注入时生效，未注入自动回退 CDP，不影响老行为）。
        "send_via_dll": bool(data.get("send_via_dll", True)),
        "send_via_dll_pipe": str(data.get("send_via_dll_pipe") or ""),
        "pdd_dll_path": str(data.get("pdd_dll_path") or r"D:\temp\pdd-send-hook\out\pdd_send_v3.dll"),
        "pdd_injector_path": str(data.get("pdd_injector_path") or r"D:\temp\pdd-send-hook\out\injector.exe"),
        # 原生接收（v0.7）: recv_via_dll 开启后从注入 DLL 收 push 帧；
        # recv_slot=-1 表示虚表槽位未确认（只允许探针模式），确认后填真实槽位号。
        "recv_via_dll": bool(data.get("recv_via_dll", False)),
        "recv_slot": int(data.get("recv_slot") or -1),
        "recv_mode": int(data.get("recv_mode") or 0),
        # 全槽位探针在 3.5.0.40 会崩客户端（未初始化临界区）；默认走导出 detour。
        "recv_probe": bool(data.get("recv_probe", False)),
        # auto=全槽位嗅探（默认，进程级）；detour=导出 detour（只收心跳/出口帧）
        "recv_engine": str(data.get("recv_engine") or "auto"),
        # 接收管道名后缀（对应 DLL 侧 PDD_BRIDGE_PIPE_SUFFIX，空=默认管道）。
        # 一台机器跑多个安装/多个工作台时用它隔离，否则会抢同一条管道。
        "recv_pipe_suffix": str(data.get("recv_pipe_suffix") or ""),
        "recv_pipe_name": str(data.get("recv_pipe_name") or ""),
        "recv_ack_pipe_name": str(data.get("recv_ack_pipe_name") or ""),
    }
    # 记录用户显式写过的键：有些默认值会在 load 时补齐，下游无法区分
    # "用户设成 90" 和 "我们补的 90"（history_live_window_seconds 就踩过）。
    out["_explicit_keys"] = sorted(k for k in data.keys() if not str(k).startswith("_"))
    if not out["agent_id"]:
        out["agent_id"] = (
            f"{plat}-"
            f"{uuid.uuid5(uuid.NAMESPACE_DNS, socket.gethostname() + '|' + str(cfg_path.resolve()) + '|' + '%012x' % uuid.getnode()).hex[:12]}"
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
        "websocket_enabled": bool(cfg.get("websocket_enabled", False)),
        "websocket_url": str(cfg.get("websocket_url") or ""),
        "websocket_path": str(cfg.get("websocket_path") or "/api/bridge/v1/ws"),
        "websocket_max_inflight": int(cfg.get("websocket_max_inflight") or 64),
        # 历史回扫 / 上报门槛必须能存盘：GUI 保存只写 payload 里的键，
        # 老实现漏了这几个，导致"改了配置重启又变回去"。
        "history_pull_seconds": float(cfg.get("history_pull_seconds") or 0),
        "history_pull_size": int(cfg.get("history_pull_size") or 20),
        "history_pull_max_buyers": int(cfg.get("history_pull_max_buyers") or 40),
        "history_pull_gap_seconds": float(cfg.get("history_pull_gap_seconds") or 0.3),
        "history_pull_timeout_seconds": float(cfg.get("history_pull_timeout_seconds") or 3.0),
        "history_pull_workers": int(cfg.get("history_pull_workers") or 3),
        "history_target_ttl_seconds": float(cfg.get("history_target_ttl_seconds") or 86400.0),
        "capture_warn_seconds": float(cfg.get("capture_warn_seconds") or 600.0),
        "history_report_max_age_seconds": float(cfg.get("history_report_max_age_seconds") or 1800),
        "history_report_user_only": bool(cfg.get("history_report_user_only", True)),
        "order_gate_state": str(cfg.get("order_gate_state") or ""),
        "order_gate_window_seconds": float(cfg.get("order_gate_window_seconds") or 86400.0),
        "order_gate_fail_open": bool(cfg.get("order_gate_fail_open", False)),
        "promote_fresh_pull": bool(cfg.get("promote_fresh_pull", True)),
        "fresh_pull_max_age_seconds": float(cfg.get("fresh_pull_max_age_seconds") or 30.0),
        "history_hold_policy": str(cfg.get("history_hold_policy") or "legacy"),
        "history_live_window_seconds": float(
            cfg["history_live_window_seconds"]
            if cfg.get("history_live_window_seconds") is not None else 90.0),
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
        "cdp_eval_timeout_seconds": float(cfg.get("cdp_eval_timeout_seconds") or 3.0),
        "cdp_health_interval_seconds": float(cfg.get("cdp_health_interval_seconds") or 30.0),
        "cdp_session_backoff_max_seconds": float(cfg.get("cdp_session_backoff_max_seconds") or 30.0),
        "cdp_emit_history": bool(cfg.get("cdp_emit_history", False)),
        "send_via_dll": bool(cfg.get("send_via_dll", False)),
        "pdd_dll_path": str(cfg.get("pdd_dll_path") or ""),
        "pdd_injector_path": str(cfg.get("pdd_injector_path") or ""),
        "recv_via_dll": bool(cfg.get("recv_via_dll", False)),
        "recv_slot": int(cfg.get("recv_slot") or -1),
        "recv_mode": int(cfg.get("recv_mode") or 0),
        "recv_probe": bool(cfg.get("recv_probe", False)),
        "recv_engine": str(cfg.get("recv_engine") or "auto"),
        "recv_pipe_suffix": str(cfg.get("recv_pipe_suffix") or ""),
        "recv_pipe_name": str(cfg.get("recv_pipe_name") or ""),
        "recv_ack_pipe_name": str(cfg.get("recv_ack_pipe_name") or ""),
    }
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    # 保留盘上已有、但 payload 里未覆盖的字段（尤其是 config_version）。
    # 老实现每次保存都丢掉 config_version，下次加载又触发 v1 迁移，
    # 把 data_source 改回 tanyu_logs —— GUI 保存一次等于把配置打回旧行为。
    merged: dict[str, Any] = {}
    try:
        if cfg_path.is_file():
            existing = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
            if isinstance(existing, dict):
                merged.update(existing)
    except (OSError, ValueError):
        merged = {}
    merged.update(payload)
    merged["config_version"] = int(cfg.get("config_version") or CONFIG_VERSION)
    # utf-8 without BOM (PowerShell UTF8 encoding often adds BOM and breaks json.loads)
    cfg_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
        "platform_version": "3.5.0.40",
        "poll_interval_ms": 200,
        "heartbeat_seconds": 5,
        "command_poll_seconds": 1.5,
        "upload_batch_size": 500,
        "upload_concurrency": 16,
        "upload_drain_budget_seconds": 3.0,
        "outgoing_dedup_seconds": 60.0,
        "history_pull_seconds": 0,
        "history_pull_size": 20,
        "history_pull_max_buyers": 40,
        "history_pull_gap_seconds": 0.3,
        "history_pull_timeout_seconds": 3.0,
        "history_pull_workers": 3,
        "history_target_ttl_seconds": 86400,
        "capture_warn_seconds": 600,
        "history_report_max_age_seconds": 1800,
        "history_report_user_only": True,
        "order_gate_state": "",
        "order_gate_window_seconds": 86400,
        "order_gate_fail_open": False,
        "promote_fresh_pull": True,
        "fresh_pull_max_age_seconds": 30,
        "history_hold_policy": "legacy",
        "history_live_window_seconds": 90,
        "send_confirm_timeout_seconds": 10.0,
        "local_first_max_wait_seconds": 1.0,
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
        "dry_run": False,
        "data_source": "cdp",
        "cdp_auto_fallback": True,
        "cdp_fallback_after_seconds": 60,
        "cdp_port": None,
        "cdp_poll_interval": 0.2,
        "cdp_eval_timeout_seconds": 3.0,
        "cdp_health_interval_seconds": 30.0,
        "cdp_session_backoff_max_seconds": 30.0,
        "cdp_emit_history": False,
        "cdp_rescan_seconds": 20,
        # 原生接收（0.7.7+）：默认关；打开前必须先用 PROBE 探针确认 recv_slot。
        "recv_via_dll": False,
        "recv_slot": -1,
        "recv_mode": 0,
        "recv_probe": False,
        "recv_engine": "auto",
        "recv_pipe_suffix": "",
        "recv_pipe_name": "",
        "recv_ack_pipe_name": "",
        "pdd_dll_path": "",
        "pdd_injector_path": "",
    }
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    if not cfg_path.exists():
        cfg_path.write_text(json.dumps(example, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg_path
