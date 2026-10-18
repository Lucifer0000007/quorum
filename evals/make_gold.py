"""Generate the Quorum eval set (evals/gold.json) straight from the raw kep.yaml files.

Independent reimplementation on purpose: this module reads `data/enhancements/keps/*/*/kep.yaml` with
`yaml.BaseLoader`, never imports from `ingest/` or `agent/`, and never touches FalkorDB. The eval then checks the
whole parse -> load -> query pipeline against answers computed here.

Run from the repo root:  python -m evals.make_gold
"""

from __future__ import annotations

import json
import random
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENHANCEMENTS = REPO_ROOT / "data" / "enhancements"
DEFAULT_OUT = Path(__file__).resolve().parent / "gold.json"

SEED = 42
TOTAL_QUESTIONS = 28
SUCCESSOR_TRUE_MAX = 4  # has_successor kind: at most 4 true + 4 false (8 total)
SUCCESSOR_TRUE_MIN = 3
MIN_SIG_KEPS = 5

SIG_ALIASES = {
    "sig-apimachinery": "sig-api-machinery",
    "sig-machinery": "sig-api-machinery",
    "sig-arch": "sig-architecture",
    "sig-contribex": "sig-contributor-experience",
    "sig-scheduler": "sig-scheduling",
}
VALID_STATUSES = {"provisional", "implementable", "implemented", "deferred", "rejected", "withdrawn", "replaced"}

_REF_RE = re.compile(r"keps/([^/\s\"'#?)]+)/([^/\s\"'#?)]+)")
_NUM_PREFIX_RE = re.compile(r"^(\d+)-")


@dataclass
class Kep:
    folder: str  # e.g. "2221-remove-dockershim"
    sig_dir: str  # e.g. "sig-node"
    data: dict
    kep_id: str | None = None  # canonical id, set only for valid unique numeric KEPs
    owning_sig: str | None = None
    raw_status: str = ""
    refs: dict[str, list[str]] = field(default_factory=dict)


def normalize_sig(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    sig = value.strip().lower()
    return SIG_ALIASES.get(sig, sig)


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str) and v.strip()]
    if isinstance(value, dict):
        return [v for v in value.values() if isinstance(v, str) and v.strip()]
    return []


def load_keps(enhancements_dir: Path) -> list[Kep]:
    """Parse every keps/*/*/kep.yaml (sorted by path). Assigns kep_id only to valid, unique, positive numbers."""
    paths = sorted((enhancements_dir / "keps").glob("*/*/kep.yaml"))
    if not paths:
        raise FileNotFoundError(f"no kep.yaml files under {enhancements_dir / 'keps'}")
    keps: list[Kep] = []
    for path in paths:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        if not isinstance(data, dict):
            data = {}
        status = data.get("status")
        keps.append(
            Kep(
                folder=path.parent.name,
                sig_dir=path.parent.parent.name,
                data=data,
                owning_sig=normalize_sig(data.get("owning-sig")),
                raw_status=status if isinstance(status, str) else "",
                refs={k: _as_list(data.get(k)) for k in ("replaces", "superseded-by", "see-also")},
            )
        )

    def numeric(kep: Kep) -> int | None:
        raw = kep.data.get("kep-number")
        if not isinstance(raw, str):
            return None
        raw = raw.strip()
        if not raw.isdigit() or int(raw) <= 0:
            return None
        return int(raw)

    counts = Counter(n for n in (numeric(k) for k in keps) if n is not None)
    for kep in keps:
        n = numeric(kep)
        if n is not None and counts[n] == 1:
            kep.kep_id = str(n)
    return keps


def _resolver(keps: list[Kep]):
    valid_ids = {k.kep_id for k in keps if k.kep_id}
    folder_to_id: dict[str, str | None] = {}
    for kep in keps:
        # A folder name seen on more than one KEP is ambiguous; it only resolves if all agree.
        if kep.folder in folder_to_id and folder_to_id[kep.folder] != kep.kep_id:
            folder_to_id[kep.folder] = None
        else:
            folder_to_id.setdefault(kep.folder, kep.kep_id)

    def resolve(ref: str) -> str | None:
        m = _REF_RE.search(ref)
        if not m:
            return None
        folder = m.group(2).rstrip("/")
        if folder.endswith(".md"):
            folder = folder[: -len(".md")]
        if folder in folder_to_id:
            return folder_to_id[folder]
        num = _NUM_PREFIX_RE.match(folder)
        if num and str(int(num.group(1))) in valid_ids:
            return str(int(num.group(1)))
        return None

    return resolve


def resolve_refs(keps: list[Kep], field_name: str) -> dict[str, set[str]]:
    """Map each valid KEP id to the set of valid KEP ids its `field_name` resolves to (self references dropped)."""
    resolve = _resolver(keps)
    out: dict[str, set[str]] = {}
    for kep in keps:
        if not kep.kep_id:
            continue
        targets = {t for t in (resolve(r) for r in kep.refs.get(field_name, [])) if t and t != kep.kep_id}
        out[kep.kep_id] = targets
    return out


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

    # has_successor: up to 4 true + same number false (8 total).
    succ = successor_ids(keps)
    trues = _sorted_ids(succ)
    if len(trues) < SUCCESSOR_TRUE_MIN:
        raise RuntimeError(f"only {len(trues)} KEPs with a successor; need >= {SUCCESSOR_TRUE_MIN}")
    if len(trues) > SUCCESSOR_TRUE_MAX:
        trues = _sorted_ids(rng.sample(trues, SUCCESSOR_TRUE_MAX))
    falses = _sorted_ids(rng.sample(_sorted_ids(k.kep_id for k in valid if k.kep_id not in succ), len(trues)))
    succ_qs = [(i, True) for i in trues] + [(i, False) for i in falses]
    rng.shuffle(succ_qs)

    remaining = TOTAL_QUESTIONS - len(succ_qs)
    per_kind = [remaining // 4 + (1 if i < remaining % 4 else 0) for i in range(4)]

    # owning_sig
    owning_pool = [k for k in valid if k.owning_sig]
    for kep in rng.sample(owning_pool, per_kind[0]):
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
    for kep in rng.sample(status_pool, per_kind[1]):
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
    for target in rng.sample(_sorted_ids(referrers), per_kind[2]):
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
    for sig in rng.sample(sig_pool, per_kind[3]):
        questions.append(
            {
                "kind": "sig_kep_count",
                "question": f"How many KEPs does {sig} own?",
                "params": {"sig": sig},
                "gold": counts[sig],
            }
        )

    rng.shuffle(questions)
    ordered = [{"id": f"q{i:02d}", **q} for i, q in enumerate(questions, start=1)]
    return {
        "source_commit": _source_commit(enhancements_dir),
        "generated_by": "evals/make_gold.py",
        "questions": ordered,
    }


def _source_commit(enhancements_dir: Path) -> str:
    out = subprocess.run(
        ["git", "-C", str(enhancements_dir), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def main() -> None:
    if not (DEFAULT_ENHANCEMENTS / "keps").is_dir():
        sys.exit(f"error: corpus not found at {DEFAULT_ENHANCEMENTS} (run `make clone-data`)")
    gold = build_gold(DEFAULT_ENHANCEMENTS)
    DEFAULT_OUT.write_text(json.dumps(gold, indent=2) + "\n", encoding="utf-8")
    kinds = Counter(q["kind"] for q in gold["questions"])
    print(f"wrote {DEFAULT_OUT} ({len(gold['questions'])} questions, commit {gold['source_commit'][:12]})")
    for kind, n in sorted(kinds.items()):
        print(f"  {kind}: {n}")


if __name__ == "__main__":
    main()
