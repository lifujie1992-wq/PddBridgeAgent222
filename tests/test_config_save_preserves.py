# -*- coding: utf-8 -*-
"""save_config 不能丢键。

旧实现只序列化一份硬编码白名单，任何没列进去的键在点一次「保存配置」后就被
静默删除（实测丢过 history_pull_seconds / self_send_match_seconds /
upload_batch_size / config_version 等十几个）。config_version 被删还会让下次
启动重跑迁移、强制覆盖用户自己选的行为参数。
"""
import json

from bridge.config import CONFIG_VERSION, load_config, save_config


def _write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def test_save_keeps_user_edited_keys(tmp_path):
    path = tmp_path / "bridge_config.json"
    _write(path, {
        "platform": "pdd",
        "server_url": "http://127.0.0.1:18765",
        "agent_token": "tok",
        "agent_id": "agent-1",
        "config_version": CONFIG_VERSION,
        # 这些都不在白名单里，但都是真实行为参数，必须留住。
        "history_pull_seconds": 120,
        "history_pull_size": 10,
        "self_send_match_seconds": 180,
        "upload_batch_size": 500,
        "upload_concurrency": 3,
        "outgoing_dedup_seconds": 60,
        "local_first_max_wait_seconds": 1.0,
        "send_confirm_timeout_seconds": 10.0,
        # 用户自己加的键也不能删。
        "my_custom_knob": "keep-me",
    })
    save_config(load_config(path), path)
    after = json.loads(path.read_text(encoding="utf-8"))
    for key, value in (
        ("history_pull_seconds", 120),
        ("history_pull_size", 10),
        ("self_send_match_seconds", 180),
        ("upload_batch_size", 500),
        ("upload_concurrency", 3),
        ("outgoing_dedup_seconds", 60),
        ("local_first_max_wait_seconds", 1.0),
        ("send_confirm_timeout_seconds", 10.0),
        ("my_custom_knob", "keep-me"),
    ):
        assert after.get(key) == value, f"{key} 被保存弄丢了: {after.get(key)!r}"


def test_save_stamps_config_version(tmp_path):
    """写了版本号，迁移才不会再触发去覆盖用户的选择。"""
    path = tmp_path / "bridge_config.json"
    _write(path, {"platform": "pdd", "agent_token": "t", "agent_id": "a"})
    save_config(load_config(path), path)
    assert json.loads(path.read_text(encoding="utf-8"))["config_version"] == CONFIG_VERSION


def test_save_roundtrip_is_stable(tmp_path):
    """保存 -> 加载 -> 再保存，结果必须一致（不能每次都漂移）。"""
    path = tmp_path / "bridge_config.json"
    _write(path, {
        "platform": "pdd", "server_url": "http://c:18765",
        "agent_token": "t", "agent_id": "a",
        "data_source": "cdp", "cdp_poll_interval": 1.0,
        "center_ws_enabled": True,
    })
    save_config(load_config(path), path)
    first = json.loads(path.read_text(encoding="utf-8"))
    save_config(load_config(path), path)
    second = json.loads(path.read_text(encoding="utf-8"))
    assert first == second


def test_save_does_not_write_runtime_internals(tmp_path):
    """running-time 内部键（下划线开头 / config_path）不能落到文件里。"""
    path = tmp_path / "bridge_config.json"
    _write(path, {"platform": "pdd", "agent_token": "t", "agent_id": "a"})
    cfg = load_config(path)
    cfg["_pddbridge_source"] = object()      # 不可序列化的运行时对象
    cfg["_effective_source"] = "cdp"
    cfg["_buyer_nick"] = "张三"
    save_config(cfg, path)
    after = json.loads(path.read_text(encoding="utf-8"))
    assert not [k for k in after if k.startswith("_")], \
        f"内部键被写进配置: {[k for k in after if k.startswith('_')]}"
    assert "config_path" not in after


def test_ws_keys_survive_save(tmp_path):
    """WS 灰度开关必须能穿过保存（本轮新增的三个键）。"""
    path = tmp_path / "bridge_config.json"
    _write(path, {"platform": "pdd", "agent_token": "t", "agent_id": "a"})
    cfg = load_config(path)
    cfg["center_ws_enabled"] = True
    cfg["center_ws_path"] = "/custom/ws"
    cfg["center_ws_ack_timeout_seconds"] = 7.5
    save_config(cfg, path)
    reloaded = load_config(path)
    assert reloaded["center_ws_enabled"] is True
    assert reloaded["center_ws_path"] == "/custom/ws"
    assert reloaded["center_ws_ack_timeout_seconds"] == 7.5
