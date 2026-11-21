# Noise documents for the Part B noise test

Three made-up documents that are **not** part of the corpus and are never indexed. The suite
(eval/suite.py) embeds them in memory and merges their chunks into each question's ranked
list by similarity, which is exactly the list Chroma would return if they had been indexed
(cosine ranking scores every chunk independently). Then it recomputes recall@3, recall@5,
MRR and nDCG@5. None of their chunks is ever relevant.

| file | why it is noise |
|---|---|
| `northvale-academic-policy.md` | another (fictional) university's rules on the same topics with different numbers: the hard case, close in meaning |
| `northvale-fee-schedule.md` | a fictional fee schedule: same vocabulary as our fee questions |
| `city-sports-roundup.md` | unrelated to universities: should never rank |
