"""结构化 LLM 接口及 DeepSeek 实现。"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from dotenv import dotenv_values
from pydantic import ValidationError


_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class ModelBudgetExceeded(RuntimeError):
    """本次运行的模型调用预算已经用完。"""


@dataclass
class ModelCallBudget:
    limit: int
    calls: int = 0


_budget: ContextVar[ModelCallBudget | None] = ContextVar("agent_model_budget", default=None)


@contextmanager
def model_call_budget(limit: int):
    """每次运行独立计数，退出时恢复调用方上下文。"""
    budget = ModelCallBudget(limit)
    token = _budget.set(budget)
    try:
        yield budget
    finally:
        _budget.reset(token)


def generate_json(llm: "StructuredLLM", system_prompt: str, user_prompt: str) -> dict[str, Any]:
    """所有内置 Agent/Tool 的模型调用入口；失败尝试也计入预算。"""
    budget = _budget.get()
    if budget is not None:
        if budget.calls >= budget.limit:
            raise ModelBudgetExceeded("已达到 maxLlmCalls，未发送额外模型请求")
        budget.calls += 1
    return llm.generate_json(system_prompt, user_prompt)


def validation_feedback(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        # ctx 可能含 ValueError 对象；不把输入正文回传到错误记录。
        return json.dumps(
            exc.errors(include_url=False, include_context=False, include_input=False),
            ensure_ascii=False,
        )
    return str(exc)


class StructuredLLM(Protocol):
    """CompetitorAgent 所依赖的最小模型接口。"""

    model_name: str

    def generate_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        """返回已经解析的 JSON 对象。"""


def parse_json_object(text: str) -> dict[str, Any]:
    """从模型文本中提取单个 JSON 对象。"""
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()

    try:
        result = json.loads(value)
    except json.JSONDecodeError:
        start = value.find("{")
        end = value.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("模型响应中没有合法 JSON 对象")
        try:
            result = json.loads(value[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"模型响应不是合法 JSON: {exc}") from exc

    if not isinstance(result, dict):
        raise ValueError("模型响应必须是 JSON object")
    return result


class DeepSeekLLM:
    """基于 OpenAI 兼容接口的 DeepSeek 客户端。"""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model_name: str = "deepseek-chat",
        timeout: float = 60.0,
        max_output_tokens: int = 8192,
    ) -> None:
        # 固定读取项目配置，不修改进程环境，也不展开密钥中的 ${...}。
        config = dotenv_values(_ENV_FILE, encoding="utf-8-sig", interpolate=False)
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY") or config.get("DEEPSEEK_API_KEY")
        self.base_url = (
            base_url or os.getenv("DEEPSEEK_BASE_URL")
            or config.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
        )
        self.model_name = model_name
        self.timeout = timeout
        # 合并/串联的提示词嵌入全文证据，模型输出长 JSON 时默认上限可能截断，
        # 显式抬高并检测 finish_reason，让截断成为可诊断错误而非静默解析失败。
        self.max_output_tokens = max_output_tokens

        if not self.api_key or not self.api_key.strip():
            raise ValueError("请在项目 .env 或环境变量中配置 DEEPSEEK_API_KEY")

    def generate_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError(
                "未安装 openai，请执行 pip install -r requirements-agent.txt"
            ) from exc

        client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=0,
        )
        response = client.chat.completions.create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
            max_tokens=self.max_output_tokens,
        )
        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise ValueError(
                f"模型输出在 {self.max_output_tokens} tokens 处被截断（finish_reason=length），"
                "JSON 不完整；请增大 max_output_tokens 或缩小输入。"
            )
        content = choice.message.content
        if not content:
            raise ValueError("模型返回了空响应")
        return parse_json_object(content)
