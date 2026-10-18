"""Pure, deterministic parsing of kubernetes/enhancements kep.yaml files into graph rows.

No network, no database, no LLM. Every normalization is explicit and counted in `stats`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

import yaml

from ingest.config import ISSUE_URL, REPO_TREE_URL

ACTIVE = ("provisional", "implementable", "implemented")
STALE = ("replaced", "rejected", "withdrawn", "deferred")
# Explicit, logged fixes for values seen in the real corpus (raw value is kept in status_raw).
STATUS_ALIASES = {"imlpemented": "implemented", "superseded": "replaced", "removed": "withdrawn"}

# Spelling variants of the same SIG found in the corpus; merged so "2+ SIGs" rules don't double count.
SIG_ALIASES = {
    "sig-apimachinery": "sig-api-machinery",
    "sig-machinery": "sig-api-machinery",
    "sig-arch": "sig-architecture",
    "sig-contribex": "sig-contributor-experience",
    "sig-scheduler": "sig-scheduling",
}

NULLISH = {"", "tbd", "n/a", "na", "none", "todo", "-", "~", "null", "tbd.", "nil"}
_HANDLE_AT = re.compile(r"(?:^|[\s(\[,<\"'])@([A-Za-z0-9][A-Za-z0-9-]*)")
_BARE_HANDLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")
_MILESTONE = re.compile(r"^v?1\.(\d+)")
_KEP_PATH = re.compile(r"keps/([^/\s#)]+)/([^/\s#)\"']+)")
_ISSUE = re.compile(r"enhancements/issues/(\d+)")
_KEP_NUM = re.compile(r"\bKEP[- ]?#?(\d+)\b", re.IGNORECASE)


def load_yaml(text: str) -> dict:
    """BaseLoader keeps every scalar a string: real files contain impossible dates and `1.20`-style milestones."""
    data = yaml.load(text, Loader=yaml.BaseLoader)  # noqa: S506 - BaseLoader constructs only str/list/dict
    return data if isinstance(data, dict) else {}


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v is not None and not isinstance(v, (list, dict))]
    if isinstance(value, dict):
        return []
    return [str(value)]


def is_nullish(value) -> bool:
    s = str(value or "").strip().strip("\"'").strip().lower()
    return s in NULLISH or s.startswith("tbd")


def normalize_status(raw) -> str:
    s = str(raw or "").strip().lower()
    if s in ACTIVE or s in STALE:
        return s
    if s in STATUS_ALIASES:
        return STATUS_ALIASES[s]
    m = re.match(r"^([a-z]+)\s*\(", s)  # e.g. "implemented (alpha)"
    if m and (m.group(1) in ACTIVE or m.group(1) in STALE):
        return m.group(1)
    return "unknown"


_GROUP_ALIAS = re.compile(r"^(sig|wg|committee)-|-(leads|tech-leads|approvers|reviewers|maintainers)$")


def is_group_alias(handle: str) -> bool:
    """GitHub team aliases like @sig-node-tech-leads are groups, not people; they are skipped and counted."""
    return bool(_GROUP_ALIAS.search(handle))


def normalize_handle(raw) -> str | None:
    """'@Foo' -> 'foo'; 'Jane Doe (@jdoe)' -> 'jdoe'; TBD/empty/group alias -> None."""
    if is_nullish(raw):
        return None
    s = str(raw).strip()
    m = _HANDLE_AT.search(s)
    handle = m.group(1).lower() if m else None
    bare = s.strip("\"' ")
    if handle is None and _BARE_HANDLE.fullmatch(bare):
        handle = bare.lower()
    if handle and is_group_alias(handle):
        return None
    return handle


def normalize_sig(raw) -> str | None:
    if is_nullish(raw):
        return None
    name = str(raw).strip().strip("\"'").lower()
    return SIG_ALIASES.get(name, name) or None


def milestone_minor(raw) -> int | None:
    m = _MILESTONE.match(str(raw or "").strip())
    return int(m.group(1)) if m else None


def parse_kep_file(path: Path, keps_root: Path) -> dict:
    """One kep.yaml -> one raw-but-normalized record (no ids or edges yet)."""
    data = load_yaml(path.read_text(encoding="utf-8"))
    rel = path.parent.relative_to(keps_root).as_posix()  # "<sig-dir>/<folder>"
    folder_sig, folder = rel.split("/", 1)
    number_raw = str(data.get("kep-number") or "").strip().strip("\"'")
    number = str(int(number_raw)) if number_raw.isdigit() and int(number_raw) > 0 else None
    record = {
        "path": rel,
        "folder": folder,
        "folder_sig": folder_sig,
        "number": number,
        "number_raw": number_raw,
        "title": str(data.get("title") or "").strip(),
        "status_raw": str(data.get("status") or "").strip(),
        "status": normalize_status(data.get("status")),
        "stage": (str(data.get("stage")).strip() or None) if data.get("stage") else None,
        "latest_milestone": str(data.get("latest-milestone") or "").strip() or None,
        "latest_minor": milestone_minor(data.get("latest-milestone")),
        "creation_date": str(data.get("creation-date") or "").strip() or None,
        "owning_sig": normalize_sig(data.get("owning-sig")),
        "participating_sigs": sorted({s for s in map(normalize_sig, _as_list(data.get("participating-sigs"))) if s}),
        "see_also_raw": _as_list(data.get("see-also")),
        "replaces_raw": _as_list(data.get("replaces")),
        "superseded_by_raw": _as_list(data.get("superseded-by")),
        "prr_approvers_raw": _as_list(data.get("prr-approvers")),
        "skipped_handles": 0,
    }
    for field in ("authors", "reviewers", "approvers"):
        handles = []
        for raw in _as_list(data.get(field)):
            h = normalize_handle(raw)
            if h:
                if h not in handles:
                    handles.append(h)
            else:
                record["skipped_handles"] += 1
        record[field] = handles
    return record


def parse_corpus(enhancements_dir: Path) -> tuple[list[dict], list[dict]]:
    """Parse every keps/*/*/kep.yaml. Returns (records, failures); failures are reported, never dropped silently."""
    keps_root = enhancements_dir / "keps"
    records, failures = [], []
    for path in sorted(keps_root.glob("*/*/kep.yaml")):
        try:
            records.append(parse_kep_file(path, keps_root))
        except Exception as exc:  # noqa: BLE001 - every failure is surfaced in stats
            failures.append({"file": path.relative_to(enhancements_dir).as_posix(), "error": str(exc)[:200]})
    return records, failures


def assign_ids(records: list[dict]) -> dict[str, int]:
    """id = kep-number if valid and unique; otherwise '<sig-dir>/<folder>'. Returns stats."""
    by_number: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        if r["number"]:
            by_number[r["number"]].append(r)
    stats = Counter()
    for r in records:
        r["id"] = r["path"]
        if not r["number"]:
            stats["ids_without_valid_number"] += 1
            continue
        group = by_number[r["number"]]
        if len(group) == 1:
            r["id"] = r["number"]
            continue
        owners = [g for g in group if g["folder"].startswith(r["number"] + "-")]
        if len(owners) == 1 and owners[0] is r:
            r["id"] = r["number"]
        else:
            stats["ids_from_duplicate_number"] += 1
    return dict(stats)


def resolve_ref(ref: str, folder_index: dict[str, str], number_index: dict[str, str]) -> str | None:
    s = str(ref).strip()
    m = _KEP_PATH.search(s)
    if m:
        folder = m.group(2).rstrip("/")
        folder = folder[:-3] if folder.endswith(".md") else folder
        if folder in folder_index:
            return folder_index[folder]
        lead = re.match(r"^(\d+)-", folder)
        if lead:
            return number_index.get(str(int(lead.group(1))))
        return None
    for pattern in (_ISSUE, _KEP_NUM):
        m = pattern.search(s)
        if m:
            return number_index.get(str(int(m.group(1))))
    return None


def build_rows(records: list[dict], source_commit: str | None = None) -> dict:
    """Records -> node/edge rows ready for UNWIND ... MERGE, plus stats and a deterministic data hash."""
    stats: Counter = Counter()
    stats.update(assign_ids(records))
    folder_index = {r["folder"]: r["id"] for r in records}
    number_index = {r["id"]: r["id"] for r in records if r["id"] == r["number"]}

    keps, sigs, people = [], set(), set()
    edges: dict[str, set] = {k: set() for k in ("AUTHORED", "REVIEWED", "APPROVED", "OWNED_BY",
                                                "PARTICIPATES", "SEE_ALSO", "REPLACES")}
    for r in sorted(records, key=lambda x: x["id"]):
        unresolved = []
        for field, rel, reverse in (("see_also_raw", "SEE_ALSO", False), ("replaces_raw", "REPLACES", False),
                                    ("superseded_by_raw", "REPLACES", True)):
            for ref in r[field]:
                if is_nullish(ref):
                    stats[f"{field[:-4]}_placeholder"] += 1
                    continue
                target = resolve_ref(ref, folder_index, number_index)
                if target is None:
                    stats[f"{field[:-4]}_unresolved"] += 1
                    unresolved.append(ref.strip())
                elif target == r["id"]:
                    stats[f"{field[:-4]}_self_reference"] += 1
                else:
                    stats[f"{field[:-4]}_resolved"] += 1
                    edges[rel].add((target, r["id"]) if reverse else (r["id"], target))
        if r["owning_sig"]:
            sigs.add(r["owning_sig"])
            edges["OWNED_BY"].add((r["id"], r["owning_sig"]))
        else:
            stats["missing_owning_sig"] += 1
        for s in r["participating_sigs"]:
            sigs.add(s)
            edges["PARTICIPATES"].add((r["id"], s))
        for field, rel in (("authors", "AUTHORED"), ("reviewers", "REVIEWED"), ("approvers", "APPROVED")):
            if not r[field]:
                stats[f"no_{field}"] += 1
            for h in r[field]:
                people.add(h)
                edges[rel].add((h, r["id"]))
        stats["skipped_handles"] += r["skipped_handles"]
        if r["prr_approvers_raw"]:
            stats["with_prr_approvers"] += 1
        if r["status"] == "unknown" or r["status"] != r["status_raw"].strip().lower():
            stats["status_normalized"] += 1
        if r["owning_sig"] and r["owning_sig"] != r["folder_sig"]:
            stats["folder_sig_differs_from_owning_sig"] += 1
        keps.append({
            "id": r["id"],
            "path": r["path"],
            "number": r["number"],
            "title": r["title"],
            "status": r["status"],
            "status_raw": r["status_raw"],
            "stage": r["stage"],
            "latest_milestone": r["latest_milestone"],
            "latest_minor": r["latest_minor"],
            "creation_date": r["creation_date"],
            "owning_sig": r["owning_sig"],
            "source_url": f"{REPO_TREE_URL}keps/{r['path']}",
            "issue_url": f"{ISSUE_URL}{r['number']}" if r["number"] else None,
            "unresolved_refs": unresolved,
        })

    rows = {
        "keps": keps,
        "sigs": [{"name": s} for s in sorted(sigs)],
        "people": [{"handle": h} for h in sorted(people)],
        "edges": {rel: [{"src": a, "dst": b} for a, b in sorted(pairs)] for rel, pairs in edges.items()},
    }
    minors = [k["latest_minor"] for k in keps if k["latest_minor"] is not None]
    meta = {
        "data_hash": hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(),
        "source_commit": source_commit,
        "max_minor": max(minors) if minors else None,
        "kep_count": len(keps),
        "person_count": len(people),
        "sig_count": len(sigs),
        "edge_count": sum(len(v) for v in edges.values()),
    }
    stats["keps"] = len(keps)
    return {"rows": rows, "meta": meta, "stats": dict(sorted(stats.items()))}
