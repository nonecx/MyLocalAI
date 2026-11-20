# -*- coding: utf-8 -*-
"""材料上传解析 API（专业用户 A · 阶段 2）：文件只在内存解析，不落盘。

POST /api/materials/upload —— multipart 表单字段 file；支持 .txt/.md/.csv/.json，
大小上限 2MB。解析规则：
  - txt/md：按空行分段（一段 = 一条材料）；
  - json：字符串数组，或对象数组取 text/content/claim 字段；
  - csv：按列名取 text/content/claim，缺这些列则取每行第一个非空单元格。
返回 {"items": [...], "count": N, "filename": "..."}；失败 400 {"detail": ...}。

隐私口径：文件内容不写盘、不进档案库——用户随后点"开始分析"才经 POST /api/batch
走既有链路（原始输入保存开关此时才生效）。上传本身零外部请求。
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any, Dict, List

from fastapi import APIRouter, File, HTTPException, UploadFile

router = APIRouter()

MAX_UPLOAD_BYTES = 2 * 1024 * 1024      # 2MB
_ALLOWED_SUFFIXES = (".txt", ".md", ".csv", ".json")
_TEXT_FIELDS = ("text", "content", "claim")


def _split_paragraphs(text: str) -> List[str]:
    """按空行分段；单段超长不分（条目长度上限由 /api/batch 校验）。"""
    parts = [p.strip() for p in text.replace("\r\n", "\n").split("\n\n")]
    return [p for p in parts if p]


def _from_json(text: str) -> List[str]:
    data = json.loads(text)
    items: List[str] = []
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, str):
                items.append(entry.strip())
            elif isinstance(entry, dict):
                for field in _TEXT_FIELDS:
                    value = entry.get(field)
                    if isinstance(value, str) and value.strip():
                        items.append(value.strip())
                        break
    if not items:
        raise ValueError("JSON 里没有可取的材料（应为字符串数组或含 text/content/claim 字段的对象数组）")
    return items


def _from_csv(text: str) -> List[str]:
    """csv：有 text/content/claim 列取该列（首行表头）；否则每行第一个非空单元格
    （首行也是数据——DictReader 会吃掉首行，这里用 reader 自己判断）。"""
    rows = [r for r in csv.reader(io.StringIO(text))
            if any(c and str(c).strip() for c in r)]
    if not rows:
        raise ValueError("CSV 里没有可取的材料")
    header = [str(c).strip().lower() for c in rows[0]]
    picked = [i for i, name in enumerate(header) if name in _TEXT_FIELDS]
    if picked:
        column = picked[0]
        items = [str(rows[r][column]).strip() for r in range(1, len(rows))
                 if column < len(rows[r]) and str(rows[r][column]).strip()]
    else:
        items = []
        for row in rows:
            cell = next((str(c).strip() for c in row if str(c).strip()), "")
            if cell:
                items.append(cell)
    if not items:
        raise ValueError("CSV 里没有可取的材料（需要 text/content/claim 列或非空单元格）")
    return items


@router.post("/api/materials/upload")
async def materials_upload(file: UploadFile = File(...)) -> Dict[str, Any]:
    """解析上传的材料文件（不落盘）；扩展名/大小/解析失败 → 400。"""
    name = str(file.filename or "").strip()
    suffix = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if suffix not in _ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型：{suffix or '（无扩展名）'}（支持 txt/md/csv/json）")
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400,
                            detail=f"文件过大（最多 {MAX_UPLOAD_BYTES // 1024} KB）")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400,
                            detail="文件不是 UTF-8 编码，请转换后重试") from exc
    try:
        if suffix == ".json":
            items = _from_json(text)
        elif suffix == ".csv":
            items = _from_csv(text)
        else:
            items = _split_paragraphs(text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001  解析器的任何意外都按 400 用户错误回
        raise HTTPException(status_code=400,
                            detail=f"文件解析失败：{type(exc).__name__}: {exc}") from exc
    if not items:
        raise HTTPException(status_code=400, detail="文件里没有解析出任何材料")
    return {"items": items, "count": len(items), "filename": name[:100]}
