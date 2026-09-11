"""可选的慢车道 LLM client 实现（不随包强依赖任何 SDK）。

约定：
- 键只从运行环境读取（MONITOR_LLM_API_KEY），绝不硬编码、不写入任何文件；
- complete() 只负责把 prompt 原样送模型并返回文本；JSON 约束与校验由 triage 模块负责；
- 任何异常都允许抛出：engine 按批捕获并降级为纯规则模式，快车道不受影响。
"""

from __future__ import annotations

import os

import httpx


class DeepSeekTriage:
    """OpenAI 兼容 chat/completions 客户端（DeepSeek 及任何兼容网关均可）。"""

    available = True

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "deepseek-chat",
        base_url: str = "https://api.deepseek.com",
        timeout: float = 30.0,
    ) -> None:
        # 凭据零接触：只从环境变量或显式参数来，不落盘。
        self.api_key = api_key or os.environ.get("MONITOR_LLM_API_KEY", "")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def complete(self, prompt: str) -> str:
        if not self.api_key:
            raise RuntimeError("MONITOR_LLM_API_KEY 未设置；分诊按批降级为纯规则模式")
        response = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,  # 判定稳定性要求，见 MONITORING_DESIGN §3
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        return str(payload["choices"][0]["message"]["content"])
