# 目标文件: 本仓库（路径按部署位置而定，勿写死盘符）
# -*- coding: utf-8 -*-
"""主程序入口（提示词工程 + 大模型 API 版）。

提供命令行交互界面：
  1. 接收用户输入的文本。
  2. 调用 classic.judge(text)（内部并行调用情感/攻击性/逻辑三个推理函数）。
  3. 入口层挂载关注度（attention）字段——classic 不 import attention（依赖方向约束）。
  4. 打印最终报告。

用法：
    python -m mylocalai.cli --text "你输入的文本"          # 单条分析
    python -m mylocalai.cli                                # 交互式逐条输入
    python -m mylocalai.cli --text "..." --attention-online   # 联网估参算关注度
"""

from __future__ import annotations

import argparse

from mylocalai.classic.judge import judge as judge_text
from mylocalai.util.console_io import force_utf8_stdio


def analyze_once(text: str, attention_online: bool = False,
                 attention_profile: str | None = None) -> None:
    """分析单条文本并打印最终报告。"""
    output = judge_text(text)
    # 关注度挂载在入口层完成（2026-10-30 M1-2 装配倒转）；算不出也不影响主报告。
    try:
        from mylocalai.attention.attach import attention_for_text
        output["attention"] = attention_for_text(
            query=text, allow_network=attention_online, profile=attention_profile)
    except Exception:  # noqa: BLE001
        pass
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
    parser.add_argument(
        "--attention-online",
        action="store_true",
        help="联网检索话题热度以估算关注度参数（默认用基准值占位，不联网）",
    )
    parser.add_argument(
        "--attention-profile",
        default=None,
        help="关注度参数档位：spec（默认，用户指定原值）/ reachable（拉高可达性）",
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
        analyze_once(args.text, attention_online=args.attention_online,
                     attention_profile=args.attention_profile)
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
            analyze_once(line, attention_online=args.attention_online,
                         attention_profile=args.attention_profile)
    except (EOFError, KeyboardInterrupt):  # noqa: BLE001
        print("\n已退出。")


if __name__ == "__main__":
    main()