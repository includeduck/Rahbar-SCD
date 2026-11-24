"""Label the judge sheet by hand, one answer at a time (Assignment 03, Part B: at least 8).

    python -m eval.label_judge eval/results/part-e/suite-baseline-20261124T175449Z-judge-sheet.jsonl --by Wasay

Shows each answer with the question and the passages the model was given, and asks:
is EVERY fact in the answer stated in (or directly implied by) these passages? Not whether
the answer is true in the real world, and not whether it answers the whole question.
  y = supported    n = not supported    s = skip    b = back one    q = save and quit
Labels are appended to eval/judge_labels.jsonl. Answers already labelled are skipped, so you
can stop and come back. The judge's verdict is never shown.
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
from pathlib import Path

HERE = Path(__file__).resolve().parent
LABELS = HERE / "judge_labels.jsonl"


def norm(text: str) -> str:
    return " ".join((text or "").split()).lower()


def show(n: int, total: int, row: dict) -> None:
    wrap = textwrap.TextWrapper(width=96, initial_indent="    ", subsequent_indent="    ")
    print("\n" + "=" * 100)
    print(f"[{n}/{total}] {row['id']}  QUESTION: {row['question']}")
    print(f"ANSWER: {row['answer']}")
    for i, c in enumerate(row.get("context", []), 1):
        print(f"\n  passage {i}: {c.get('source')}, section {str(c.get('section'))[:60]}, "
              f"page {c.get('page')}")
        print(wrap.fill(" ".join(str(c.get("text", "")).split())))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("sheet", type=Path)
    ap.add_argument("--by", required=True, help="your name, stored with each label")
    ap.add_argument("--labels", type=Path, default=LABELS)
    args = ap.parse_args(argv)

    rows = [json.loads(line) for line in args.sheet.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    done = set()
    if args.labels.exists():
        for line in args.labels.read_text(encoding="utf-8").splitlines():
            if line.strip():
                lab = json.loads(line)
                done.add((lab["id"], norm(lab["answer"])))
    todo = [r for r in rows if (r["id"], norm(r["answer"])) not in done]
    print(f"{len(rows)} answers in the sheet, {len(rows) - len(todo)} already labelled, "
          f"{len(todo)} to go (at least 8 in all are needed).")

    new: list[dict] = []
    i = 0
    while i < len(todo):
        row = todo[i]
        show(i + 1, len(todo), row)
        choice = input("\nEvery fact in the answer is in the passages? [y/n/s/b/q] ").strip().lower()
        if choice == "q":
            break
        if choice == "b":
            if new and new[-1]["id"] == todo[max(i - 1, 0)]["id"]:
                new.pop()
            i = max(i - 1, 0)
            continue
        if choice == "s":
            i += 1
            continue
        if choice not in ("y", "n"):
            print("type y, n, s, b or q")
            continue
        new.append({**row, "supported": choice == "y", "by": args.by})
        i += 1

    if new:
        with args.labels.open("a", encoding="utf-8") as f:
            for lab in new:
                f.write(json.dumps(lab, ensure_ascii=False) + "\n")
    total = len(done) + len(new)
    print(f"\nsaved {len(new)} new labels to {args.labels} ({total} in all"
          f"{'' if total >= 8 else f'; {8 - total} more needed'}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
