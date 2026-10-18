"""Run the bailiff over every KEP in the live graph and record the verdict distribution (data/verdicts.json)."""

import json
import time
from collections import Counter

from agent.bailiff import preflight
from agent.db import db, meta
from ingest.config import BRAIN_GRAPH, DATA_DIR


def main():
    ids = [r[0] for r in db().select_graph(BRAIN_GRAPH).ro_query(
        "MATCH (k:KEP) RETURN k.id ORDER BY k.id", timeout=3000).result_set]
    t0 = time.time()
    verdicts, rules = Counter(), Counter()
    for kid in ids:
        r = preflight(kid)
        verdicts[r["verdict"]] += 1
        rules.update(x["rule"] for x in r["reasons"])
    # Only deterministic values go into the committed file (no run time), so re-running it leaves git clean.
    out = {"data_hash": meta()["data_hash"], "keps": len(ids), "verdicts": dict(verdicts.most_common()),
           "rules_fired": dict(rules.most_common())}
    (DATA_DIR / "verdicts.json").write_text(json.dumps(out, indent=2), encoding="utf-8", newline="\n")
    print(json.dumps(out, indent=2))
    print(f"({len(ids)} pre-flight checks in {time.time() - t0:.1f} s)")


if __name__ == "__main__":
    main()
