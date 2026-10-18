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
BASE_QUESTIONS = 28  # the five original kinds
HARD_KIND_COUNT = 5  # blast_radius_count and approvers each
BLAST_MIN, BLAST_MAX = 1, 60
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


def node_id(kep: Kep) -> str:
    """Graph node id: the canonical number for valid KEPs, else "<sig-dir>/<folder>" (as the app does)."""
    return kep.kep_id or f"{kep.sig_dir}/{kep.folder}"


def _resolver(keps: list[Kep]):
    """Return resolve(ref) -> node id (canonical number, or "<sig-dir>/<folder>" for KEPs without one)."""
    valid_ids = {k.kep_id for k in keps if k.kep_id}
    folder_to_id: dict[str, str | None] = {}
    for kep in keps:
        # A folder name seen on more than one KEP is ambiguous; it only resolves if all agree.
        if kep.folder in folder_to_id and folder_to_id[kep.folder] != node_id(kep):
            folder_to_id[kep.folder] = None
        else:
            folder_to_id.setdefault(kep.folder, node_id(kep))

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
    valid_ids = {k.kep_id for k in keps if k.kep_id}
    out: dict[str, set[str]] = {}
    for kep in keps:
        if not kep.kep_id:
            continue
        resolved = (resolve(r) for r in kep.refs.get(field_name, []))
        out[kep.kep_id] = {t for t in resolved if t in valid_ids and t != kep.kep_id}
    return out


def kep_graph(keps: list[Kep]) -> dict[str, set[str]]:
    """Undirected adjacency over ALL KEPs (node ids per `node_id`). Edges: see-also and replaces (src -> target),
    superseded-by (target -> src). Direction is irrelevant once undirected; self references are dropped."""
    resolve = _resolver(keps)
    adj: dict[str, set[str]] = {node_id(k): set() for k in keps}
    for kep in keps:
        src = node_id(kep)
        for field_name in ("see-also", "replaces", "superseded-by"):
            for ref in kep.refs.get(field_name, []):
                tgt = resolve(ref)
                if tgt and tgt != src:
                    adj[src].add(tgt)
                    adj[tgt].add(src)
    return adj


def blast_radius_count(adj: dict[str, set[str]], start: str, max_hops: int = 3) -> int:
    """Number of other nodes reachable from `start` within `max_hops` hops (BFS)."""
    seen = {start}
    frontier = {start}
    for _ in range(max_hops):
        frontier = {n for f in frontier for n in adj.get(f, ()) if n not in seen}
        if not frontier:
            break
        seen |= frontier
    return len(seen) - 1


_HANDLE_RE = re.compile(r"@([A-Za-z0-9][A-Za-z0-9_./-]*)")
_TEAM_RE = re.compile(r"^(sig|wg|committee)-|-(leads|tech-leads|approvers|reviewers|maintainers)$")
_SKIP_HANDLES = {"", "tbd", "n/a", "none"}


def approver_handles(kep: Kep) -> list[str]:
    """Sorted, deduplicated, lowercased approver handles, excluding placeholders and team aliases."""
    handles: set[str] = set()
    for entry in _as_list(kep.data.get("approvers")):
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

    remaining = BASE_QUESTIONS - len(succ_qs)
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

    # blast_radius_count: split the distinct radius sizes in [BLAST_MIN, BLAST_MAX] into contiguous bands and
    # pick one KEP per band, so the sample spans small and large radii.
    adj = kep_graph(keps)
    radius = {k.kep_id: blast_radius_count(adj, k.kep_id) for k in valid}
    eligible = {kid: n for kid, n in radius.items() if BLAST_MIN <= n <= BLAST_MAX}
    sizes = sorted(set(eligible.values()))
    if len(sizes) < HARD_KIND_COUNT:
        raise RuntimeError(f"only {len(sizes)} distinct blast-radius sizes in range; need {HARD_KIND_COUNT}")
    bands = [
        sizes[i * len(sizes) // HARD_KIND_COUNT : (i + 1) * len(sizes) // HARD_KIND_COUNT]
        for i in range(HARD_KIND_COUNT)
    ]
    for band in bands:
        band_ids = _sorted_ids(kid for kid, n in eligible.items() if n in set(band))
        target = rng.choice(band_ids)
        questions.append(
            {
                "kind": "blast_radius_count",
                "question": f"What is the blast radius of KEP-{target}?",
                "params": {"kep_id": target},
                "gold": eligible[target],
            }
        )

    # approvers
    approver_pool = [k for k in valid if approver_handles(k)]
    for kep in rng.sample(approver_pool, HARD_KIND_COUNT):
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
