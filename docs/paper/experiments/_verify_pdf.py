# -*- coding: utf-8 -*-
"""PDF 文本层核验：乱码、缺字、关键段落与引用完整性。

前置：pdftotext -enc UTF-8 main.pdf main.txt 已生成 main.txt。
用法：python experiments/_verify_pdf.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PAPER = Path(__file__).resolve().parents[1]
TXT = PAPER / "main.txt"
TEX = PAPER / "main.tex"

CHECKS = [
    "判定与生成分离",
    "谣言识别",
    "关注度",
    "护栏",
    "单飞",
    "55.03/50.93/48.74/45.86/38.19",
    "当作“让头部话题过线”",
    "2 611",
    "参考文献",
]


def main() -> int:
    text = TXT.read_text(encoding="utf-8", errors="replace")
    flat = re.sub(r"\s+", "", text)

    print(f"chars={len(text)} cjk={len(re.findall(r'[一-鿿]', text))}")
    print(f"replacement_chars={text.count(chr(0xFFFD))}")
    print(f"mojibake_hits={len(re.findall(r'[ÃÂ][\u0080-\u00bf]|â€|ï¼', text))}")

    missing = []
    for token in CHECKS:
        ok = re.sub(r"\s+", "", token) in flat
        print(f"  {'OK ' if ok else 'MISS'} {token}")
        if not ok:
            missing.append(token)

    tex = TEX.read_text(encoding="utf-8")
    # 把 \input 的片段一并纳入标签/引用统计，否则会误判 tab:api 之类为未定义。
    for inc in re.findall(r"\\input\{([^}]+)\}", tex):
        inc_path = PAPER / inc
        if not inc_path.suffix:
            inc_path = inc_path.with_suffix(".tex")
        if inc_path.exists():
            tex += "\n" + inc_path.read_text(encoding="utf-8")
    cites = set(re.findall(r"\\cite\{([^}]+)\}", tex))
    cited = {k.strip() for group in cites for k in group.split(",")}
    bib = set(re.findall(r"\\bibitem\{([^}]+)\}", tex))
    print(f"cite_keys={len(cited)} bibitems={len(bib)}")
    print(f"cited_not_in_bib={sorted(cited - bib)}")
    print(f"bib_never_cited={sorted(bib - cited)}")

    refs = set(re.findall(r"\\ref\{([^}]+)\}", tex))
    labels = set(re.findall(r"\\label\{([^}]+)\}", tex))
    print(f"undefined_refs={sorted(refs - labels)}")
    print(f"unused_labels={sorted(labels - refs)}")
    print(f"question_marks_in_pdf={text.count('??')}")

    bad = len(missing) or text.count(chr(0xFFFD)) or (cited - bib) or (refs - labels)
    print("RESULT:", "FAIL" if bad else "PASS")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
