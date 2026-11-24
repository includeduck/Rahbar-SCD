"""How often the faithfulness judge agrees with answers labelled by hand (Assignment 03,
Part B: "label at least 8 answers by hand and report how often the judge agrees with you").

1. The suite writes eval/results/suite-<label>-<ts>-judge-sheet.jsonl: every answer the judge
   scored in run 1, with the question and the passages the model saw, and "supported": null.
   The judge's own verdict is not in the sheet, so it cannot steer the labeller.
2. Read each answer against its passages and set "supported" to true or false, and "by" to
   your name. Copy the labelled lines (at least 8) into eval/judge_labels.jsonl.
3. python -m eval.agreement eval/results/suite-<label>-<ts>.json

A label applies to an answer only if the answer text is the same, so labels stay valid when the
suite is run again with the same seed (the answers repeat) and stop counting when an answer
changes. The result file and its .md summary are updated in place.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


def _norm(text: str) -> str:
    return " ".join((text or "").split()).lower()


def load_labels(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    labels = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
              if line.strip()]
    return [lab for lab in labels if isinstance(lab.get("supported"), bool)]


def agreement(result: dict[str, Any], labels_path: Path) -> dict[str, Any] | None:
    labels = load_labels(labels_path)
    if not labels:
        return None
    by_key = {(lab["id"], _norm(lab["answer"])): lab for lab in labels}
    pairs = []
    for run_no, run in enumerate(result["runs"], 1):
        for row in (run.get("e2e") or {}).get("per_question", []):
            lab = by_key.get((row["id"], _norm(row["answer"])))
            if lab is not None and "judge" in row:
                pairs.append({"run": run_no, "id": row["id"], "human": lab["supported"],
                              "judge": row["judge"]["supported"],
                              "judge_reason": row["judge"]["reason"], "by": lab.get("by")})
    first = [p for p in pairs if p["run"] == 1] or pairs
    agree = sum(p["human"] == p["judge"] for p in first)
    both_yes = sum(p["human"] and p["judge"] for p in first)
    both_no = sum(not p["human"] and not p["judge"] for p in first)
    n = len(first)
    # Cohen's kappa: agreement beyond what two raters with these yes-rates would reach by chance.
    p_h = sum(p["human"] for p in first) / n if n else 0
    p_j = sum(p["judge"] for p in first) / n if n else 0
    chance = p_h * p_j + (1 - p_h) * (1 - p_j)
    kappa = ((agree / n - chance) / (1 - chance)) if n and chance < 1 else None
    return {
        "labels_file": str(labels_path), "labels": len(labels), "labelled": n,
        "agree": agree, "rate": round(agree / n, 4) if n else None,
        "both_supported": both_yes, "both_unsupported": both_no,
        "human_yes_judge_no": sum(p["human"] and not p["judge"] for p in first),
        "human_no_judge_yes": sum(not p["human"] and p["judge"] for p in first),
        "cohen_kappa": round(kappa, 4) if kappa is not None else None,
        "disagreements": [p for p in first if p["human"] != p["judge"]],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Judge vs hand labels for one suite result.")
    parser.add_argument("result", type=Path)
    parser.add_argument("--labels", type=Path, default=HERE / "judge_labels.jsonl")
    args = parser.parse_args(argv)
    result = json.loads(args.result.read_text(encoding="utf-8"))
    found = agreement(result, args.labels)
    if not found or not found["labelled"]:
        print(f"no labels in {args.labels} match the answers in {args.result}")
        return 1
    result["judge_agreement"] = found
    args.result.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    from eval.suite import summary_markdown

    args.result.with_suffix(".md").write_text(summary_markdown(result, args.result.stem),
                                              encoding="utf-8")
    print(f"judge vs hand labels: {found['agree']} of {found['labelled']} agree "
          f"({found['rate']:.0%}), Cohen's kappa {found['cohen_kappa']}")
    print(f"  both supported {found['both_supported']}, both unsupported "
          f"{found['both_unsupported']}, you yes / judge no {found['human_yes_judge_no']}, "
          f"you no / judge yes {found['human_no_judge_yes']}")
    for d in found["disagreements"]:
        print(f"  {d['id']}: you {d['human']}, judge {d['judge']} ({d['judge_reason']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
