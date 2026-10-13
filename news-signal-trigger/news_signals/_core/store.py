"""监测层本地持久化：告警 JSONL（追加）、状态 JSON（原子写）、观察箱 JSONL。

会话视图是内存态、重启即空——告警绝不能依赖它，这里全部落盘。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class MonitorStore:
    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.alerts_path = self.data_dir / "alerts.jsonl"
        self.observation_path = self.data_dir / "observation.jsonl"
        self.state_path = self.data_dir / "state.json"

    # ---- 告警 ----

    def append_alerts(self, alerts: list[dict[str, Any]]) -> None:
        if not alerts:
            return
        with self.alerts_path.open("a", encoding="utf-8") as handle:
            for alert in alerts:
                handle.write(json.dumps(alert, ensure_ascii=False) + "\n")

    def load_alerts(self) -> list[dict[str, Any]]:
        if not self.alerts_path.exists():
            return []
        alerts: list[dict[str, Any]] = []
        with self.alerts_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    alerts.append(json.loads(line))
                except ValueError:
                    continue  # 半行损坏不拖垮整个视图
        return alerts

    def pending_alerts(self, limit: int = 50) -> list[dict[str, Any]]:
        pending = [alert for alert in self.load_alerts() if alert.get("status") == "pending"]
        pending.reverse()  # 最新在前
        return pending[:limit]

    def update_alert_status(self, alert_id: str, status: str, note: str = "") -> bool:
        """全量重写告警文件更新状态（文件量级：一天几百条，可接受）。"""
        alerts = self.load_alerts()
        changed = False
        for alert in alerts:
            if alert.get("alertId") == alert_id:
                alert["status"] = status
                alert["statusNote"] = note
                changed = True
        if changed:
            self._rewrite_alerts(alerts)
        return changed

    def _rewrite_alerts(self, alerts: list[dict[str, Any]]) -> None:
        temp_path = self.alerts_path.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            for alert in alerts:
                handle.write(json.dumps(alert, ensure_ascii=False) + "\n")
        os.replace(temp_path, self.alerts_path)

    # ---- 观察箱 ----

    def append_observation(self, entries: list[dict[str, Any]]) -> None:
        if not entries:
            return
        with self.observation_path.open("a", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def load_observation(self, limit: int = 200) -> list[dict[str, Any]]:
        if not self.observation_path.exists():
            return []
        entries: list[dict[str, Any]] = []
        with self.observation_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except ValueError:
                        continue
        return entries[-limit:]

    # ---- 引擎状态 ----

    def load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except ValueError:
            return {}  # 状态损坏按冷启动处理，告警文件不受影响

    def save_state(self, state: dict[str, Any]) -> None:
        temp_path = self.state_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temp_path, self.state_path)

    @staticmethod
    def prune_state(state: dict[str, Any], retention_days: float, now: datetime) -> dict[str, Any]:
        """按保留期清理状态，防止无限增长。"""
        cutoff = now - timedelta(days=retention_days)
        cutoff_iso = cutoff.isoformat()

        def _iso(value: Any) -> str:
            return str(value or "")

        items = {
            key: entry for key, entry in (state.get("items") or {}).items()
            if _iso(entry.get("lastSeenAt")) >= cutoff_iso or _iso(entry.get("alertedAt")) >= cutoff_iso
        }
        groups = {
            key: entry for key, entry in (state.get("groups") or {}).items()
            if _iso(entry.get("lastAlertAt")) >= cutoff_iso or _iso(entry.get("lastSeenAt")) >= cutoff_iso
        }
        heat = state.get("heat") or {}
        pruned = dict(state)
        pruned["items"] = items
        pruned["groups"] = groups
        pruned["heat"] = {
            platform: values[-500:] for platform, values in heat.items() if isinstance(values, list)
        }
        return pruned
