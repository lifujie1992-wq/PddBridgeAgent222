# -*- coding: utf-8 -*-
"""老配置自动升级（0.7.5.0）。

场景：同事机器上是老版本装的，bridge_config.json 里 data_source=cdp、
cdp_poll_interval=0.2 这些老值都还在。load_config 的 `data.get(k) or default`
只在键缺失时兜底，键存在就照用 —— 结果就是「装了新版还是老毛病」。
migrate_config 用 config_version 做一次性覆盖，同时保留身份/环境字段。
"""
import json

from bridge.config import CONFIG_VERSION, load_config


def _write(tmp_path, payload):
    path = tmp_path / "bridge_config.json"
    (tmp_path / "logs").mkdir(exist_ok=True)
    payload.setdefault("tanyu_log_dir", str(tmp_path / "logs"))
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_old_config_is_upgraded_but_identity_kept(tmp_path):
    path = _write(tmp_path, {
        "platform": "pdd",
        "server_url": "http://example.test:18765",
        "agent_token": "tok-abc",
        "agent_id": "agent-keepme",
        "agent_name": "拼多多-DESKTOP-XX",
        # 老版本的行为参数
        "data_source": "cdp",
        "cdp_poll_interval": 0.2,
        "poll_interval_ms": 400,
        "upload_concurrency": 1,
        "command_poll_seconds": 20,
    })

    cfg = load_config(path)

    # 身份 / 环境相关：一个都不能动
    assert cfg["agent_token"] == "tok-abc"
    assert cfg["agent_id"] == "agent-keepme"
    assert cfg["agent_name"] == "拼多多-DESKTOP-XX"
    assert cfg["server_url"] == "http://example.test:18765"
    assert cfg["tanyu_log_dir"] == str(tmp_path / "logs")
    # 行为参数：迁到实测调优值（v1 + v2 两段都要跑到）
    assert cfg["data_source"] == "tanyu_logs"
    assert cfg["cdp_poll_interval"] == 1.0
    assert cfg["poll_interval_ms"] == 200
    assert cfg["upload_concurrency"] == 6          # v2
    assert cfg["local_first_max_wait_seconds"] == 0.3   # v2，老配置里没这个键
    assert cfg["command_poll_seconds"] == 1.5
    # 落盘 + 留备份
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["config_version"] == CONFIG_VERSION
    assert saved["data_source"] == "tanyu_logs"
    assert saved["agent_token"] == "tok-abc"
    backup = tmp_path / f"bridge_config.json.pre-v{CONFIG_VERSION}.bak"
    assert backup.is_file()
    assert json.loads(backup.read_text(encoding="utf-8"))["data_source"] == "cdp"


def test_v1_config_only_runs_the_v2_step(tmp_path):
    """已经升过 v1 的机器，再升 v2 时**不能**把 v1 整份重放。

    v1 里有 data_source 这种用户会自己改的键。老实现是"只要 version < CONFIG_VERSION
    就重放 V1"，用户把 data_source 改成 cdp 之后装个新版又被冲回 tanyu_logs。
    """
    path = _write(tmp_path, {
        "platform": "pdd",
        "agent_token": "tok",
        "config_version": 1,
        "data_source": "cdp",               # 用户自己改的，必须保住
        "cdp_poll_interval": 0.5,           # 用户自己调的，必须保住
        "upload_concurrency": 3,            # v2 会覆盖这一项
    })

    cfg = load_config(path)

    assert cfg["data_source"] == "cdp"
    assert cfg["cdp_poll_interval"] == 0.5
    assert cfg["upload_concurrency"] == 6
    assert cfg["local_first_max_wait_seconds"] == 0.3


def test_config_already_migrated_is_left_alone(tmp_path):
    path = _write(tmp_path, {
        "platform": "pdd",
        "agent_token": "tok",
        "config_version": CONFIG_VERSION,
        "data_source": "cdp",          # 用户自己选的就该保留
        "poll_interval_ms": 350,
    })
    before = path.stat().st_mtime_ns

    cfg = load_config(path)

    assert cfg["data_source"] == "cdp"
    assert cfg["poll_interval_ms"] == 350
    assert path.stat().st_mtime_ns == before      # 没再写盘


def test_stale_paths_fall_back_next_to_config(tmp_path):
    missing = tmp_path / "old-install-dir"
    path = _write(tmp_path, {
        "platform": "pdd",
        "agent_token": "tok",
        "local_queue_path": str(missing / "bridge_queue_pdd.jsonl"),
        "command_journal_path": str(missing / "bridge_commands_pdd.json"),
    })

    cfg = load_config(path)

    assert cfg["local_queue_path"] == str(tmp_path / "bridge_queue_pdd.jsonl")
    assert cfg["command_journal_path"] == str(tmp_path / "bridge_commands_pdd.json")
