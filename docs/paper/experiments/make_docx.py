# -*- coding: utf-8 -*-
"""把 docs/paper/main.tex 转成完整的中文 DOCX（python-docx）。

只处理本文用到的 LaTeX 子集：section/subsection、paragraph、itemize、table、
figure（用已渲染的 PNG）、align/equation、thebibliography、常用行内命令。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

ROOT = Path(__file__).resolve().parents[3]
PAPER = ROOT / "docs" / "paper"
FIG = PAPER / "figures" / "arch-1.png"
OUT = PAPER / "MyLocalAI-论文.docx"

MATH_MAP = {
    r"\ge": "≥", r"\le": "≤", r"\in": "∈", r"\land": "∧", r"\times": "×",
    r"\approx": "≈", r"\pm": "±", r"\cdot": "·", r"\ldots": "…", r"\dots": "…",
    r"\lambda": "λ", r"\varepsilon": "ε", r"\quad": "  ", r"\qquad": "    ",
    r"\exp": "exp", r"\ln": "ln", r"\min": "min", r"\max": "max", r"\log": "log",
    r"\leftarrow": "←", r"\rightarrow": "→", r"\leftrightarrow": "↔",
    r"\left": "", r"\right": "", r"\,": "", r"\;": "", r"\!": "", r"\ ": " ",
    r"\text": "", r"\mathrm": "", r"\operatorname": "",
}


def read_braced(text: str, start: int) -> tuple[str, int]:
    """text[start] == '{'，返回 (内容, 右括号之后的下标)。"""
    assert text[start] == "{"
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:i], i + 1
    return text[start + 1:], len(text)


def expand_macros(text: str, macros: dict[str, str]) -> str:
    for name in sorted(macros, key=len, reverse=True):
        value = macros[name]
        text = re.sub(r"\\" + name + r"\{\}", lambda _m, v=value: v, text)
        text = re.sub(r"\\" + name + r"(?![A-Za-z])", lambda _m, v=value: v, text)
    return text


def math_to_text(expr: str) -> str:
    expr = expr.strip()
    expr = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1)/(\2)", expr)
    for cmd, repl in sorted(MATH_MAP.items(), key=lambda kv: -len(kv[0])):
        expr = expr.replace(cmd, repl)
    expr = re.sub(r"\\text\{([^{}]*)\}", r"\1", expr)
    expr = re.sub(r"\\mathrm\{([^{}]*)\}", r"\1", expr)
    expr = re.sub(r"\^\{([^{}]*)\}", r"^\1", expr)
    expr = re.sub(r"_\{([^{}]*)\}", r"_\1", expr)
    expr = expr.replace("{", "").replace("}", "")
    expr = expr.replace("\\", "")
    expr = re.sub(r"\s+", " ", expr)
    return expr.strip()


def inline_segments(raw: str, macros: dict[str, str], cites: dict[str, int], refs: dict[str, int]):
    """返回 [(text, style)]，style ∈ normal/bold/mono/sup。"""
    raw = expand_macros(raw, macros)
    segments: list[tuple[str, str]] = []
    pattern = re.compile(r"\\textbf\{([^{}]*)\}|\\texttt\{([^{}]*)\}|\\cite\{([^{}]*)\}|\\ref\{([^{}]*)\}")
    pos = 0
    for match in pattern.finditer(raw):
        if match.start() > pos:
            segments.append((clean_text(raw[pos:match.start()], macros), "normal"))
        if match.group(1) is not None:
            segments.append((clean_text(match.group(1), macros), "bold"))
        elif match.group(2) is not None:
            segments.append((clean_text(match.group(2), macros), "mono"))
        elif match.group(3) is not None:
            keys = [k.strip() for k in match.group(3).split(",")]
            nums = [str(cites.get(k, "?")) for k in keys]
            segments.append(("[" + ",".join(nums) + "]", "sup"))
        else:
            key = match.group(4).strip()
            segments.append((str(refs.get(key, "?")), "normal"))
        pos = match.end()
    if pos < len(raw):
        segments.append((clean_text(raw[pos:], macros), "normal"))
    return [(t, s) for t, s in segments if t]


def clean_text(raw: str, macros: dict[str, str]) -> str:
    text = expand_macros(raw, macros)
    text = re.sub(r"\\vspace\{[^}]*\}|\\hspace\{[^}]*\}|\\zihao\{[^}]*\}", "", text)
    text = re.sub(r"\$([^$]*)\$", lambda m: math_to_text(m.group(1)), text)
    text = text.replace(r"\%", "%").replace(r"\_", "_").replace(r"\&", "&").replace(r"\#", "#")
    text = text.replace(r"\,", "").replace(r"\;", "").replace(r"\!", "")
    text = text.replace(r"\LaTeX{}", "LaTeX").replace(r"\LaTeX", "LaTeX")
    text = text.replace("~", " ").replace("---", "—").replace("--", "–")
    text = re.sub(r"\\begin\{[^}]*\}|\\end\{[^}]*\}", "", text)
    text = re.sub(r"\\[a-zA-Z]+\*?(\[[^\]]*\])?", "", text)
    text = text.replace("{", "").replace("}", "")
    text = text.replace("\\", "")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def set_run_font(run, name: str, east: str, size: float | None = None, bold: bool | None = None):
    run.font.name = name
    run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), east)
    if size is not None:
        run.font.size = Pt(size)
    if bold is not None:
        run.font.bold = bold
    run.font.color.rgb = RGBColor(0, 0, 0)


def add_runs(paragraph, segments, size=12, east="宋体"):
    for text, style in segments:
        run = paragraph.add_run(text)
        if style == "bold":
            set_run_font(run, "Times New Roman", east, size, True)
        elif style == "mono":
            set_run_font(run, "Consolas", east, size - 0.5, False)
        elif style == "sup":
            set_run_font(run, "Times New Roman", east, size - 1.5, False)
            run.font.superscript = True
        else:
            set_run_font(run, "Times New Roman", east, size, False)


def style_document(doc: Document):
    section = doc.sections[0]
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(2.54)
    section.bottom_margin = Cm(2.54)
    section.left_margin = Cm(2.54)
    section.right_margin = Cm(2.54)

    normal = doc.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(12)
    normal.element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), "宋体")
    pf = normal.paragraph_format
    pf.line_spacing = 1.5
    pf.space_before = Pt(0)
    pf.space_after = Pt(0)
    pf.first_line_indent = Pt(24)

    for name, size in (("Heading 1", 16), ("Heading 2", 14), ("Heading 3", 12)):
        style = doc.styles[name]
        style.font.name = "Times New Roman"
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), "黑体")
        style.paragraph_format.first_line_indent = Pt(0)
        style.paragraph_format.space_before = Pt(12 if name == "Heading 1" else 8)
        style.paragraph_format.space_after = Pt(6)
        style.paragraph_format.keep_with_next = True


def add_page_number(doc: Document):
    footer = doc.sections[0].footer
    paragraph = footer.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run()
    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = "PAGE"
    fld_end = OxmlElement("w:fldChar")
    fld_end.set(qn("w:fldCharType"), "end")
    run._r.append(fld_begin)
    run._r.append(instr)
    run._r.append(fld_end)


def parse_table(block: str, macros: dict[str, str], cites, refs):
    caption = ""
    match = re.search(r"\\caption\{(.*?)\}", block)
    if match:
        caption = clean_text(match.group(1), macros)
    body = block
    if "\\midrule" in body:
        head_part, body = body.split("\\midrule", 1)
    else:
        head_part = ""
    if "\\bottomrule" in body:
        body = body.split("\\bottomrule", 1)[0]
    if "\\toprule" in head_part:
        head_part = head_part.split("\\toprule", 1)[1]
    head_rows = [r for r in head_part.split("\\\\") if r.strip()]
    body_rows = [r for r in body.split("\\\\") if r.strip()]
    header = [clean_text(c, macros) for c in head_rows[0].split("&")] if head_rows else []
    rows = [[clean_text(c, macros) for c in row.split("&")] for row in body_rows]
    rows = [r for r in rows if any(c for c in r)]
    return caption, header, rows


def add_table(doc: Document, caption: str, header: list[str], rows: list[list[str]], widths=None):
    if caption:
        cap = doc.add_paragraph()
        cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
        cap.paragraph_format.first_line_indent = Pt(0)
        cap.paragraph_format.space_before = Pt(6)
        cap.paragraph_format.space_after = Pt(3)
        run = cap.add_run(caption)
        set_run_font(run, "Times New Roman", "宋体", 10.5, True)

    cols = max(len(header), max((len(r) for r in rows), default=0))
    table = doc.add_table(rows=0, cols=cols)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = True

    def fill(cells, values, bold=False):
        for idx, cell in enumerate(cells):
            text = values[idx] if idx < len(values) else ""
            cell.text = ""
            paragraph = cell.paragraphs[0]
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            paragraph.paragraph_format.first_line_indent = Pt(0)
            paragraph.paragraph_format.line_spacing = 1.0
            run = paragraph.add_run(text)
            set_run_font(run, "Times New Roman", "宋体", 9, bold)

    if header:
        fill(table.add_row().cells, header, True)
    for row in rows:
        fill(table.add_row().cells, row, False)
    doc.add_paragraph().paragraph_format.space_after = Pt(0)


def build():
    text = (PAPER / "main.tex").read_text(encoding="utf-8")

    for name in ("generated/numbers.tex", "generated/table_api.tex"):
        content = (PAPER / name).read_text(encoding="utf-8")
        text = text.replace(r"\input{" + name + "}", content)

    macros: dict[str, str] = {}
    for match in re.finditer(r"\\newcommand\{\\([A-Za-z]+)\}\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}", text):
        macros[match.group(1)] = match.group(2)
    macros["PaperDate"] = macros.get("PaperDate", "2026年11月25日")

    body = text.split(r"\begin{document}", 1)[1].split(r"\end{document}", 1)[0]

    cites: dict[str, int] = {}
    for idx, match in enumerate(re.finditer(r"\\bibitem\{([^}]+)\}", body), 1):
        cites[match.group(1)] = idx
    refs = {"tab:det": 1, "tab:api": 2, "tab:res": 3, "fig:arch": 1}

    title_raw = re.search(r"\\title\{(.*?)\}\n", text, re.S).group(1)
    title_raw = title_raw.replace(r"\zihao{2}", "").replace(r"\bfseries", "")
    title_lines = [clean_text(t, macros) for t in title_raw.split(r"\\") if clean_text(t, macros)]
    author_raw = re.search(r"\\author\{(.*?)\}\n", text, re.S).group(1)
    author_name = clean_text(author_raw.split(r"\thanks", 1)[0], macros)
    thanks = re.search(r"\\thanks\{(.*?)\}", author_raw)
    author_note = clean_text(thanks.group(1), macros) if thanks else ""
    date_text = clean_text(re.search(r"\\date\{(.*?)\}\n", text, re.S).group(1), macros)

    doc = Document()
    style_document(doc)
    add_page_number(doc)

    for line in title_lines:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.first_line_indent = Pt(0)
        p.paragraph_format.space_before = Pt(18 if line == title_lines[0] else 6)
        p.paragraph_format.space_after = Pt(6)
        run = p.add_run(line)
        set_run_font(run, "Times New Roman", "黑体", 22, True)

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.first_line_indent = Pt(0)
    p.paragraph_format.space_before = Pt(24)
    run = p.add_run(author_name)
    set_run_font(run, "Times New Roman", "宋体", 12, False)

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.first_line_indent = Pt(0)
    run = p.add_run(date_text)
    set_run_font(run, "Times New Roman", "宋体", 12, False)

    lines = body.splitlines()
    i = 0
    section_no = 0
    subsection_no = 0
    table_no = 0
    figure_no = 0
    first_abstract = True
    pending_paragraph: list[str] = []

    def flush_paragraph():
        nonlocal pending_paragraph
        if not pending_paragraph:
            return
        joined = "".join(pending_paragraph)
        pending_paragraph = []
        p = doc.add_paragraph()
        p.paragraph_format.first_line_indent = Pt(24)
        p.paragraph_format.space_after = Pt(3)
        add_runs(p, inline_segments(joined, macros, cites, refs), 12)

    while i < len(lines):
        line = lines[i].strip()
        if not line or line.startswith("%"):
            flush_paragraph()
            i += 1
            continue
        if line == r"\maketitle" or line.startswith(r"\vspace") or line == r"\noindent" \
                or line.startswith(r"\begin{sloppypar}") or line.startswith(r"\end{sloppypar}"):
            i += 1
            continue
        if line == r"\newpage":
            flush_paragraph()
            doc.add_page_break()
            i += 1
            continue
        if line.startswith(r"\begin{abstract}"):
            flush_paragraph()
            end = lines.index(r"\end{abstract}", i)
            block = "\n".join(lines[i + 1:end])
            if first_abstract:
                heading = doc.add_paragraph()
                heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
                heading.paragraph_format.first_line_indent = Pt(0)
                heading.paragraph_format.space_before = Pt(18)
                run = heading.add_run("摘  要")
                set_run_font(run, "Times New Roman", "黑体", 14, True)
                first_abstract = False
            else:
                heading = doc.add_paragraph()
                heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
                heading.paragraph_format.first_line_indent = Pt(0)
                heading.paragraph_format.space_before = Pt(0)
                run = heading.add_run("Abstract")
                set_run_font(run, "Times New Roman", "黑体", 14, True)
            for para in re.split(r"\n\s*\n", block):
                para = para.strip()
                if not para:
                    continue
                p = doc.add_paragraph()
                p.paragraph_format.first_line_indent = Pt(24)
                p.paragraph_format.space_after = Pt(3)
                add_runs(p, inline_segments(para, macros, cites, refs), 12)
            i = end + 1
            continue
        if line.startswith(r"\section{") or line.startswith(r"\section*{"):
            flush_paragraph()
            starred = line.startswith(r"\section*")
            content, _ = read_braced(line, line.index("{"))
            content = re.sub(r"\\label\{[^}]*\}", "", content)
            if starred:
                title = clean_text(content, macros)
            else:
                section_no += 1
                subsection_no = 0
                title = f"{section_no}  {clean_text(content, macros)}"
            p = doc.add_paragraph(style="Heading 1")
            p.paragraph_format.first_line_indent = Pt(0)
            run = p.add_run(title)
            set_run_font(run, "Times New Roman", "黑体", 16, True)
            i += 1
            continue
        if line.startswith(r"\subsection{"):
            flush_paragraph()
            content, _ = read_braced(line, line.index("{"))
            content = re.sub(r"\\label\{[^}]*\}", "", content)
            subsection_no += 1
            title = f"{section_no}.{subsection_no}  {clean_text(content, macros)}"
            p = doc.add_paragraph(style="Heading 2")
            p.paragraph_format.first_line_indent = Pt(0)
            run = p.add_run(title)
            set_run_font(run, "Times New Roman", "黑体", 14, True)
            i += 1
            continue
        if line.startswith(r"\paragraph{"):
            flush_paragraph()
            content, _ = read_braced(line, line.index("{"))
            p = doc.add_paragraph()
            p.paragraph_format.first_line_indent = Pt(0)
            p.paragraph_format.space_before = Pt(6)
            p.paragraph_format.space_after = Pt(0)
            run = p.add_run(clean_text(content, macros) + "  ")
            set_run_font(run, "Times New Roman", "黑体", 12, True)
            i += 1
            continue
        if line.startswith(r"\begin{itemize}"):
            flush_paragraph()
            end = lines.index(r"\end{itemize}", i)
            for item in lines[i + 1:end]:
                item = item.strip()
                if not item.startswith(r"\item"):
                    continue
                p = doc.add_paragraph(style="List Bullet")
                p.paragraph_format.first_line_indent = Pt(0)
                p.paragraph_format.left_indent = Cm(0.74)
                p.paragraph_format.space_after = Pt(2)
                add_runs(p, inline_segments(item[len(r"\item"):].strip(), macros, cites, refs), 12)
            i = end + 1
            continue
        if line.startswith(r"\begin{table}"):
            flush_paragraph()
            end = lines.index(r"\end{table}", i)
            block = "\n".join(lines[i:end + 1])
            caption, header, rows = parse_table(block, macros, cites, refs)
            table_no += 1
            add_table(doc, f"表 {table_no}  {caption}", header, rows)
            i = end + 1
            continue
        if line.startswith(r"\begin{figure}"):
            flush_paragraph()
            end = lines.index(r"\end{figure}", i)
            block = "\n".join(lines[i:end + 1])
            match = re.search(r"\\caption\{(.*?)\}", block)
            caption = clean_text(match.group(1), macros) if match else ""
            figure_no += 1
            caption = f"图 {figure_no}  {caption}"
            if FIG.exists():
                p = doc.add_paragraph()
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                p.paragraph_format.first_line_indent = Pt(0)
                p.paragraph_format.space_before = Pt(6)
                p.add_run().add_picture(str(FIG), width=Cm(10.5))
            cap = doc.add_paragraph()
            cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
            cap.paragraph_format.first_line_indent = Pt(0)
            cap.paragraph_format.space_after = Pt(6)
            run = cap.add_run(caption)
            set_run_font(run, "Times New Roman", "宋体", 10.5, False)
            i = end + 1
            continue
        if line.startswith(r"\begin{align}") or line.startswith(r"\begin{equation}"):
            flush_paragraph()
            env = "align" if "align" in line else "equation"
            end = lines.index(r"\end{" + env + "}", i)
            for raw in lines[i + 1:end]:
                raw = raw.strip()
                if not raw:
                    continue
                raw = re.sub(r"^&", "", raw).replace(r"\\", "")
                if "&&" in raw:
                    left, right = raw.split("&&", 1)
                    text = math_to_text(left.strip()) + "    " + math_to_text(right.strip())
                else:
                    text = math_to_text(raw)
                p = doc.add_paragraph()
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                p.paragraph_format.first_line_indent = Pt(0)
                p.paragraph_format.space_before = Pt(3)
                p.paragraph_format.space_after = Pt(3)
                run = p.add_run(text)
                set_run_font(run, "Cambria Math", "宋体", 11, False)
            i = end + 1
            continue
        if line.startswith(r"\begin{center}"):
            flush_paragraph()
            end = lines.index(r"\end{center}", i)
            block = " ".join(l.strip() for l in lines[i + 1:end])
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.first_line_indent = Pt(0)
            p.paragraph_format.space_before = Pt(3)
            p.paragraph_format.space_after = Pt(3)
            add_runs(p, inline_segments(block, macros, cites, refs), 11)
            i = end + 1
            continue
        if line.startswith(r"\begin{thebibliography}"):
            flush_paragraph()
            end = lines.index(r"\end{thebibliography}", i)
            p = doc.add_paragraph(style="Heading 1")
            p.paragraph_format.first_line_indent = Pt(0)
            run = p.add_run("参考文献")
            set_run_font(run, "Times New Roman", "黑体", 16, True)
            for idx, raw in enumerate(lines[i + 1:end], 1):
                raw = raw.strip()
                if not raw.startswith(r"\bibitem"):
                    continue
                content = re.sub(r"^\\bibitem\{[^}]+\}\s*", "", raw)
                p = doc.add_paragraph()
                p.paragraph_format.first_line_indent = Pt(0)
                p.paragraph_format.left_indent = Cm(0.74)
                p.paragraph_format.first_line_indent = Cm(-0.74)
                p.paragraph_format.space_after = Pt(2)
                p.paragraph_format.line_spacing = 1.15
                run = p.add_run(f"[{idx}]  ")
                set_run_font(run, "Times New Roman", "宋体", 10.5, False)
                add_runs(p, inline_segments(content, macros, cites, refs), 10.5)
            i = end + 1
            continue

        pending_paragraph.append(line)
        i += 1

    flush_paragraph()
    try:
        doc.save(OUT)
        target = OUT
    except PermissionError:
        # 原文件被 Word/WPS 占用时不留半成品，改写同目录的 .updated.docx 并显式提示。
        target = OUT.with_suffix(".updated.docx")
        doc.save(target)
        print(f"WARN: {OUT.name} 被其他程序占用，已改写到 {target.name}")
    print(f"saved {target}")

    check = Document(target)
    print("paragraphs", len(check.paragraphs), "tables", len(check.tables),
          "images", len(check.inline_shapes))
    heads = [p.text for p in check.paragraphs if p.style.name.startswith("Heading")][:12]
    print("headings", heads)


if __name__ == "__main__":
    build()
