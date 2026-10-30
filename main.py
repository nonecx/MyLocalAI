# 目标文件: 本仓库（路径按部署位置而定，勿写死盘符）
# -*- coding: utf-8 -*-
"""主程序入口（推理版）。

提供命令行交互界面：
  1. 接收用户输入的文本。
  2. 调用 core_judge.judge(text)（内部会依次调用情感/攻击性/逻辑三个推理函数）。
  3. 打印最终报告。

用法：
    python main.py --text "你输入的文本"   # 单条分析
    python main.py                          # 交互式逐条输入，输入 exit 退出
"""

from __future__ import annotations

import argparse

from core_judge import judge as judge_text
from shared_core.console_io import force_utf8_stdio


def analyze_once(text: str) -> None:
    """分析单条文本并打印最终报告。"""
    # judge() 内部会调用 report_generator.generate_report，返回 report 字段
    output = judge_text(text)
    report = output.get("report", "")
    print(report)
    print("\n" + "=" * 50)


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        description="本地人工智能分析系统（情感 / 攻击性 / 逻辑裁定）"
    )
    parser.add_argument(
        "--text",
        default=None,
        help="待分析文本；省略时进入交互式输入模式",
    )
    return parser


def main() -> None:
    """主流程：单条或交互式分析。"""
    # 非 UTF-8 控制台（英文 Windows 的 cp1252 等）下打印中文报告会抛
    # UnicodeEncodeError 并以退出码 1 结束（2026-10-22 检验报告发现 8）。
    force_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args()

    if args.text:
        # 单条分析
        analyze_once(args.text)
        return

    # 交互式模式
    print("已进入交互模式，请输入要分析的文本（输入 exit 退出）：")
    try:
        while True:
            line = input("> ").strip()
            if not line:
                continue
            if line.lower() in ("exit", "quit", "退出"):
                print("已退出。")
                break
            analyze_once(line)
    except (EOFError, KeyboardInterrupt):  # noqa: BLE001
        print("\n已退出。")


if __name__ == "__main__":
    main()