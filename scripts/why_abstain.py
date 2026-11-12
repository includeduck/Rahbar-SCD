"""Why did RAHBAR abstain? Re-run one question through the api's own steps and show the model's text.

When the api abstains it shows a fixed message and never logs what the model wrote. This
script runs inside an api pod, with that pod's settings, retriever and provider, and prints
which step decided and the model's own answer. It is a fresh generation: the model can word
things differently from the run you saw, and it bypasses the semantic cache.

    kubectl -n rahbar exec -i deploy/api -- python - "Your question here" < scripts/why_abstain.py
"""
import logging
import sys

sys.path.insert(0, "/app")
logging.disable(logging.WARNING)  # the api's own warnings would interleave; this script reports them

from app.config import get_settings  # noqa: E402
from app.providers.llm import get_provider  # noqa: E402
from app.retrieval import get_retriever  # noqa: E402
from app.schemas import RetrievedRef  # noqa: E402
from app.services.answer import _filter_citations  # noqa: E402
from app.services.safety import mentions_self_harm  # noqa: E402

question = " ".join(sys.argv[1:]).strip()
if not question:
    sys.exit('usage: ... python - "Your question" < scripts/why_abstain.py')

s = get_settings()
print(f"question: {question}")
print(f"provider: {s.llm_provider} ({s.llm_model}); floor {s.relevance_floor}; "
      f"top_k {s.top_k}; passages to the model {s.max_context_passages}\n")

if mentions_self_harm(question):
    print("DECIDED BY: the self-harm check. The support message is shown; nothing is retrieved "
          "and the model is not called.")
    sys.exit(0)

passages = get_retriever(s).retrieve(question, k=s.top_k)
below_floor = not passages or all(p.score < s.relevance_floor for p in passages)
print("retrieved:")
for i, p in enumerate(passages, 1):
    sent = "  -> sent to the model" if i <= s.max_context_passages and not below_floor else ""
    print(f"  {i}. {p.score:.4f}  {p.source} | {p.section[:60]}{sent}")

if below_floor:
    best = passages[0].score if passages else 0.0
    print(f"\nDECIDED BY: the relevance floor. Best score {best:.4f} < {s.relevance_floor}, so "
          "the model was never called; there is no model answer.")
    sys.exit(0)

context = passages[: max(1, s.max_context_passages)]
refs = [RetrievedRef(chunk_id=p.chunk_id, source=p.source, section=p.section, page=p.page,
                     score=p.score) for p in context]
answer = get_provider(s).generate(question, context)
kept = _filter_citations(answer.citations, refs)

print("\nthe model wrote:")
print(f"  answer:                {answer.answer}")
print(f"  reasoning:             {answer.reasoning}")
print(f"  answered_from_context: {answer.answered_from_context}")
print(f"  confidence:            {answer.confidence}")
print(f"  citations:             {[f'{c.source} | {c.section}' for c in answer.citations]}")
print(f"  citations that match a passage it was given: {len(kept)} of {len(answer.citations)}")

if not answer.answered_from_context and kept:
    verdict = ("the abstain-on-false rule. The model flagged its answer as not from the "
               "documents but cited a passage it was given; the api abstains whenever the flag is "
               "false (docs/evidence/flag-rule-comparison.txt)")
elif not answer.answered_from_context:
    verdict = "the model itself: it declined, with no valid citation"
elif not kept:
    verdict = ("the grounding check. The model claimed an answer, but none of its citations "
               "names a passage it was given; the api retries once, then abstains")
else:
    verdict = "nothing: this run would be shown as an answer from the documents"
print(f"\nDECIDED BY: {verdict}.")
