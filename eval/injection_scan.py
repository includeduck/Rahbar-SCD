"""Run the injection patterns (api/app/services/guardrails.py) over every chunk in the active
collection, to see what the retrieval rail would drop: the planted Batch B notice should be
the only hit, and any other hit is a false alarm (Assignment 03, D4).

Runs inside an api pod, which has the guardrails module and may reach Chroma:

    kubectl -n rahbar exec -i deploy/api -- python - < eval/injection_scan.py
"""

from __future__ import annotations

import os
import sys
from collections import Counter

import httpx

from app.services.guardrails import scan

BASE = (f"http://{os.environ.get('CHROMA_HOST', 'chroma')}:{os.environ.get('CHROMA_PORT', '8000')}"
        "/api/v2/tenants/default_tenant/databases/default_database/collections")


def main() -> int:
    with httpx.Client(timeout=30) as http:
        pointer = http.get(f"{BASE}/rahbar-active").json()
        name = (pointer.get("metadata") or {}).get("active") or "rahbar"
        col = http.get(f"{BASE}/{name}").json()
        ids: list[str] = []
        docs: list[str] = []
        metas: list[dict] = []
        offset = 0
        while True:
            page = http.post(f"{BASE}/{col['id']}/get", json={
                "limit": 200, "offset": offset, "include": ["documents", "metadatas"]}).json()
            if not page["ids"]:
                break
            ids += page["ids"]
            docs += page["documents"]
            metas += page["metadatas"]
            offset += len(page["ids"])
    print(f"collection {name}: {len(ids)} chunks scanned "
          f"({sum(1 for m in metas if m.get('superseded'))} of them superseded)")
    hits = 0
    per_doc: Counter[str] = Counter()
    for cid, text, meta in zip(ids, docs, metas):
        flags = scan(text or "")
        if not flags:
            continue
        hits += 1
        per_doc[str(meta.get("doc_id") or meta.get("source"))] += 1
        print(f"  FLAGGED {cid}  {meta.get('doc_id') or meta.get('source')} @ "
              f"{meta.get('doc_version')}  section {str(meta.get('section'))[:50]!r}")
        for f in flags:
            print(f"      {f.rule:<20} {f.match!r}")
    print(f"{hits} chunk(s) flagged in {len(per_doc)} document(s): "
          + (", ".join(f"{d} {n}" for d, n in per_doc.items()) or "none"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
