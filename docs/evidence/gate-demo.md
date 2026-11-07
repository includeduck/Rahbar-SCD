# CI gate demonstration (section 6)

PR [#11](https://github.com/includeduck/Rahbar-SCD/pull/11), opened on purpose and closed
without merging, 7–8 November 2026.

| Step | Commit | `e2e-eval` | Screenshot |
| --- | --- | --- | --- |
| 1. A plausible-looking regression: `RELEVANCE_FLOOR` 0.60 → 0.40 in `k8s/overlays/ci` ("answer more questions") | `8af6509` | **failed**: `GATE FAILED – abstention correctness 0.0000 < required 1.0`, exit code 1 ([run 42403480627](https://github.com/includeduck/Rahbar-SCD/actions/runs/42403480627)) | [`gate-demo-1-e2e-eval-failed.png`](gate-demo-1-e2e-eval-failed.png): the other five checks green, `e2e-eval` red, the failing step's log |
| | | merge blocked: "1 failing, 5 successful checks", merge button disabled | [`gate-demo-2-merge-blocked.png`](gate-demo-2-merge-blocked.png) |
| 2. The fix: the floor back to 0.60 | `b148671` | **passed** ([run 42404003617](https://github.com/includeduck/Rahbar-SCD/actions/runs/42404003617)) | [`gate-demo-3-all-checks-passed.png`](gate-demo-3-all-checks-passed.png): all six checks green, each marked Required |

**Why it failed.** On the fixture corpus the must-abstain question ("How do I apply for a
hostel room?") scores 0.500 against its best chunk. In CI the LLM is the stub, so only the
relevance floor can decline it: at 0.60 it is declined, at 0.40 it is "answered from
context", and the gate's rule that every must-abstain question is declined fails.

**Why merging still says "blocked" in the green screenshot.** The `main` ruleset requires
both the six checks (`lint`, `test`, `manifests`, `build`, `scan`, `e2e-eval`) and one
approving review. In step 1 the failing check blocked the merge as well as the missing
review; in step 2 only the review is outstanding.
