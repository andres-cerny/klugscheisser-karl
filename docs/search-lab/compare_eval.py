"""Compare two chat-eval runs: python3 compare_eval.py main.json branch.json [live.json]"""

import json
import sys


def load(p):
    with open(p) as f:
        d = json.load(f)
    return {r["id"]: r for r in d["ergebnisse"]}


runs = [(p, load(p)) for p in sys.argv[1:]]
for p, r in runs:
    usd = sum(x.get("kosten_usd") or 0 for x in r.values())
    sec = sorted(x.get("sekunden") or 0 for x in r.values())
    print(
        f"{p}: {sum(1 for x in r.values() if x.get('ok'))}/{len(r)} ok, ${usd:.2f}, median {sec[len(sec) // 2]:.1f} s"
    )
(a_name, a), (b_name, b) = runs[0], runs[1]
for qid in sorted(set(a) | set(b)):
    oa, ob = (a.get(qid) or {}).get("ok"), (b.get(qid) or {}).get("ok")
    if oa != ob:
        r = b.get(qid) or a.get(qid)
        word = "BETTER" if ob else "WORSE"
        print(f"{word:6} {qid} {r['frage'][:70]} | probleme: {(b.get(qid) or {}).get('probleme')}")
