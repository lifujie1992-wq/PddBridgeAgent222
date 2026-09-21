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
CONFIG_VERSION = 3

# v1（0.7.5.0）：全部是实测调优过的行为参数
_MIGRATIONS_V1: dict[str, Any] = {
    "data_source": "tanyu_logs",          # 探域日志为主 + CDP 周期补拉（实测 10/10 店铺不掉线）
    "poll_interval_ms": 200,
    "heartbeat_seconds": 5.0,
    "command_poll_seconds": 1.5,          # 中心长轮询 20s 会让 AI 回复排名延迟几十秒
    "cdp_poll_interval": 1.0,
    "cdp_auto_fallback": True,
    "cdp_fallback_after_seconds": 60.0,
    "upload_batch_size": 500,
    "upload_concurrency": 3,
    "history_pull_seconds": 120,
# 跨数据源入站判重窗口（秒）。0 = 自动（= max(600, history_pull_seconds × 5)）。
# 探域日志腿实时报、CDP 腿每 history_pull_seconds 回拉一次，同一条买家消息会被
# 两条腿各报一次（实测间隔 15~18s）。窗口必须盖过回拉间隔，否则拦不住。
    "cross_source_dedup_seconds": 0,
    # 不串台：只处理"本机工作台实际登录的席位"的消息。默认开。
    # 席位集合自动发现（CDP 会话 = 工作台打开的标签页）；这两个键是配置侧补充。
    # 探测不到时 fail-open（放行 + 大声告警），所以想强制只服务固定席位就填
    # allowed_seat_accounts。
    # 命令长轮询并发条数。中心 /commands 每次固定 ~9.15 秒（实测），
    # 单条线 = 每 10.7 秒才能取一次指令，回复可能在取到前作废。
    # 3 条并发把间隔压到约 3.6 秒。1 = 原行为。
    "command_poll_workers": 3,

    "enforce_seat_scope": True,
    "seat_scope_refresh_seconds": 30.0,
    "allowed_seat_accounts": [],
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

# v2（0.9.1.0）：10 店铺峰值实测调优。这两个键在老配置里**都存在**，
# load_config 的 `data.get(k) or default` 只在键缺失时兜底，所以改默认值对
# 已装机的同事完全无效 —— 必须走迁移覆盖，否则装了新版行为还是老毛病。
_MIGRATIONS_V2: dict[str, Any] = {
    # 3 → 6：上传池只有 3 个线程时，突发一到就有批次在队列里排队，
    # 批量上传的并发优势被自己人卡住（实测 10 店铺突发时中心侧可见明显串行间隙）。
    "upload_concurrency": 6,
    # 1.0 → 0.3：这是**纯等待**，不是处理时间。本地优先的宽限期内事件不发给中心，
    # 直接加在峰值延迟上（实测走门控 1.11s vs 直通 0.08s）。宽限本身是"给本地工作台
    # 一点抢先时间"，0.3s 足够；再长就是拿大脑的响应时间换本地的抢先。
    "local_first_max_wait_seconds": 0.3,
}

# 按版本号递增累加。升级时**只跑没跑过的那些**：老实现是"只要 version < CONFIG_VERSION
# 就把 V1 整份重放一遍"，V1 里包含 data_source 这类用户会手改的键，重放会把用户
# 改过的值再冲掉一次。
_MIGRATIONS: tuple[tuple[int, dict[str, Any]], ...] = (
    (1, _MIGRATIONS_V1),
    (2, _MIGRATIONS_V2),
)

# 这些键是**绝对路径**。升级时把 bridge_config.json 拷进新安装目录（换版本、换盘、
# 换目录都会这么做），它们仍然指向老目录 —— 于是新安装继续读写**老安装的**队列和
# 台账。两套安装共用一个 outbox：清理/卸载其中一个，另一个的消息会被一起带走，
# 而且日志上完全看不出来。实测踩过（打包时拷配置带过去的）。
_PATH_KEYS = (
    "local_queue_path",
    "command_journal_path",
    "parser_profile_cache_path",
    "delivery_ledger_path",
)


def relocate_paths_from_other_install(cfg_path: Path, data: dict[str, Any]) -> list[str]:
    """路径指向"同产品的另一套安装"时，挪回本配置所在的目录。

    判据很严：父目录不是本配置所在目录，**且**那个目录里确实有 bridge_config.json
    （= 是同产品的另一套安装）。用户故意把队列放到共享目录（父目录里没有
    bridge_config.json）时不动 —— 那是合理配置，不该被"纠正"。
    """
    if not isinstance(data, dict):
        return []
    own_dir = cfg_path.parent
    moved: list[str] = []
    source_dirs: set[str] = set()
    for key in _PATH_KEYS:
        current = str(data.get(key) or "").strip()
        if not current:
            continue
        try:
            parent = Path(current).parent
        except (OSError, ValueError):
            continue
        if parent == own_dir:
            continue
        if not (parent / "bridge_config.json").is_file():
            continue
        # 先把老目录记下来再改 data —— 改完再读 data 打出来的就是新路径了。
        source_dirs.add(str(parent))
        data[key] = str(own_dir / Path(current).name)
        moved.append(key)
    if moved:
        log.warning("配置里的路径指向另一套安装 %s，已挪回 %s（否则两套安装会共用"
                    "队列/台账文件）：%s",
                    ", ".join(sorted(source_dirs)), own_dir, ", ".join(moved))
    return moved


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
    # 只跑「本配置还没跑过」的迁移段。全量重放会把用户后来手改过的 V1 键再冲掉一次。
    for target, migrations in _MIGRATIONS:
        if version >= target:
            continue
        for key, value in migrations.items():
            if data.get(key) != value:
                changed.append(key)
                data[key] = value
    if version < 1:
        for key, filename in _PATH_KEYS_V1:
            current = str(data.get(key) or "")
            if current and not Path(current).parent.is_dir():
                data[key] = str(cfg_path.parent / filename)
                changed.append(key)
    changed.extend(relocate_paths_from_other_install(cfg_path, data))
    data["config_version"] = CONFIG_VERSION

    try:
        # 备份名带目标版本号：老实现是固定名 + `not backup.exists()`，
        # 于是"上次迁移留过备份"会让这次静默不备份 —— 正是最需要备份的时候。
        backup = cfg_path.with_suffix(cfg_path.suffix + f".pre-v{CONFIG_VERSION}.bak")
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
DEFAULT_SERVER_URL = "http://203.0.113.10:18765"


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


def as_bool(value: Any, default: bool = False) -> bool:
    """JSON 里的布尔要按字面解析。

    `bool("false")` 是 True —— 手改过的配置、模板生成的字符串都会把一个
    默认关的开关变成默认开（这个坑在 allow_ui_fallback 上已经有实例）。
    agent.py 解析同类值时用的就是这套字符串判定。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _shop_id_allowlist(value: Any) -> list[str]:
    """灰度店铺白名单：只接受明确的字符串列表，其余一律视为空（fail closed）。

    缺失键、`[]`、字符串、数字、`None` 都等于**关闭**；绝不接受"除黑名单外全放"，
    也不接受把 `"mall_123"` 这种裸字符串当成单元素列表。
    """
    if not isinstance(value, (list, tuple, set)):
        return []
    out: list[str] = []
    for item in value:
        shop_id = str(item or "").strip()
        if shop_id and shop_id not in out:
            out.append(shop_id)
    return out


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
        # 迁移只在版本号落后时跑一次；"配置被拷进另一套安装"这件事随时可能发生，
        # 所以每次加载都查一遍（只改内存 + 告警，不偷偷写盘）。
        relocate_paths_from_other_install(cfg_path, data)
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
        # 高并发进线调优：每批上送条数 / 单次唤醒排空预算 / 本地优先的最大宽限
        "upload_batch_size": int(data.get("upload_batch_size") or 500),
        "upload_concurrency": int(data.get("upload_concurrency") or 6),
        "upload_drain_budget_seconds": float(data.get("upload_drain_budget_seconds") or 3.0),
        # 灰度（默认全关，fail closed）：只有白名单命中的店铺才走新路径。
        #   immediate_ingress_shop_ids —— 命中时买家消息不再被订单上下文门控扣住，
        #     原始消息立即上传，订单上下文随后按同一个 msg_id 补发一条增强事件；
        #     中心按 msg_id 判重，只有首次插入才会入队 AI 任务，不会二次回复。
        #   command_sender_shop_ids / command_sender_workers —— 命中时出站发送交给
        #     并发发送池，取指令循环不再被上一条的发送确认超时（默认 10s）占住；
        #     同一买家仍由逐会话锁保证有序。
        "immediate_ingress_shop_ids": _shop_id_allowlist(data.get("immediate_ingress_shop_ids")),
        "command_sender_shop_ids": _shop_id_allowlist(data.get("command_sender_shop_ids")),
        "command_sender_workers": int(data.get("command_sender_workers") or 6),
        "outgoing_dedup_seconds": float(data.get("outgoing_dedup_seconds") or 60.0),
        # 中心 WS 上行通道（protocol_version 1）。默认关：灰度按机器打开，连接失败
        # 或协议不匹配时自动回落 HTTP。**故意不放进 _MIGRATIONS_V1** —— 迁移会强制
        # 覆盖老配置，那会把已灰度打开/关闭的选择冲掉。
        "center_ws_enabled": as_bool(data.get("center_ws_enabled"), False),
        # 留空则从 server_url 推导（http→ws / https→wss + path）。WS 走独立入口
        # （单独域名/端口/前置网关）时才需要显式填。
        "center_ws_url": str(data.get("center_ws_url") or ""),
        "center_ws_path": str(data.get("center_ws_path") or "/api/bridge/v1/ws"),
        "center_ws_ack_timeout_seconds": float(
            data.get("center_ws_ack_timeout_seconds") or 10.0),
        # tanyu_logs 为主时，用 CDP 周期回拉历史，补上“会话未激活→拼多多不推送”的消息
        "history_pull_seconds": float(data.get("history_pull_seconds") or 0),
        "history_pull_size": int(data.get("history_pull_size") or 20),
        "history_pull_max_buyers": int(data.get("history_pull_max_buyers") or 40),
        "history_pull_gap_seconds": float(data.get("history_pull_gap_seconds") or 0.3),
        "history_report_max_age_seconds": float(data.get("history_report_max_age_seconds") or 1800),
        "history_report_user_only": bool(data.get("history_report_user_only", True)),
        "send_confirm_timeout_seconds": float(data.get("send_confirm_timeout_seconds") or 10.0),
        "local_first_max_wait_seconds": float(data.get("local_first_max_wait_seconds") or 0.3),
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
        # 单个会话一次 drain/pull 的 eval 超时预算。原实现用的是 websocket 连接超时
        # （8s），一次 eval 能吃掉 8 秒；10 个会话串行就是 80 秒，其间谁都别想拿到消息。
        "cdp_eval_timeout": float(data.get("cdp_eval_timeout") or 4.0),
        # 单轮 drain 的总预算：跑满就停手，下一轮从**断点**继续（见 _pump_cursor）。
        # 没有这个的话一个坏 tab 每轮都吃掉全部预算，排在它后面的会话永远轮不到。
        "cdp_round_budget_seconds": float(data.get("cdp_round_budget_seconds") or 6.0),
        # 连续多少轮 drain 失败才丢弃该会话。原来一次失败就丢，页面偶发卡顿会被
        # 误判成注入丢失，把好好的店铺整个摘掉（要等下一轮重扫才挂回来）。
        "cdp_session_fail_limit": int(data.get("cdp_session_fail_limit") or 3),
        # 只补历史（history_only）时，没有 pull 在飞就按这个间隔空转巡检一次。
        # 原来按 cdp_poll_interval 轮 10 个会话 —— 每秒几十次 eval 全是空转。
        "history_only_idle_poll_seconds": float(
            data.get("history_only_idle_poll_seconds") or 5.0),
        # pull_history 发起后，按快节奏 drain 收应答的窗口。
        "pull_drain_window_seconds": float(data.get("pull_drain_window_seconds") or 6.0),
        # 按 msg_id 判重的窗口。0 = 自动（= max(120, history_pull_seconds × 5)）。
        # **必须长于"同一条消息被重新报出来的间隔"**：实测历史补拉每 ~243s 就把
        # 同一批消息重报一遍，窗口写死 120s 时必然每次都在重报前过期，去重失效。
        "dedup_window_seconds": float(data.get("dedup_window_seconds") or 0),
        # 跨数据源（探域日志 / CDP 回拉）入站判重窗口。0 = 自动。
        "cross_source_dedup_seconds": float(
            data.get("cross_source_dedup_seconds") or 0),
        "enforce_seat_scope": bool(data.get("enforce_seat_scope", True)),
        "seat_scope_refresh_seconds": float(
            data.get("seat_scope_refresh_seconds") or 30.0),
        "allowed_seat_accounts": [
            str(v) for v in (data.get("allowed_seat_accounts") or [])
            if str(v).strip()],
        # 静默检测：超过这么久没收到任何帧就告警（PDD 未打开会话时不推送）。
        # 0 关闭。原来的 socket 探针读的是探域包装对象，恒返回未连接，是假信号。
        "silent_warn_seconds": float(data.get("silent_warn_seconds") or 300.0),
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
            f"{uuid.uuid5(uuid.NAMESPACE_DNS, socket.gethostname() + '|' + str(cfg_path.resolve()) + '|' + '%012x' % uuid.getnode()).hex[:12]}"
        )
    return out


# 这些键只在本进程内使用，不写回配置文件。
_NON_PERSISTED_KEYS = frozenset(("config_path",))


def _persistable_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """挑出能落盘的键：跳过运行时内部键与不可 JSON 序列化的值。

    load_config 会往 cfg 里塞一批仅供运行时使用的键（_pddbridge_source、
    _effective_source、_buyer_nick、_command_is_expired ...），它们不能写回文件。
    """
    out: dict[str, Any] = {}
    for key, value in (cfg or {}).items():
        if not isinstance(key, str) or key.startswith("_") or key in _NON_PERSISTED_KEYS:
            continue
        try:
            json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            continue
        out[key] = value
    return out


def save_config(cfg: dict[str, Any], path: Path | None = None) -> Path:
    cfg_path = path or Path(cfg.get("config_path") or default_config_path(str(cfg.get("platform") or "pdd")))
    # 已知键的规范化值（类型转换/默认值/派生路径）。注意它只是"覆盖层"，
    # 不再是唯一的输出源 —— 见下面的合并逻辑。
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
        "silent_warn_seconds": float(cfg.get("silent_warn_seconds") or 300.0),
        "send_via_dll": bool(cfg.get("send_via_dll", False)),
        "pdd_dll_path": str(cfg.get("pdd_dll_path") or ""),
        "pdd_injector_path": str(cfg.get("pdd_injector_path") or ""),
        "recv_via_dll": bool(cfg.get("recv_via_dll", False)),
        "recv_slot": int(cfg.get("recv_slot") or -1),
        "recv_mode": int(cfg.get("recv_mode") or 0),
        # 中心 WS 上行通道。默认关：灰度按机器打开，连不上自动回落 HTTP。
        "center_ws_enabled": as_bool(cfg.get("center_ws_enabled"), False),
        "center_ws_url": str(cfg.get("center_ws_url") or ""),
        "center_ws_path": str(cfg.get("center_ws_path") or "/api/bridge/v1/ws"),
        "center_ws_ack_timeout_seconds": float(
            cfg.get("center_ws_ack_timeout_seconds") or 10.0),
    }
    # 以"盘上现有配置"为基底再叠加，而不是只写白名单。
    # 旧实现只序列化上面的 payload，任何没列进去的键都会在点一次「保存配置」后
    # 被静默删除 —— 实测会丢 history_pull_seconds、self_send_match_seconds、
    # upload_batch_size、config_version 等十几个；其中 config_version 被删还会让
    # 下次启动重跑迁移，强制覆盖用户自己选的行为参数。
    merged: dict[str, Any] = {}
    try:
        if cfg_path.is_file():
            existing = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
            if isinstance(existing, dict):
                merged.update(existing)
    except (OSError, ValueError):
        pass
    merged.update(_persistable_config(cfg))
    merged.update(payload)          # 已知键仍走这里的规范化与默认值
    merged["config_version"] = CONFIG_VERSION
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
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
        "platform_version": "9.77.01N" if plat.name == "taobao" else "3.5.0.40",
        "poll_interval_ms": 200,
        "heartbeat_seconds": 5,
        "command_poll_seconds": 1.5,
        "upload_batch_size": 500,
        "upload_concurrency": 6,
        "upload_drain_budget_seconds": 3.0,
        # 灰度（默认全关）：白名单命中的店铺才走即时上传 / 并发发送池。
        "immediate_ingress_shop_ids": [],
        "command_sender_shop_ids": [],
        "command_sender_workers": 6,
        "outgoing_dedup_seconds": 60.0,
        "history_pull_seconds": 0,
        "history_pull_size": 20,
        "history_pull_max_buyers": 40,
        "history_pull_gap_seconds": 0.3,
        "history_report_max_age_seconds": 1800,
        "history_report_user_only": True,
        "send_confirm_timeout_seconds": 10.0,
        "local_first_max_wait_seconds": 0.3,
        # 中心 WS 上行：默认关，灰度打开；连不上自动回落 HTTP。
        "center_ws_enabled": False,
        "center_ws_url": "",
        "center_ws_path": "/api/bridge/v1/ws",
        "center_ws_ack_timeout_seconds": 10.0,
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
        "cdp_eval_timeout": 4.0,
        "cdp_round_budget_seconds": 6.0,
        "cdp_session_fail_limit": 3,
        "history_only_idle_poll_seconds": 5.0,
        "pull_drain_window_seconds": 6.0,
        "dedup_window_seconds": 0,
        "cross_source_dedup_seconds": 0,
        "enforce_seat_scope": True,
        "seat_scope_refresh_seconds": 30.0,
        "allowed_seat_accounts": [],
        "silent_warn_seconds": 300.0,
    }
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    if not cfg_path.exists():
        cfg_path.write_text(json.dumps(example, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg_path
