"""Independent, YAML-only view of the Kubernetes enhancements corpus, shared by both gold generators.

Everything here reads `data/enhancements/keps/*/*/kep.yaml` with `yaml.BaseLoader` (plus the git commit id of the
corpus checkout) and nothing else. It never imports from `ingest/` or `agent/`, never imports falkordb or redis, and
never connects to a database. The point of the evaluation is that the gold answers do not depend on the app they are
used to check.

What lives here (the pieces both `evals/lookups/make_gold.py` and `evals/multihop/make_gold.py` need):

* corpus loading and canonical ids: a KEP gets the id "<n>" only when its `kep-number` is a positive integer that no
  other kep.yaml uses; every other KEP is keyed "<sig-dir>/<folder>" (see `node_id`);
* SIG alias merging, status normalization, and the ACTIVE / STALE status groups;
* reference resolution (a `see-also` / `replaces` / `superseded-by` entry -> a KEP id). An entry names a KEP in one of
  three ways: a `keps/<sig>/<folder>` path (a renamed folder still resolves by its number prefix), a link to the KEP's
  enhancement issue (`.../enhancements/issues/<n>`; a KEP's number is its tracking issue's number), or a `KEP-<n>`
  mention. Everything else (design docs, pull requests, legacy `.md` paths, "n/a") names no KEP;
* the undirected KEP graph and `reach`, the minimum hop distance to every KEP within N hops;
* `contested_edges`: links a more generous reader would follow although the rules above do not (a bare number in a
  keps path, a KEP folder name inside a mistyped path). The generators keep questions away from them, so no gold
  answer depends on how such a link is read.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENHANCEMENTS = REPO_ROOT / "data" / "enhancements"

SIG_ALIASES = {
    "sig-apimachinery": "sig-api-machinery",
    "sig-machinery": "sig-api-machinery",
    "sig-arch": "sig-architecture",
    "sig-contribex": "sig-contributor-experience",
    "sig-scheduler": "sig-scheduling",
}

VALID_STATUSES = {"provisional", "implementable", "implemented", "deferred", "rejected", "withdrawn", "replaced"}
# Misspellings and synonyms found in the corpus, mapped onto the valid statuses. Anything else is "unknown".
STATUS_ALIASES = {
    "imlpemented": "implemented",
    "implemented (alpha)": "implemented",
    "superseded": "replaced",
    "removed": "withdrawn",
}
UNKNOWN_STATUS = "unknown"
ACTIVE = frozenset({"provisional", "implementable", "implemented"})
STALE = frozenset({"replaced", "rejected", "withdrawn", "deferred"})

REF_FIELDS = ("see-also", "replaces", "superseded-by")

_REF_RE = re.compile(r"keps/([^/\s\"'#?)]+)/([^/\s\"'#?)]+)")
_NUM_PREFIX_RE = re.compile(r"^(\d+)-")
_ISSUE_RE = re.compile(r"enhancements/issues/(\d+)")
_KEP_MENTION_RE = re.compile(r"\bKEP[- ]?#?(\d+)\b", re.IGNORECASE)
_BARE_NUMBER_RE = re.compile(r"keps/[^/\s\"'#?)]+/(\d+)(?![\w-])")
_TOKEN_RE = re.compile(r"[^/\s\"'()\[\],]+")


@dataclass
class Kep:
    folder: str  # e.g. "2221-remove-dockershim"
    sig_dir: str  # e.g. "sig-node"
    data: dict
    kep_id: str | None = None  # canonical id, set only for valid unique numeric KEPs
    owning_sig: str | None = None  # alias-merged
    raw_status: str = ""
    refs: dict[str, list[str]] = field(default_factory=dict)

    @property
    def status(self) -> str:
        """Normalized status (see `normalize_status`)."""
        return normalize_status(self.raw_status)


def normalize_sig(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    sig = value.strip().lower()
    return SIG_ALIASES.get(sig, sig)


def normalize_status(raw: object) -> str:
    """Map a raw `status` value to one of VALID_STATUSES, or "unknown" when it is none of them."""
    if not isinstance(raw, str):
        return UNKNOWN_STATUS
    value = raw.strip().lower()
    value = STATUS_ALIASES.get(value, value)
    return value if value in VALID_STATUSES else UNKNOWN_STATUS


def as_list(value: object) -> list[str]:
    """A YAML scalar / list / mapping as a list of its non-blank string items."""
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
                refs={k: as_list(data.get(k)) for k in REF_FIELDS},
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
    """Graph node id: the canonical number for valid KEPs, else "<sig-dir>/<folder>"."""
    return kep.kep_id or f"{kep.sig_dir}/{kep.folder}"


def id_sort_key(ident: str) -> tuple[int, int, str]:
    """Numbered ids first in numeric order, then "<sig-dir>/<folder>" ids alphabetically."""
    return (0, int(ident), "") if ident.isdigit() else (1, 0, ident)


def sort_ids(ids) -> list[str]:
    return sorted(ids, key=id_sort_key)


def make_resolver(keps: list[Kep]):
    """Return resolve(ref) -> node id (canonical number, or "<sig-dir>/<folder>" for KEPs without one), or None."""
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
            # No keps/ path: a link to the KEP's enhancement issue, or a "KEP-<n>" mention, names KEP <n>.
            for pattern in (_ISSUE_RE, _KEP_MENTION_RE):
                hit = pattern.search(ref)
                if hit:
                    number = str(int(hit.group(1)))
                    return number if number in valid_ids else None
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
    resolve = make_resolver(keps)
    valid_ids = {k.kep_id for k in keps if k.kep_id}
    out: dict[str, set[str]] = {}
    for kep in keps:
        if not kep.kep_id:
            continue
        resolved = (resolve(r) for r in kep.refs.get(field_name, []))
        out[kep.kep_id] = {t for t in resolved if t in valid_ids and t != kep.kep_id}
    return out


def contested_edges(keps: list[Kep]) -> set[tuple[str, str, str]]:
    """(source node id, field, target node id) for links a more generous reader would follow although `make_resolver`
    does not: a bare number in a keps path (`/keps/sig-node/5307`) or a KEP folder name inside a mistyped path
    (`/kep/sig-network/0752-endpointslices/`)."""
    resolve = make_resolver(keps)
    valid_ids = {k.kep_id for k in keps if k.kep_id}
    folder_to_ids: dict[str, set[str]] = {}
    for kep in keps:
        folder_to_ids.setdefault(kep.folder, set()).add(node_id(kep))
    found: set[tuple[str, str, str]] = set()
    for kep in keps:
        src = node_id(kep)
        for field_name in REF_FIELDS:
            for ref in kep.refs.get(field_name, []):
                if resolve(ref) is not None:
                    continue
                target = None
                bare = _BARE_NUMBER_RE.search(ref)
                if bare and str(int(bare.group(1))) in valid_ids:
                    target = str(int(bare.group(1)))
                else:
                    for token in _TOKEN_RE.findall(ref):
                        ids = folder_to_ids.get(token.removesuffix(".md"))
                        if ids and len(ids) == 1:
                            target = next(iter(ids))
                            break
                if target and target != src:
                    found.add((src, field_name, target))
    return found


def kep_graph(keps: list[Kep], include_contested: bool = False) -> dict[str, set[str]]:
    """Undirected adjacency over ALL KEPs (node ids per `node_id`).

    Edges: see-also and replaces (source -> target), superseded-by (target -> source). Direction is irrelevant once the
    graph is undirected; self references are dropped. With `include_contested`, the links of `contested_edges` are
    added too (used only to find targets whose answers depend on them).
    """
    resolve = make_resolver(keps)
    adj: dict[str, set[str]] = {node_id(k): set() for k in keps}
    edges = []
    for kep in keps:
        src = node_id(kep)
        for field_name in REF_FIELDS:
            for ref in kep.refs.get(field_name, []):
                edges.append((src, resolve(ref)))
    if include_contested:
        edges += [(src, tgt) for src, _, tgt in contested_edges(keps)]
    for src, tgt in edges:
        if tgt and tgt != src:
            adj[src].add(tgt)
            adj[tgt].add(src)
    return adj


def reach(adj: dict[str, set[str]], start: str, max_hops: int) -> dict[str, int]:
    """Minimum hop distance of every other node within `max_hops` of `start` (breadth-first; `start` is excluded)."""
    dist = {start: 0}
    frontier = [start]
    for hop in range(1, max_hops + 1):
        nxt: list[str] = []
        for node in frontier:
            for neighbour in sorted(adj.get(node, ())):
                if neighbour not in dist:
                    dist[neighbour] = hop
                    nxt.append(neighbour)
        if not nxt:
            break
        frontier = nxt
    del dist[start]
    return dist


def source_commit(enhancements_dir: Path) -> str:
    out = subprocess.run(
        ["git", "-C", str(enhancements_dir), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def write_gold(gold: dict, out_path: Path) -> None:
    """Write a gold file as UTF-8 with LF newlines and print a per-kind summary."""
    out_path.write_text(json.dumps(gold, indent=2) + "\n", encoding="utf-8", newline="\n")
    kinds = Counter(q["kind"] for q in gold["questions"])
    print(f"wrote {out_path} ({len(gold['questions'])} questions, commit {gold['source_commit'][:12]})")
    for kind, n in sorted(kinds.items()):
        print(f"  {kind}: {n}")
