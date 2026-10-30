# -*- coding: utf-8 -*-
"""命令行入口。

用法（在项目根目录）：
    python -m rumor_agent "听说自来水被下毒了，速转！"
    python -m rumor_agent --json "..."      输出完整结构化结果
    python -m rumor_agent                   进入交互模式，输入 exit 退出
"""

from __future__ import annotations

import argparse
import json
import sys

from .agent import RumorAgent
from .report import format_report


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="rumor_agent", description="基于 LangChain 的网络谣言识别 Agent")
    parser.add_argument("text", nargs="*", help="待识别文本；省略则进入交互模式")
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON 而非文字报告")
    args = parser.parse_args(argv)

    agent = RumorAgent()
    if agent.llm is None:
        print("提示：未检测到可用的大模型配置，将使用降级流水线模式（需要 .env 中的 LONGCAT_API_KEY）。", file=sys.stderr)

    def run(text: str) -> None:
        result = agent.analyze(text)
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else format_report(result))

    if args.text:
        run(" ".join(args.text))
        return 0

    print("谣言识别 Agent 已就绪，输入文本回车识别（exit 退出）：")
    try:
        while True:
            line = input("> ").strip()
            if not line:
                continue
            if line.lower() in ("exit", "quit", "退出"):
                break
            run(line)
            print("=" * 60)
    except (EOFError, KeyboardInterrupt):
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
