# -*- coding: utf-8 -*-
"""被动提醒（工作指导 §2.2 / §6 M3-7）：档案年龄 / 快照变化 → 一行提示。

设计口径（§2.2"不制造新压力"铁律）：
  - **被动**：无推送、无通知、无定时打扰。提醒只在用户打开会话时由 web 层取一次，
    在消息流顶部以一行浅色文案呈现，不弹窗、不打断。
  - **纯函数**：本模块不碰网络、不碰 LLM、不写库；输入 (dossier_id, store, now)，
    输出提醒行列表。永不抛异常——提醒是附加信息，任何坏输入都静默退空。
  - **规则外置**：阈值读 assistant_rules.json 的 reminders 段
    （dossier_age_days=14 / snapshot_change_ratio=0.5）；文案读 prompts.json 的
    reminder_line 键。配置缺失/写坏一律退内置默认并继续工作（不崩、不空转）。

两条规则（M3-7）：
  1. 档案 updated_at 距今 > dossier_age_days 天 → 一行"已 N 天未复核"提示
     （文案用 prompts.reminder_line 既有键）；
  2. 同档案快照序列最新两分值变化幅度 > snapshot_change_ratio → 一行"热度变化明显"。
     幅度定义：|E_new − E_old| / max(|E_old|, |E_new|)（对称相对变化，两值同为 0 视为无变化；
     该定义不需要 trend 模块在场，避免读路径之间的隐式依赖）。

依赖方向：assistant → stdlib（assistant 不许 import web/dossier，见 test_import_rules）。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

__all__ = ["reminder_lines", "aggregate_reminder_lines", "load_reminder_rules",
           "DEFAULT_RULES", "DEFAULT_REMINDER_LINE", "DEFAULT_HEAT_CHANGE_LINE"]

#: assistant_rules.json 缺失/写坏时的内置默认（与现行配置逐字一致）
DEFAULT_RULES: Dict[str, Any] = {
    "dossier_age_days": 14,
    "snapshot_change_ratio": 0.5,
}

#: prompts.json 缺失/写坏时的内置默认（与 prompts.json 的 reminder_line 逐字一致）
DEFAULT_REMINDER_LINE = "提醒：档案 #{dossier_id}《{title}》已 {days} 天未复核，其间证据可能更新。"

#: "热度变化明显"提示文案（本次新增，无既有键；占位符与 reminder_line 同风格）
DEFAULT_HEAT_CHANGE_LINE = ("提醒：档案 #{dossier_id}《{title}》热度变化明显"
                            "（E {old} → {new}），证据面可能已有新进展。")

#: 聚合上限：§2.2"提醒只在用户打开会话时以一行呈现"——多档案最多合并 3 条，不刷屏
MAX_AGGREGATED_LINES = 3


def _pkg_json(name: str) -> Dict[str, Any]:
    """读包内 JSON 配置（与 assistant/loop._pkg_json 同一容错口径）：坏文件退空 dict。"""
    path = os.path.join(os.path.dirname(__file__), name)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001  配置写坏退空 dict（各键取代码内默认）
        return {}


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _normalize_rules(raw: Any) -> Dict[str, Any]:
    """reminders 段 → 合法阈值；缺键/类型坏/非法值逐键退默认，永不抛。"""
    rules = dict(DEFAULT_RULES)
    if not isinstance(raw, dict):
        return rules
    age = raw.get("dossier_age_days", DEFAULT_RULES["dossier_age_days"])
    if _is_num(age) and age >= 0:
        rules["dossier_age_days"] = float(age)
    ratio = raw.get("snapshot_change_ratio", DEFAULT_RULES["snapshot_change_ratio"])
    if _is_num(ratio) and ratio >= 0:
        rules["snapshot_change_ratio"] = float(ratio)
    return rules


def load_reminder_rules() -> Dict[str, Any]:
    """读 assistant_rules.json 的 reminders 段（写坏/缺失退默认）。**永不抛。**"""
    try:
        return _normalize_rules(_pkg_json("assistant_rules.json").get("reminders"))
    except Exception:  # noqa: BLE001
        return dict(DEFAULT_RULES)


def load_reminder_prompt() -> str:
    """读 prompts.json 的 reminder_line 文案键（写坏/缺失退内置默认）。**永不抛。**"""
    try:
        value = _pkg_json("prompts.json").get("reminder_line")
        if isinstance(value, str) and value.strip():
            return value
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_REMINDER_LINE


def _format_line(template: str, fallback: str, **kwargs: Any) -> str:
    """模板填空：模板坏（占位符不匹配等）退 fallback，再坏退最简一句。"""
    for tpl in (template, fallback):
        try:
            text = tpl.format(**kwargs)
            if text.strip():
                return text
        except Exception:  # noqa: BLE001  KeyError/IndexError/ValueError 都可能
            continue
    return f"提醒：档案 #{kwargs.get('dossier_id', '?')} 请复核。"


def _parse_iso(value: Any) -> Optional[datetime]:
    """ISO 时间字符串 → datetime（解析不了退 None，不抛）。"""
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed


def _relative_change(old: float, new: float) -> float:
    """对称相对变化幅度：|new−old| / max(|old|,|new|)；两值同为 0 → 0。"""
    base = max(abs(old), abs(new))
    if base <= 1e-12:
        return 0.0
    return abs(new - old) / base


def reminder_lines(dossier_id: int, store: Any, now: Optional[datetime] = None,
                   rules: Optional[Dict[str, Any]] = None,
                   prompt: Optional[str] = None) -> List[str]:
    """单档案的被动提醒行（**纯函数**，永不抛；无命中 → []）。

    参数:
        dossier_id: 档案号（非法/不存在 → []，档案页不会因为坏 id 多出一行）。
        store:      DossierStore（只调用 get_dossier / snapshots 两个只读方法）。
        now:        "今天"（可注入以便测试）；None 取当前本地时间。
        rules:      已规整的规则 dict（可注入）；None 时读 assistant_rules.json，
                    写坏退 DEFAULT_RULES。
        prompt:     年龄提示文案模板（可注入）；None 时读 prompts.json 的
                    reminder_line 键，写坏退 DEFAULT_REMINDER_LINE。

    返回:
        [年龄提示行?, 热度变化行?]（各自命中才出现；顺序固定，便于快照断言）。
    """
    try:
        did = int(dossier_id)
        if did <= 0 or store is None:
            return []
        now_dt = now if isinstance(now, datetime) else datetime.now().astimezone()
        cfg = _normalize_rules(rules if rules is not None else load_reminder_rules())
        header = store.get_dossier(did)
        if not isinstance(header, dict) or not header:
            return []
        title = str(header.get("title") or "")

        lines: List[str] = []
        # ---- 规则 1：档案年龄（updated_at 距今 > dossier_age_days 天） ----
        updated = _parse_iso(header.get("updated_at"))
        if updated is not None:
            if updated.tzinfo is None and now_dt.tzinfo is not None:
                updated = updated.replace(tzinfo=now_dt.tzinfo)   # 天真时间按本地时区算
            elapsed_days = (now_dt - updated).total_seconds() / 86400.0
            if elapsed_days > float(cfg.get("dossier_age_days", 14)):
                lines.append(_format_line(
                    prompt if isinstance(prompt, str) else load_reminder_prompt(),
                    DEFAULT_REMINDER_LINE,
                    dossier_id=did, title=title, days=int(elapsed_days)))

        # ---- 规则 2：快照序列最新两分值变化幅度 > snapshot_change_ratio ----
        try:
            snaps = store.snapshots(did) or []
        except Exception:  # noqa: BLE001  快照读不到就只按年龄规则提醒
            snaps = []
        if len(snaps) >= 2:
            try:
                old = float(snaps[-2].get("e_score", 0.0) or 0.0)
                new = float(snaps[-1].get("e_score", 0.0) or 0.0)
            except (TypeError, ValueError):
                old = new = 0.0
            change = _relative_change(old, new)
            if change > float(cfg.get("snapshot_change_ratio", 0.5)):
                lines.append(_format_line(
                    DEFAULT_HEAT_CHANGE_LINE, DEFAULT_HEAT_CHANGE_LINE,
                    dossier_id=did, title=title, old=f"{old:g}", new=f"{new:g}"))
        return lines
    except Exception:  # noqa: BLE001  提醒永不拖垮调用方
        return []


def aggregate_reminder_lines(dossier_ids: Sequence[int], store: Any,
                             now: Optional[datetime] = None) -> List[str]:
    """多档案聚合提醒：逐档案取行、按出现顺序去重、最多 MAX_AGGREGATED_LINES 条。

    web 层 GET /api/sessions/{sid}/reminders 用它把"该会话涉及的档案"收敛成
    消息流顶部的一小段浅色提示（§2.2：一行呈现，不弹窗不打断）。永不抛。
    """
    try:
        ids = [did for did in (dossier_ids or [])]
    except Exception:  # noqa: BLE001
        return []
    lines: List[str] = []
    seen: set = set()
    for did in ids:
        for line in reminder_lines(did, store, now=now):
            if line in seen:
                continue
            seen.add(line)
            lines.append(line)
            if len(lines) >= MAX_AGGREGATED_LINES:
                return lines
    return lines
