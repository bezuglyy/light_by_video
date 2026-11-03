#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make-faq.py — собирает FAQ.xlsx из docs/FAQ.md (один источник — markdown).

Структура итога:
  Лист «FAQ»     — № · Вопрос · Ответ
  Лист «Разделы» — раздел · количество вопросов

  python3 tools/make-faq.py
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
MD = os.path.join(PROJ, "docs", "FAQ.md")
XLSX = os.path.join(PROJ, "docs", "FAQ.xlsx")


def parse(md_path):
    items = []
    section = "Общее"
    cur = None
    for line in open(md_path, encoding="utf-8"):
        t = line.rstrip("\n")
        if t.startswith("## "):
            section = t[3:].strip()
            continue
        m = re.match(r"\*\*(\d+)\.\s*(.+?)\*\*\s*$", t.strip())
        if m:
            if cur:
                items.append(cur)
            cur = {
                "n": int(m.group(1)),
                "q": m.group(2).strip(),
                "a": [],
                "section": section,
            }
            continue
        if cur is not None:
            if t.strip() == "":
                items.append(cur)
                cur = None
            else:
                cur["a"].append(t.strip())
    if cur:
        items.append(cur)
    for it in items:
        it["a"] = " ".join(it["a"]).strip()
    return items


def main():
    if not os.path.isfile(MD):
        print("нет файла:", MD)
        return 2
    items = parse(MD)
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError:
        print("нужен openpyxl")
        return 2

    wb = Workbook()
    ws = wb.active
    ws.title = "FAQ"
    head = ["№", "Вопрос", "Ответ"]
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="0E7490")
        c.alignment = Alignment(vertical="center")
    for it in items:
        ws.append([it["n"], it["q"], it["a"]])
    ws.column_dimensions["A"].width = 5
    ws.column_dimensions["B"].width = 48
    ws.column_dimensions["C"].width = 110
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(vertical="top", wrap_text=True)
    ws.freeze_panes = "A2"

    ws2 = wb.create_sheet("Разделы")
    ws2.append(["Раздел", "Вопросов"])
    for c in ws2[1]:
        c.font = Font(bold=True)
    counts = {}
    for it in items:
        counts[it["section"]] = counts.get(it["section"], 0) + 1
    for k, v in counts.items():
        ws2.append([k, v])
    ws2.column_dimensions["A"].width = 40
    ws2.column_dimensions["B"].width = 12

    wb.save(XLSX)
    print("FAQ.xlsx: %d вопросов, %d разделов → %s" % (len(items), len(counts), XLSX))
    return 0


if __name__ == "__main__":
    sys.exit(main())
