"""Compare two rules for an answer flagged answered_from_context=false (api/app/services/answer.py).

Rule A (shipped): answered_from_context=False always abstains.
Rule B: False with a valid citation and confidence medium/high counts as answered.

Each golden question is generated once inside an api pod and both rules are applied to that
same output, so model randomness cannot favour either. Output: docs/evidence/flag-rule-comparison.txt.

    python -c "import yaml,json; print(json.dumps([{'id': q['id'], 'question': q['question'],
      'must_abstain': bool(q.get('must_abstain')), 'expect': q.get('expect')}
      for q in yaml.safe_load(open('eval/golden.yaml'))['questions']]))" > /tmp/golden.json
    kubectl -n rahbar exec -i deploy/api -- sh -c 'cd /app && python -c "$1"' _       "$(cat eval/compare_flag_rules.py)" < /tmp/golden.json
"""
import json
import sys

from app.config import get_settings
from app.providers.llm import get_provider
from app.retrieval import get_retriever
from app.schemas import RetrievedRef
from app.services.answer import _filter_citations

golden = json.load(sys.stdin)
s = get_settings()
retriever, provider = get_retriever(s), get_provider(s)
rows = []
for g in golden:
    passages = retriever.retrieve(g["question"], k=s.top_k)
    if not passages or all(p.score < s.relevance_floor for p in passages):
        rows.append({"id": g["id"], "abstain": g["must_abstain"], "floor": True})
        continue
    ctx = passages[: max(1, s.max_context_passages)]
    refs = [RetrievedRef(chunk_id=p.chunk_id, source=p.source, section=p.section, page=p.page,
                         score=p.score) for p in ctx]
    a = provider.generate(g["question"], ctx)
    cites = _filter_citations(a.citations, refs)
    a_answered = a.answered_from_context and bool(cites)
    b_answered = bool(cites) and (a.answered_from_context or a.confidence != "low")
    expect = (g.get("expect") or "").lower()
    rows.append({
        "id": g["id"], "abstain": g["must_abstain"], "flag": a.answered_from_context,
        "conf": a.confidence, "cites": len(cites), "A": a_answered, "B": b_answered,
        "expect": g.get("expect"), "hit": bool(expect) and expect in a.answer.lower(),
        "answer": a.answer[:140], "top": round(passages[0].score, 3),
    })
    print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
print("SUMMARY", json.dumps(rows, ensure_ascii=False))
