"""Build the Assignment 03 report PDF from report/report.md (Part G).

    python report/build_report.py            # -> report/24I-3071_Assignment03_Report.pdf

Converts the Markdown subset the report uses (headings, paragraphs, bullet lists, pipe
tables, bold, inline code, images) to HTML with a print stylesheet, prints it with headless
Chrome or Edge, and reports the page count, and on which page the appendix starts: the body
must fit in 8 pages. Standard library only, plus pypdf for the count if it is installed.
"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "report.md"
OUT_HTML = HERE / "build" / "report.html"
OUT_PDF = HERE / "24I-3071_Assignment03_Report.pdf"
BODY_PAGE_LIMIT = 8
BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "google-chrome", "chromium", "chromium-browser", "microsoft-edge",
]

CSS = """
@page { size: A4; margin: 15mm 15mm 16mm 15mm; }
body { font-family: 'Segoe UI', 'Helvetica Neue', Arial, sans-serif; font-size: 9.6pt;
       line-height: 1.33; color: #1a1a1a; }
h1 { font-size: 16pt; margin: 0 0 2pt; }
h1 + p { color: #555; margin-top: 0; }
h2 { font-size: 12.5pt; margin: 11pt 0 4pt; border-bottom: 1px solid #ccc; padding-bottom: 2pt; }
h3 { font-size: 10.6pt; margin: 9pt 0 3pt; }
p { margin: 3pt 0 5pt; }
ul { margin: 2pt 0 5pt 0; padding-left: 15pt; }
li { margin: 1pt 0; }
code { font-family: Consolas, 'Courier New', monospace; font-size: 8.4pt; background: #f2f2f2;
       padding: 0 2px; border-radius: 2px; }
table { border-collapse: collapse; width: 100%; margin: 4pt 0 7pt; font-size: 8.3pt;
        line-height: 1.25; page-break-inside: auto; }
tr { page-break-inside: avoid; }
th, td { border: 1px solid #c8c8c8; padding: 2.5pt 4pt; vertical-align: top; text-align: left; }
th { background: #eef1f5; }
td code, th code { font-size: 7.6pt; }
img { max-width: 100%; }
.figure img { max-width: 82%; display: block; margin: 4pt auto; }
.appendix { page-break-before: always; }
.appendix img { max-width: 100%; max-height: 118mm; display: block; margin: 3pt auto 10pt;
                border: 1px solid #ddd; }
.appendix p { page-break-after: avoid; }
"""


def inline(text: str) -> str:
    """Escape, then bold and inline code (code first, so its contents stay literal)."""
    parts = re.split(r"(`[^`]+`)", text)
    out = []
    for part in parts:
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            out.append(f"<code>{html.escape(part[1:-1])}</code>")
        else:
            s = html.escape(part, quote=False)
            s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
            out.append(s)
    return "".join(out)


def table(lines: list[str]) -> str:
    def cells(line: str) -> list[str]:
        return [c.strip() for c in line.strip().strip("|").split("|")]
    head, body = cells(lines[0]), [cells(l) for l in lines[2:]]
    h = "".join(f"<th>{inline(c)}</th>" for c in head)
    rows = "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in body)
    return f"<table><thead><tr>{h}</tr></thead><tbody>{rows}</tbody></table>"


def convert(md: str) -> str:
    out: list[str] = []
    lines = md.splitlines()
    i, in_appendix = 0, False
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            text = line[level:].strip()
            if level == 2 and text.lower().startswith("appendix"):
                in_appendix = True
                out.append("<section class='appendix'>")
            out.append(f"<h{level}>{inline(text)}</h{level}>")
            i += 1
            continue
        if line.startswith("|"):
            block = []
            while i < len(lines) and lines[i].startswith("|"):
                block.append(lines[i])
                i += 1
            out.append(table(block))
            continue
        if line.startswith("- "):
            items = []
            while i < len(lines) and lines[i].startswith("- "):
                items.append(f"<li>{inline(lines[i][2:])}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        m = re.match(r"!\[(.*?)\]\((.+?)\)", line.strip())
        if m:
            cls = "" if in_appendix else " class='figure'"
            out.append(f"<div{cls}><img src='../{html.escape(m.group(2))}' "
                       f"alt='{html.escape(m.group(1))}'></div>")
            i += 1
            continue
        para = []
        while i < len(lines) and lines[i].strip() and not re.match(r"(#|\||- |!\[)", lines[i]):
            para.append(lines[i].strip())
            i += 1
        out.append(f"<p>{inline(' '.join(para))}</p>")
    if in_appendix:
        out.append("</section>")
    return "\n".join(out)


def browser() -> str:
    for b in BROWSERS:
        if Path(b).exists() or shutil.which(b):
            return b
    raise SystemExit("no Chrome or Edge found")


def main() -> int:
    md = SOURCE.read_text(encoding="utf-8")
    title = md.splitlines()[0].lstrip("# ").strip()
    OUT_HTML.parent.mkdir(exist_ok=True)
    OUT_HTML.write_text(f"<!doctype html><html><head><meta charset='utf-8'><title>{title}"
                        f"</title><style>{CSS}</style></head><body>{convert(md)}</body></html>",
                        encoding="utf-8")
    subprocess.run([browser(), "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={OUT_PDF}", OUT_HTML.as_uri()],
                   check=True, capture_output=True)
    try:
        from pypdf import PdfReader
    except ImportError:
        print(f"wrote {OUT_PDF} (install pypdf to count pages)")
        return 0
    pages = PdfReader(str(OUT_PDF)).pages
    appendix = next((n for n, p in enumerate(pages, 1)
                     if "Appendix: screenshots" in (p.extract_text() or "")), len(pages) + 1)
    body = appendix - 1
    print(f"wrote {OUT_PDF}: {len(pages)} pages, body {body} (limit {BODY_PAGE_LIMIT}), "
          f"appendix from page {appendix}")
    return 0 if body <= BODY_PAGE_LIMIT else 1


if __name__ == "__main__":
    sys.exit(main())
