"""Generate the multi-hop eval suite (evals/multihop/gold.json) straight from the raw kep.yaml files.

These questions ask what is reachable from one KEP over the undirected graph of `see-also`, `replaces` and
`superseded-by` references (a reference is one hop whichever way it points; self references are dropped), within 2 or
3 hops. An answer needs more than the target's own file, which is what separates this suite from the lookups.

Questions are always phrased "reachable via see-also/replaces" and never use "depends on" or "dependency": the
corpus has no dependency edges, and those words would send the keyword router to a different tool.

Kinds (every question has params {"kep_id": "<n>"}; targets always have a unique valid kep-number):

* hop2_set: ids within 2 hops (a sorted list; unnumbered KEPs appear as "<sig-dir>/<folder>").
* reachable_count_3hop: how many KEPs are within 3 hops (an integer).
* stale_reach: starting from a withdrawn / rejected / replaced KEP, the ACTIVE KEPs within 3 hops (sorted ids).
* reachable_sigs: the SIGs that own the ACTIVE KEPs within 3 hops (sorted, alias-merged, unique).

ACTIVE and the status and SIG normalization come from `evals/yamlcorpus.py`, which reads the YAML only and never
imports from `ingest/` or `agent/` or touches FalkorDB.

Run from the repo root:  python -m evals.multihop.make_gold
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

from evals.yamlcorpus import (
    ACTIVE,
    DEFAULT_ENHANCEMENTS,
    kep_graph,
    load_keps,
    node_id,
    reach,
    sort_ids,
    source_commit,
    write_gold,
)

DEFAULT_OUT = Path(__file__).resolve().parent / "gold.json"

SEED = 42
HOP2_COUNT = 8
HOP2_NEED_SECOND_HOP = 6  # of the 8, targets whose 2-hop set is strictly larger than their 1-hop set
HOP2_MIN, HOP2_MAX = 1, 40
COUNT_QUESTIONS = 5
COUNT_MIN, COUNT_MAX = 1, 60
STALE_QUESTIONS = 8  # an upper bound: every eligible target is used, and there may be fewer than this
STALE_STATUSES = frozenset({"withdrawn", "rejected", "replaced"})
SIGS_ONE, SIGS_TWO, SIGS_MANY = 3, 3, 2  # targets whose active reach spans exactly 1, exactly 2, or 3+ SIGs


def banded_pick(rng: random.Random, sizes: dict[str, int], n: int) -> list[str]:
    """Pick `n` ids spread over the size range: split the distinct sizes into `n` contiguous bands and choose one id
    per band, so the sample holds small and large answers."""
    distinct = sorted(set(sizes.values()))
    if len(distinct) < n:
        raise RuntimeError(f"only {len(distinct)} distinct sizes to spread {n} questions over")
    picked = []
    for i in range(n):
        band = set(distinct[i * len(distinct) // n : (i + 1) * len(distinct) // n])
        picked.append(rng.choice(sorted((kid for kid, size in sizes.items() if size in band), key=int)))
    return picked


def build_gold(enhancements_dir: Path) -> dict:
    keps = load_keps(enhancements_dir)
    by_node = {node_id(k): k for k in keps}
    adj = kep_graph(keps)
    wide = kep_graph(keps, include_contested=True)
    within2: dict[str, dict[str, int]] = {}
    within3: dict[str, dict[str, int]] = {}
    candidates: list[str] = []  # targets: a unique valid kep-number, and an answer that no doubtful link changes
    for kid in sorted((k.kep_id for k in keps if k.kep_id), key=int):
        within3[kid] = reach(adj, kid, 3)
        if within3[kid] == reach(wide, kid, 3):  # (the 3-hop distances also fix the 2-hop set)
            within2[kid] = reach(adj, kid, 2)
            candidates.append(kid)

    def active_within3(kid: str) -> list[str]:
        return sort_ids(n for n in within3[kid] if by_node[n].status in ACTIVE)

    rng = random.Random(SEED)
    questions: list[dict] = []
    used: set[str] = set()  # one target per question across the suite

    def add(kind: str, question: str, kid: str, gold) -> None:
        used.add(kid)
        questions.append({"kind": kind, "question": question, "params": {"kep_id": kid}, "gold": gold})

    # stale_reach: withdrawn / rejected / replaced KEPs that still have an ACTIVE KEP within 3 hops.
    stale_pool = [kid for kid in candidates if by_node[kid].status in STALE_STATUSES and active_within3(kid)]
    if len(stale_pool) > STALE_QUESTIONS:
        stale_pool = sorted(rng.sample(stale_pool, STALE_QUESTIONS), key=int)
    if len(stale_pool) < STALE_QUESTIONS:
        print(
            f"note: only {len(stale_pool)} stale KEPs have an active KEP within 3 hops; "
            f"stale_reach has {len(stale_pool)} questions, not {STALE_QUESTIONS}",
            file=sys.stderr,
        )
    for kid in stale_pool:
        status = by_node[kid].status
        add(
            "stale_reach",
            f"Which active KEPs are reachable via see-also/replaces within 3 hops of KEP-{kid}, which is {status}?",
            kid,
            active_within3(kid),
        )

    # hop2_set: most targets need the second hop; a few do not (their 2-hop set equals their 1-hop set).
    needs_second_hop: dict[str, int] = {}
    one_hop_enough: list[str] = []
    for kid in candidates:
        if kid in used or not HOP2_MIN <= len(within2[kid]) <= HOP2_MAX:
            continue
        first_hop = sum(1 for hops in within2[kid].values() if hops == 1)
        if len(within2[kid]) > first_hop:
            needs_second_hop[kid] = len(within2[kid])
        elif len(within2[kid]) >= 2:
            one_hop_enough.append(kid)
    hop2_targets = banded_pick(rng, needs_second_hop, HOP2_NEED_SECOND_HOP)
    hop2_targets += rng.sample(one_hop_enough, HOP2_COUNT - HOP2_NEED_SECOND_HOP)
    for kid in hop2_targets:
        add(
            "hop2_set",
            f"Which KEPs are reachable via see-also/replaces within 2 hops of KEP-{kid}?",
            kid,
            sort_ids(within2[kid]),
        )

    # reachable_count_3hop: spread over small and large neighbourhoods.
    count_pool = {
        kid: len(within3[kid])
        for kid in candidates
        if kid not in used and COUNT_MIN <= len(within3[kid]) <= COUNT_MAX
    }
    for kid in banded_pick(rng, count_pool, COUNT_QUESTIONS):
        add(
            "reachable_count_3hop",
            f"How many KEPs are reachable via see-also/replaces within 3 hops of KEP-{kid}?",
            kid,
            len(within3[kid]),
        )

    # reachable_sigs: some targets whose active reach sits in one SIG, some spanning several.
    sigs_of: dict[str, list[str]] = {}
    for kid in candidates:
        if kid in used:
            continue
        sigs = sorted({by_node[n].owning_sig for n in active_within3(kid) if by_node[n].owning_sig})
        if sigs:
            sigs_of[kid] = sigs
    one = [kid for kid, sigs in sigs_of.items() if len(sigs) == 1]
    two = [kid for kid, sigs in sigs_of.items() if len(sigs) == 2]
    many = [kid for kid, sigs in sigs_of.items() if len(sigs) >= 3]
    sig_targets = rng.sample(one, SIGS_ONE) + rng.sample(two, SIGS_TWO) + rng.sample(many, SIGS_MANY)
    for kid in sig_targets:
        add(
            "reachable_sigs",
            f"Which SIGs own the active KEPs reachable via see-also/replaces within 3 hops of KEP-{kid}?",
            kid,
            sigs_of[kid],
        )

    rng.shuffle(questions)
    ordered = [{"id": f"q{i:02d}", **q} for i, q in enumerate(questions, start=1)]
    return {
        "source_commit": source_commit(enhancements_dir),
        "generated_by": "evals/multihop/make_gold.py",
        "questions": ordered,
    }


def main() -> None:
    if not (DEFAULT_ENHANCEMENTS / "keps").is_dir():
        sys.exit(f"error: corpus not found at {DEFAULT_ENHANCEMENTS} (run `make clone-data`)")
    write_gold(build_gold(DEFAULT_ENHANCEMENTS), DEFAULT_OUT)


if __name__ == "__main__":
    main()
