"""Generate the lookups eval suite (evals/lookups/gold.json) straight from the raw kep.yaml files.

Lookups are single-fact questions about one KEP or one SIG: owning SIG, status, successor, see-also referrers, SIG
size and approvers. The multi-hop questions live in `evals/multihop/`.

Independent reimplementation on purpose: this module reads `data/enhancements/keps/*/*/kep.yaml` through
`evals/yamlcorpus.py` (`yaml.BaseLoader`), never imports from `ingest/` or `agent/`, and never touches FalkorDB. The
eval then checks the whole parse -> load -> query pipeline against answers computed here.

Run from the repo root:  python -m evals.lookups.make_gold
"""

from __future__ import annotations

import random
import re
import sys
from collections import Counter
from pathlib import Path

from evals.yamlcorpus import (
    DEFAULT_ENHANCEMENTS,
    VALID_STATUSES,
    Kep,
    as_list,
    contested_edges,
    load_keps,
    resolve_refs,
    source_commit,
    write_gold,
)

DEFAULT_OUT = Path(__file__).resolve().parent / "gold.json"

SEED = 42
PER_KIND = 5  # owning_sig, status, see_also_referrers, sig_kep_count and approvers each
SUCCESSOR_EACH = 4  # has_successor: this many true plus this many false (8 questions)
MIN_SIG_KEPS = 5

_HANDLE_RE = re.compile(r"@([A-Za-z0-9][A-Za-z0-9_./-]*)")
_TEAM_RE = re.compile(r"^(sig|wg|committee)-|-(leads|tech-leads|approvers|reviewers|maintainers)$")
_SKIP_HANDLES = {"", "tbd", "n/a", "none"}


def approver_handles(kep: Kep) -> list[str]:
    """Sorted, deduplicated, lowercased approver handles, excluding placeholders and team aliases."""
    handles: set[str] = set()
    for entry in as_list(kep.data.get("approvers")):
        entry = entry.strip()
        m = _HANDLE_RE.search(entry)
        if m:
            token = m.group(1)
        else:
            parts = entry.split()
            token = parts[0] if parts else ""
        handle = token.strip("\"',;:().").lower()
        if handle in _SKIP_HANDLES or _TEAM_RE.search(handle):
            continue
        handles.add(handle)
    return sorted(handles)


def successor_ids(keps: list[Kep]) -> set[str]:
    """Valid KEP ids that have a successor: another KEP's `replaces` points at them, or their own
    `superseded-by` resolves to another KEP."""
    result: set[str] = set()
    for targets in resolve_refs(keps, "replaces").values():
        result |= targets
    for kep_id, targets in resolve_refs(keps, "superseded-by").items():
        if targets:
            result.add(kep_id)
    return result


def see_also_referrers(keps: list[Kep]) -> dict[str, list[str]]:
    """Map target KEP id -> sorted (numerically) list of KEP ids whose see-also resolves to it."""
    referrers: dict[str, set[str]] = {}
    for src, targets in resolve_refs(keps, "see-also").items():
        for tgt in targets:
            referrers.setdefault(tgt, set()).add(src)
    return {tgt: sorted(srcs, key=int) for tgt, srcs in referrers.items()}


def sig_kep_counts(keps: list[Kep]) -> Counter:
    """Owning-SIG counts over ALL kep.yaml files (including ones without a valid kep-number)."""
    return Counter(k.owning_sig for k in keps if k.owning_sig)


def _sorted_ids(ids) -> list[str]:
    return sorted(ids, key=int)


