"""本地模型配置测试；只使用测试值，不调用模型。"""

import os
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch

from agent.llm import DeepSeekLLM


class LLMConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env_file = Path(self.directory.name) / ".env"
        self.env_patch = patch.dict(os.environ, {}, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.path_patch = patch("agent.llm._ENV_FILE", self.env_file)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)

    def test_local_file_quotes_comments_bom_and_no_environment_mutation(self):
        self.env_file.write_text(
            '\ufeff# 本地测试\nDEEPSEEK_API_KEY="test-only-key" # 注释\n'
            'DEEPSEEK_BASE_URL=https://file.example.test\n', encoding="utf-8",
        )
        client = DeepSeekLLM()
        self.assertEqual(client.api_key, "test-only-key")
        self.assertEqual(client.base_url, "https://file.example.test")
        self.assertNotIn("DEEPSEEK_API_KEY", os.environ)
        self.assertNotIn("DEEPSEEK_BASE_URL", os.environ)

    def test_explicit_arguments_override_environment_and_file(self):
        self.env_file.write_text(
            "DEEPSEEK_API_KEY=file-test-value\nDEEPSEEK_BASE_URL=https://file.example.test\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {
            "DEEPSEEK_API_KEY": "environment-test-value",
            "DEEPSEEK_BASE_URL": "https://env.example.test",
        }):
            client = DeepSeekLLM()
            self.assertEqual(client.api_key, "environment-test-value")
            self.assertEqual(client.base_url, "https://env.example.test")
            client = DeepSeekLLM(api_key="argument-test-value", base_url="https://arg.example.test")
            self.assertEqual(client.api_key, "argument-test-value")
            self.assertEqual(client.base_url, "https://arg.example.test")

    def test_missing_file_still_accepts_environment_configuration(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "environment-test-value"}):
            client = DeepSeekLLM()
        self.assertEqual(client.api_key, "environment-test-value")
        self.assertEqual(client.base_url, "https://api.deepseek.com")

    def test_missing_or_blank_key_fails_without_network(self):
        with self.assertRaisesRegex(ValueError, "DEEPSEEK_API_KEY"):
            DeepSeekLLM()
        for text in ("DEEPSEEK_API_KEY=\n", 'DEEPSEEK_API_KEY="   "\n'):
            with self.subTest(text=text):
                self.env_file.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "DEEPSEEK_API_KEY"):
                    DeepSeekLLM()

    def test_file_location_is_independent_of_working_directory(self):
        self.env_file.write_text("DEEPSEEK_API_KEY=project-test-value\n", encoding="utf-8")
        with tempfile.TemporaryDirectory() as other_directory:
            with chdir(other_directory):
                Path(".env").write_text("DEEPSEEK_API_KEY=wrong-directory-value\n", encoding="utf-8")
                self.assertEqual(DeepSeekLLM().api_key, "project-test-value")

    def test_key_is_not_interpolated(self):
        self.env_file.write_text("DEEPSEEK_API_KEY='literal-${UNSET_VARIABLE}'\n", encoding="utf-8")
        self.assertEqual(DeepSeekLLM().api_key, "literal-${UNSET_VARIABLE}")
