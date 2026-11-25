# -*- coding: utf-8 -*-
"""配置迁移/保存不得把用户显式设置改回去。

现场事故链（KO5MD2C）：
  1. 客户把 data_source 设成 cdp；
  2. 下次启动，v1 迁移发现"值不等于 v1 默认"，把它覆盖成 tanyu_logs；
  3. GUI 点一下保存，config_version 丢了，下次启动再迁移一次。
结果：谁改配置都白改，"脱离探域"推不动。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from bridge.config import CONFIG_VERSION, load_config, save_config


def _write(tmp: Path, data: dict) -> Path:
    path = tmp / "bridge_config.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_migration_keeps_explicit_data_source():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(Path(tmp), {"platform": "pdd", "data_source": "cdp"})  # 没有 config_version
        cfg = load_config(path)
        assert cfg["data_source"] == "cdp", "显式 cdp 被迁移覆盖成了 tanyu_logs"
        on_disk = json.loads(path.read_text(encoding="utf-8-sig"))
        assert on_disk["config_version"] == CONFIG_VERSION


def test_migration_fills_missing_keys_with_tanyu_logs():
    """缺失时才补默认；3.5.0.40 上 CDP 页面通道收不到消息，默认走插件日志。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(Path(tmp), {"platform": "pdd"})
        cfg = load_config(path)
        assert cfg["data_source"] == "tanyu_logs"


def test_save_config_preserves_config_version_and_unknown_fields():
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(Path(tmp), {
            "platform": "pdd", "data_source": "cdp", "config_version": CONFIG_VERSION,
            "some_future_key": "keep-me",
        })
        cfg = load_config(path)
        cfg["data_source"] = "cdp"
        save_config(cfg, path)
        on_disk = json.loads(path.read_text(encoding="utf-8-sig"))
        assert on_disk["config_version"] == CONFIG_VERSION, "保存后 config_version 丢了"
        assert on_disk.get("some_future_key") == "keep-me", "未知字段被丢掉"

        # 重新加载不得再触发迁移、也不得改回 tanyu_logs
        again = load_config(path)
        assert again["data_source"] == "cdp"


def test_gui_save_then_reload_keeps_cdp():
    """复现 GUI 那条路径：load → 改 → save → 再 load。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = _write(Path(tmp), {"platform": "pdd", "data_source": "cdp"})
        cfg = load_config(path)
        cfg["history_pull_seconds"] = 30
        save_config(cfg, path)
        again = load_config(path)
        assert again["data_source"] == "cdp"
        assert again["history_pull_seconds"] == 30.0