def build_gold(enhancements_dir: Path) -> dict:
    keps = load_keps(enhancements_dir)
    valid = sorted((k for k in keps if k.kep_id), key=lambda k: int(k.kep_id))
    rng = random.Random(SEED)
    questions: list[dict] = []

    # Leave out KEPs whose answer would change if a doubtful link (see `contested_edges`) were followed.
    contested = contested_edges(keps)
    unsafe_referrer_targets = {tgt for _, field, tgt in contested if field == "see-also"}
    unsafe_successor_keps = {tgt for _, field, tgt in contested if field == "replaces"} | {
        src for src, field, _ in contested if field == "superseded-by"
    }

    # has_successor: SUCCESSOR_EACH true + the same number false.
    succ = successor_ids(keps)
    trues = _sorted_ids(succ - unsafe_successor_keps)
    if len(trues) < SUCCESSOR_EACH:
        raise RuntimeError(f"only {len(trues)} KEPs with a successor; need >= {SUCCESSOR_EACH}")
    if len(trues) > SUCCESSOR_EACH:
        trues = _sorted_ids(rng.sample(trues, SUCCESSOR_EACH))
    false_pool = _sorted_ids(k.kep_id for k in valid if k.kep_id not in succ | unsafe_successor_keps)
    falses = _sorted_ids(rng.sample(false_pool, len(trues)))
    succ_qs = [(i, True) for i in trues] + [(i, False) for i in falses]
    rng.shuffle(succ_qs)

    # owning_sig
    owning_pool = [k for k in valid if k.owning_sig]
    for kep in rng.sample(owning_pool, PER_KIND):
        questions.append(
            {
                "kind": "owning_sig",
                "question": f"Which SIG owns KEP-{kep.kep_id}?",
                "params": {"kep_id": kep.kep_id},
                "gold": kep.owning_sig,
            }
        )

    # status
    status_pool = [k for k in valid if k.raw_status in VALID_STATUSES]
    for kep in rng.sample(status_pool, PER_KIND):
        questions.append(
            {
                "kind": "status",
                "question": f"What is the status of KEP-{kep.kep_id}?",
                "params": {"kep_id": kep.kep_id},
                "gold": kep.raw_status.strip().lower(),
            }
        )

    for kep_id, has in succ_qs:
        questions.append(
            {
                "kind": "has_successor",
                "question": f"Does KEP-{kep_id} have a successor?",
                "params": {"kep_id": kep_id},
                "gold": has,
            }
        )

    # see_also_referrers
    referrers = see_also_referrers(keps)
    for target in rng.sample(_sorted_ids(set(referrers) - unsafe_referrer_targets), PER_KIND):
        questions.append(
            {
                "kind": "see_also_referrers",
                "question": f"Which KEPs list KEP-{target} in see-also?",
                "params": {"kep_id": target},
                "gold": referrers[target],
            }
        )

    # sig_kep_count
    counts = sig_kep_counts(keps)
    sig_pool = sorted(s for s, n in counts.items() if n >= MIN_SIG_KEPS)
    for sig in rng.sample(sig_pool, PER_KIND):
        questions.append(
            {
                "kind": "sig_kep_count",
                "question": f"How many KEPs does {sig} own?",
                "params": {"sig": sig},
                "gold": counts[sig],
            }
        )

    # approvers
    approver_pool = [k for k in valid if approver_handles(k)]
    for kep in rng.sample(approver_pool, PER_KIND):
        questions.append(
            {
                "kind": "approvers",
                "question": f"Who approved KEP-{kep.kep_id}?",
                "params": {"kep_id": kep.kep_id},
                "gold": approver_handles(kep),
            }
        )

    rng.shuffle(questions)
    ordered = [{"id": f"q{i:02d}", **q} for i, q in enumerate(questions, start=1)]
    return {
        "source_commit": source_commit(enhancements_dir),
        "generated_by": "evals/lookups/make_gold.py",
        "questions": ordered,
    }


def main() -> None:
    if not (DEFAULT_ENHANCEMENTS / "keps").is_dir():
        sys.exit(f"error: corpus not found at {DEFAULT_ENHANCEMENTS} (run `make clone-data`)")
    write_gold(build_gold(DEFAULT_ENHANCEMENTS), DEFAULT_OUT)


if __name__ == "__main__":
    main()
