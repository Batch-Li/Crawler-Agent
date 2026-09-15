"""agent 收尾触发浏览器自动化补采（discover --trigger-capture，默认关）。

异步定位：agent 不实时驱使浏览器，只在运行结束后**同步拉起**两个栅栏脚本——
小红书消费捕获队列、头条补全正文。真正的限速全部由脚本内置
（单次 ≤5 / 间隔 ≥60s / 每日 ≤10 / 同词 ≤2 / 即停自锁），本模块只负责：
定位脚本目录、传 base_url、超时保护、一个平台撞墙即停止后续触发。

边界：不重试失败脚本；超时等同撞墙处理；脚本以 crawler_tool 为工作目录
（profile 与日志均为相对路径，落在 gitignore 的 data/ 下）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BLOCKED_EXIT = 2
DEFAULT_TIMEOUT_SECONDS = 1800

SCRIPT_SPECS = (
    ("xiaohongshu", "xhs_browser_capture.py", ("--from-queue",)),
    ("toutiao", "toutiao_browser_capture.py", ("--from-store",)),
)


def crawler_tool_root() -> Path:
    import crawler_tool  # agent 环境必然以 -e ../crawler_tool 安装

    return Path(crawler_tool.__file__).resolve().parent.parent


def default_runner(cmd: list[str], cwd: Path, timeout_seconds: int) -> int:
    proc = subprocess.run(cmd, cwd=str(cwd), timeout=timeout_seconds)
    return proc.returncode


def trigger_browser_capture(
    base_url: str = "http://127.0.0.1:8301",
    *,
    runner=default_runner,
    root: Path | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> list[dict[str, object]]:
    """顺序触发补采脚本，返回逐平台结果；撞墙（exit=2）即停止后续触发。"""
    root = root or crawler_tool_root()
    summary: list[dict[str, object]] = []
    for name, script_name, script_args in SCRIPT_SPECS:
        script = root / "scripts" / "browser_auto" / script_name
        if not script.exists():
            summary.append({"platform": name, "exit": "script_missing"})
            continue
        cmd = [sys.executable, str(script), *script_args, "--base-url", base_url]
        try:
            exit_code = runner(cmd, root, timeout_seconds)
        except subprocess.TimeoutExpired:
            summary.append({"platform": name, "exit": "timeout"})
            summary.append({"platform": "chain", "note": "超时视同撞墙，停止后续触发"})
            break
        summary.append({"platform": name, "exit": exit_code})
        if exit_code == BLOCKED_EXIT:
            summary.append({"platform": "chain", "note": "撞墙即停，当日不再触发后续平台"})
            break
    return summary
