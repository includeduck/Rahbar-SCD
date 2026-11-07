"""Choose CACHE_THRESHOLD from measurements (section 3.4; api/app/services/cache.py).

Embeds question pairs with the pinned model and prints, for each candidate threshold,
how many paraphrases would hit the cache (wanted) and how many different questions
would hit it (a false hit returns the wrong cached answer). The rule: ship the lowest
threshold with zero false hits.

    python eval/cache_threshold.py --ollama http://localhost:11434
"""

from __future__ import annotations

import argparse
import json
import math
import urllib.request

# Same question, different wording: these should hit the cache.
PARAPHRASES = [
    ("What CGPA does an undergraduate need to stay off academic warning?",
     "What is the minimum CGPA to avoid academic warning?"),
    ("What CGPA puts a student on academic warning?",
     "academic warning CGPA threshold"),
    ("How much is the BS fee per credit hour?",
     "What is the fee per credit hour for BS?"),
    ("How do I apply for a hostel room?",
     "How do I apply for hostel accommodation?"),
    ("How much absence can the Head of Department condone?",
     "How many absences can the HoD condone?"),
    ("What is the minimum attendance percentage required to sit the final exam?",
     "What attendance do I need to be allowed into the final exam?"),
    ("If I repeat a course, is my grade capped?",
     "Is the grade capped when a course is repeated?"),
    ("Can a student register for more than five courses in a semester?",
     "Can I take more than five courses in one semester?"),
    ("What does clause 1.21 of the academic rules require?",
     "What does academic rule 1.21 say?"),
    ("What is the admission fee?",
     "How much is the admission fee?"),
]

# Similar wording, different question: a hit here would serve the wrong answer.
DIFFERENT = [
    ("What is academic warning?", "What is the grading policy?"),
    ("What CGPA puts a student on academic warning?",
     "What CGPA is needed to graduate?"),
    ("What CGPA does an undergraduate need to stay off academic warning?",
     "What happens if a student stays on academic warning for two semesters?"),
    ("How much is the BS fee per credit hour?",
     "How much is the MS fee per credit hour?"),
    ("How much is the BS fee per credit hour?",
     "How much is the admission fee?"),
    ("What is the minimum attendance percentage required to sit the final exam?",
     "What is the minimum CGPA to graduate?"),
    ("If I repeat a course, is my grade capped?",
     "If I fail a course, when do I have to retake it?"),
    ("Can a student register for more than five courses in a semester?",
     "Can a student register for fewer than three courses in a semester?"),
    ("What does clause 1.21 of the academic rules require?",
     "What does clause 1.22 of the academic rules require?"),
    ("How do I apply for a hostel room?",
     "How do I apply for a scholarship?"),
]

THRESHOLDS = [0.80, 0.85, 0.88, 0.90, 0.92, 0.95, 0.96, 0.97, 0.99]


def embed(url: str, model: str, texts: list[str]) -> list[list[float]]:
    request = urllib.request.Request(
        f"{url.rstrip('/')}/api/embed",
        data=json.dumps({"model": model, "input": texts}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.load(response)["embeddings"]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def similarities(url: str, model: str, pairs: list[tuple[str, str]]) -> list[float]:
    vectors = embed(url, model, [text for pair in pairs for text in pair])
    return [cosine(vectors[2 * i], vectors[2 * i + 1]) for i in range(len(pairs))]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ollama", default="http://localhost:11434")
    parser.add_argument("--model", default="nomic-embed-text")
    args = parser.parse_args()

    same = similarities(args.ollama, args.model, PARAPHRASES)
    other = similarities(args.ollama, args.model, DIFFERENT)
    print(f"model {args.model}\n\nparaphrases (should hit):")
    for (a, b), s in sorted(zip(PARAPHRASES, same), key=lambda x: -x[1]):
        print(f"  {s:.4f}  {a!r} ~ {b!r}")
    print("\ndifferent questions (must not hit):")
    for (a, b), s in sorted(zip(DIFFERENT, other), key=lambda x: -x[1]):
        print(f"  {s:.4f}  {a!r} ~ {b!r}")
    print(f"\n{'threshold':>9}  paraphrase hits  false hits")
    for t in THRESHOLDS:
        hits = sum(s >= t for s in same)
        false_hits = sum(s >= t for s in other)
        print(f"{t:>9.2f}  {hits:>6}/{len(same):<8}  {false_hits:>4}/{len(other)}")
    safe = [t for t in THRESHOLDS if not any(s >= t for s in other)]
    print(f"\nlowest threshold with zero false hits: {min(safe) if safe else 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
