"""配置加载：包内默认配置 + 外部覆盖目录（四层文件机制的加载端）。

优先级：config_dir 下的 base_rules.json / entities.json 覆盖包内默认值；
整段替换而非深合并——配置文件必须经 Schema 校验（MonitorConfig），改坏拒绝启动。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from crawler_tool.monitoring.models import MonitorConfig

DEFAULT_CONFIG_DIR = Path(__file__).parent / "default_config"


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


def load_monitor_config(config_dir: str | Path | None = None) -> MonitorConfig:
    base = _read_json(DEFAULT_CONFIG_DIR / "base_rules.json") or {}

    entities_payload = _read_json(DEFAULT_CONFIG_DIR / "entities.example.json") or {"entities": []}

    if config_dir is not None:
        override_dir = Path(config_dir)
        if not override_dir.is_dir():
            # 目录写错时静默回退到示例主体太危险——直接拒绝启动。
            raise FileNotFoundError(f"monitoring config dir does not exist: {override_dir}")
        override_rules = _read_json(override_dir / "base_rules.json")
        if override_rules:
            base = {**base, **override_rules}
        override_entities = _read_json(override_dir / "entities.json")
        if override_entities is not None:
            entities_payload = override_entities

    merged: dict[str, Any] = dict(base)
    merged["entities"] = entities_payload.get("entities", [])
    return MonitorConfig(**merged)


def entities_source(config_dir: str | Path | None = None) -> str:
    """当前主体档案来自哪里：业务覆盖文件还是包内虚构示例（用于运行提示）。"""
    if config_dir is not None and (Path(config_dir) / "entities.json").exists():
        return "config_dir"
    return "packaged_example"
