"""从 JSON 文件运行真实竞品 Agent 的简单命令行入口。"""

from __future__ import annotations

import argparse
from pathlib import Path

from .competitor import CompetitorAgent
from .llm import DeepSeekLLM
from .models import Event


def main() -> None:
    parser = argparse.ArgumentParser(description="运行 SignalX 竞品 Agent")
    parser.add_argument("input", type=Path, help="Event JSON 文件")
    parser.add_argument("--output", type=Path, help="可选的结果输出文件")
    args = parser.parse_args()

    event = Event.model_validate_json(args.input.read_text(encoding="utf-8"))
    analysis = CompetitorAgent(DeepSeekLLM()).analyze(event)
    output = analysis.model_dump_json(by_alias=True, indent=2)

    if args.output:
        args.output.write_text(output + "\n", encoding="utf-8")
    else:
        print(output)


if __name__ == "__main__":
    main()
