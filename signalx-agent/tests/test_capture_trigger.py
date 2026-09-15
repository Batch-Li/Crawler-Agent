"""capture_trigger：命令组装、撞墙停链、脚本缺失、超时处理。"""

import unittest
from pathlib import Path

from agent.capture_trigger import crawler_tool_root, trigger_browser_capture


class TriggerBrowserCaptureTests(unittest.TestCase):
    def test_runs_both_scripts_in_crawler_tool_dir(self):
        calls = []

        def runner(cmd, cwd, timeout_seconds):
            calls.append((cmd, cwd, timeout_seconds))
            return 0

        summary = trigger_browser_capture(runner=runner)
        self.assertEqual([entry["exit"] for entry in summary], [0, 0])
        self.assertEqual(len(calls), 2)
        first_cmd, first_cwd, timeout = calls[0]
        self.assertIn("xhs_browser_capture.py", " ".join(first_cmd))
        self.assertIn("--from-queue", first_cmd)
        self.assertIn("--base-url", first_cmd)
        self.assertEqual(first_cwd, crawler_tool_root())
        self.assertEqual(timeout, 1800)
        self.assertIn("toutiao_browser_capture.py", " ".join(calls[1][0]))
        self.assertIn("--from-store", calls[1][0])

    def test_blocked_exit_stops_chain(self):
        calls = []

        def runner(cmd, cwd, timeout_seconds):
            calls.append(cmd)
            return 2  # 第一个平台撞墙

        summary = trigger_browser_capture(runner=runner)
        self.assertEqual(len(calls), 1)  # 第二个平台不再触发
        self.assertTrue(any(entry.get("platform") == "chain" for entry in summary))

    def test_missing_script_recorded_and_chain_continues(self):
        calls = []

        def runner(cmd, cwd, timeout_seconds):
            calls.append(cmd)
            return 0

        summary = trigger_browser_capture(runner=runner, root=Path("Z:/nowhere"))
        self.assertEqual(summary[0], {"platform": "xiaohongshu", "exit": "script_missing"})
        self.assertEqual(summary[1], {"platform": "toutiao", "exit": "script_missing"})
        self.assertEqual(len(calls), 0)  # 脚本缺失不是撞墙，记录后继续但不调用

    def test_timeout_treated_as_blocked(self):
        import subprocess

        def runner(cmd, cwd, timeout_seconds):
            raise subprocess.TimeoutExpired(cmd, timeout_seconds)

        summary = trigger_browser_capture(runner=runner)
        self.assertTrue(any(entry.get("exit") == "timeout" for entry in summary))
        self.assertEqual(len(summary), 2)  # timeout + 停链说明，不再触发 toutiao


if __name__ == "__main__":
    unittest.main()
