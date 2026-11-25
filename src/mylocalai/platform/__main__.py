# -*- coding: utf-8 -*-
"""平台命令行入口（platform/__main__.py）。

用法：
    python -m mylocalai.platform --once                 # 跑一次巡检（可挂 Windows 计划任务）
    python -m mylocalai.platform --once --query "话题A" --query "话题B"
    python -m mylocalai.platform --status               # 调度器状态 + 最近一次巡检摘要
    python -m mylocalai.platform --serve                # 前台调度循环（Ctrl-C 停）
    python -m mylocalai.platform --once --json          # 机器可读输出

退出码：0 = 巡检正常结束（completed/partial/cancelled/skipped 都是合法状态，
skipped 通常是未授权外部检索——按提示去设置里开授权）；1 = failed。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional

from mylocalai.platform.risk import LEVEL_LABELS
from mylocalai.util.console_io import force_utf8_stdio

#: 状态 → 中文口径（skipped 不是错误，是要去授权的提示）。
_STATUS_TEXT = {
    "completed": "完成", "partial": "部分完成（有降级）", "cancelled": "已取消",
    "skipped": "跳过（未授权外部检索/无监控词）", "failed": "失败",
}

#: 巡检报告 LLM 组织状态 → 中文口径（与 web/static/platform.js 的
#: LLM_STATUS_TEXT、platform/store.py REPORT_LLM_STATUSES 同封闭集；
#: 越界值如实显示原值，不猜一个看似正常的状态）。
_LLM_STATUS_TEXT = {
    "ok": "已生成", "disabled": "已关闭", "unavailable": "模型不可用",
    "budget_exhausted": "预算耗尽", "cancelled": "已取消",
    "skipped_empty": "本轮无新发现", "error": "生成异常",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mylocalai.platform",
        description="热门信息监测平台：自动发现 → 初筛 → 深查 → 传播风险分级")
    parser.add_argument("--once", action="store_true",
                        help="立即跑一次巡检并打印结果（默认行为）")
    parser.add_argument("--query", action="append", default=None,
                        help="指定查询词（可重复）；缺省用库内启用中的监控词")
    parser.add_argument("--status", action="store_true",
                        help="只看调度器状态与最近一次巡检摘要，不跑巡检")
    parser.add_argument("--serve", action="store_true",
                        help="前台运行调度循环（按规则间隔自动巡检，Ctrl-C 停止）")
    parser.add_argument("--interval", type=int, default=None,
                        help="覆盖巡检间隔（秒，下限 10）；仅 --serve 有效")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    return parser


def _entry_line(entry: Dict[str, Any]) -> str:
    level = LEVEL_LABELS.get(str(entry.get("risk_level") or ""), "?")
    e = entry.get("e_score")
    e_text = f"E={e:.1f}" if isinstance(e, (int, float)) else "E=?"
    verdict = entry.get("verdict_label") or "未深查"
    score = entry.get("risk_score")
    verdict_text = f"{verdict}({score:g})" if isinstance(score, (int, float)) else verdict
    title = str(entry.get("title") or "")[:40]
    return (f"  [{level}] {title}  {e_text}  {verdict_text}"
            f"  {entry.get('url') or ''}")


def _print_report(report: Dict[str, Any]) -> None:
    """巡检报告段（runner._build_report 挂进 summary 的 payload）。

    口径：叙述性部分由 LLM 组织（只做语言组织与逻辑判断，不判真假）；llm_status
    非 ok 时先如实说明本轮没有叙述性报告（中文原因同前端 platform.js），再打印
    代码兜底的标题/摘要——代码汇总（计数/分布）不受 LLM 状态影响，绝不为有输出
    而编。叙事四区（话题要点/分析/风险/跟进）只在 ok 时打印。
    """
    if not isinstance(report, dict):
        return
    llm_status = str(report.get("llm_status") or "")
    if llm_status != "ok":
        reason = _LLM_STATUS_TEXT.get(llm_status, llm_status or "状态未知")
        print(f"巡检报告：本轮未生成叙述性报告（{reason}），以下为代码汇总")
    else:
        print("巡检报告：")
    print(f"  {str(report.get('title') or '').strip() or '（无标题）'}")
    summary_text = str(report.get("summary") or "").strip()
    if summary_text:
        print(f"  {summary_text}")
    if llm_status != "ok":
        return                      # 叙事四区在非 ok 时本就是空列表，不摆空标题
    for note in report.get("topic_notes") or []:
        if not isinstance(note, dict):
            continue
        query = str(note.get("query") or "").strip()
        text = str(note.get("note") or "").strip()
        if query or text:
            print(f"  · {query or '（未命名监控词）'}：{text}")
    for item in (report.get("analysis") or [])[:3]:
        if not isinstance(item, dict):
            continue
        point = str(item.get("point") or "").strip()
        ids = [str(i) for i in (item.get("entry_ids") or [])]
        suffix = f"（条目 {', '.join(ids)}）" if ids else ""
        if point or suffix:
            print(f"  · {point}{suffix}")
    risks = str(report.get("risks") or "").strip()
    if risks:
        print(f"  风险提示：{risks}")
    followups = [str(f).strip() for f in (report.get("followups") or [])
                 if str(f or "").strip()]
    if followups:
        print("  后续跟进：")
        for text in followups:
            print(f"   - {text}")


def _print_summary(summary: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    status = str(summary.get("status") or "failed")
    counters = summary.get("counters") or {}
    print(f"巡检结果：{_STATUS_TEXT.get(status, status)}（{status}）")
    print(f"查询词 {counters.get('queries', 0)} 个 · 候选 {counters.get('candidates', 0)} 条"
          f" · 深查 {counters.get('verified', 0)} 条 · 去重 {counters.get('deduped', 0)} 条"
          f" · 降级 {counters.get('degraded', 0)} 次")
    if summary.get("scan_id"):
        print(f"scan_id: {summary['scan_id']}")
    if summary.get("report") is not None:
        _print_report(summary["report"])       # 无 report 键（早退路径）时跳过
    for entry in summary.get("entries") or []:
        print(_entry_line(entry))
    for warning in summary.get("warnings") or []:
        print(f"注意：{warning}")
    if summary.get("error"):
        print(f"原因：{summary['error']}")
    if status == "skipped":
        print("提示：外部检索默认关闭。在网页「设置 → 隐私与授权」启用"
              "「public-search」提供方授权后重试。")


def _print_status(status: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return
    print(f"调度器：{'运行中' if status.get('running') else '未运行'}"
          f" · 开关 {'开' if status.get('enabled') else '关'}"
          f" · 间隔 {status.get('interval_s')}s"
          f" · 扫描中 {'是' if status.get('scan_in_progress') else '否'}")
    last = status.get("last_scan") or {}
    if last:
        counters = last.get("counters") or {}
        print(f"最近巡检：{last.get('status')}（scan_id={last.get('scan_id')}）"
              f" 候选 {counters.get('candidates', 0)} · 深查 {counters.get('verified', 0)}")
        if last.get("error"):
            print(f"原因：{last['error']}")
    else:
        print("最近巡检：无记录")
    for warning in status.get("rule_warnings") or []:
        print(f"规则告警：{warning}")


def main(argv: Optional[List[str]] = None, *, runner: Any = None,
         scheduler: Any = None) -> int:
    """CLI 主流程。runner/scheduler 可注入（测试换 stub）。返回退出码。"""
    force_utf8_stdio()
    args = build_parser().parse_args(argv)

    if args.status:
        if scheduler is None:
            from mylocalai.platform.scheduler import PlatformScheduler
            scheduler = PlatformScheduler(runner=runner)
        _print_status(scheduler.status(), args.json)
        return 0

    if args.serve:
        if scheduler is None:
            from mylocalai.platform.scheduler import PlatformScheduler
            scheduler = PlatformScheduler(runner=runner)
        if args.interval is not None:
            # --interval 只影响本次进程：包一层规则加载器覆盖间隔
            from mylocalai.platform import rules as rules_mod
            base_loader = scheduler._rules_loader

            def _loader():
                loaded, warnings = base_loader()
                loaded["scan_interval_s"] = args.interval
                return loaded, warnings
            scheduler._rules_loader = _loader
        if not scheduler.start():
            print("调度器未启动：platform_rules.json 的 enabled=false。"
                  "设为 true 或用 --once 跑单次。")
            return 0
        print("调度循环已启动（Ctrl-C 停止）……")
        try:
            while scheduler.status().get("running"):
                import time
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n收到中断，正在停止……")
        finally:
            scheduler.stop()
        return 0

    # 默认/--once：同步跑一次
    if runner is None:
        from mylocalai.platform.runner import PlatformRunner
        runner = PlatformRunner()
    summary = runner.scan(queries=args.query)
    _print_summary(summary, args.json)
    return 1 if summary.get("status") == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
