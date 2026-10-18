"""Raw-YAML keyword-search baseline: what a single grep over the kep.yaml files can answer.

The eval compares Quorum (graph + typed queries) with this baseline on the same questions as the gold files in
`evals/lookups/` and `evals/multihop/`. The baseline is meant to be a fair, plain text search, not a strawman, and it
is deliberately blind to everything that needs structure. In plain words:

What it does
* It reads the kep.yaml files as text. It does not import a YAML parser, `ingest/`, `agent/`, falkordb or the gold
  helpers, and it keeps no graph.
* It has exactly three primitives, and every answer is built from them:
  (a) find the files whose text contains a token (a substring or a regex on the raw text; files are addressed by their
      path, as in `grep -rl` output, so a reference like /keps/sig-cli/2386-foo/ is followed by opening that path);
  (b) read the first `key: value` line of a top-level key;
  (c) read the lines under a top-level key up to the next top-level key.
* It does one retrieval round per question. For a multi-hop question that round returns the 1-hop neighbourhood of
  the target: the files whose see-also / replaces / superseded-by blocks mention the target's folder name, plus the
  files named in the target's own blocks. It then reduces that set the way the question asks (list the ids, count
  them, keep the active ones, list their SIGs).
* A KEP's id is the number on its `kep-number:` line, or "<sig-dir>/<folder>" when that is 0 or missing.
* Small text hygiene only: it drops quotes and a trailing `# comment` from a value, lowercases statuses and handles,
  and ignores comment-only lines inside a block.

What it cannot do, so it gets these questions wrong
* Multi-hop reach: it never goes past the first hop, so 2-hop and 3-hop answers come back too small.
* No alias merging of SIG names, no status normalization (a typo such as "imlpemented" or a synonym such as
  "superseded" does not count as a known status; ACTIVE is the raw status in {provisional, implementable,
  implemented}).
* No filtering of approvers: team aliases such as @sig-node-tech-leads and placeholders such as TBD are returned.
* It only follows references that spell out a KEP's folder name. A link to a KEP's enhancement issue
  (.../issues/<n>) or a "KEP-<n>" mention is not followed, and neither is a reference to a folder that has since been
  renamed (the graph repairs those from the number prefix).
* No duplicate-number detection: two files that share a kep-number both keep that number.

Optional check: `python -m evals.baseline --selfcheck` scores the baseline against both gold files.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ENHANCEMENTS = Path(__file__).resolve().parent.parent / "data" / "enhancements"
GOLD_FILES = {
    "lookups": Path(__file__).resolve().parent / "lookups" / "gold.json",
    "multihop": Path(__file__).resolve().parent / "multihop" / "gold.json",
}

REF_KEYS = ("see-also", "replaces", "superseded-by")
ACTIVE_RAW = frozenset({"provisional", "implementable", "implemented"})

_KEY_LINE = re.compile(r"^([A-Za-z0-9_-]+):(.*)$")
_REF_PATH = re.compile(r"keps/([^/\s\"'#?)\],]+)/([^/\s\"'#?)\],]+)")
_HANDLE_JUNK = "\"',;:()[]."


@dataclass(frozen=True)
class Doc:
    """One kep.yaml: where it lives and its raw text."""

    sig_dir: str
    folder: str
    text: str

    @property
    def lines(self) -> list[str]:
        return self.text.split("\n")


def _clean(value: str) -> str:
    """A raw value without a trailing `# comment` and without surrounding quotes."""
    value = re.sub(r"\s+#.*$", "", value).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def first_line(doc: Doc, key: str) -> str | None:
    """Primitive (b): the value on the first top-level `key: value` line, or None when the key is absent."""
    for line in doc.lines:
        m = _KEY_LINE.match(line)
        if m and m.group(1) == key:
            return _clean(m.group(2))
    return None


def block(doc: Doc, key: str) -> list[str]:
    """Primitive (c): the lines under the first top-level `key:` line (the rest of that line included), up to the
    next top-level key. Blank and comment-only lines are dropped."""
    lines = doc.lines
    for i, line in enumerate(lines):
        m = _KEY_LINE.match(line)
        if m and m.group(1) == key:
            out = [m.group(2)]
            for following in lines[i + 1 :]:
                if _KEY_LINE.match(following):
                    break
                out.append(following)
            return [ln for ln in out if ln.strip() and not ln.strip().startswith("#")]
    return []


def _mentions(folder: str) -> re.Pattern[str]:
    """A folder name as a whole token (so 12-foo does not match 12-foo-bar)."""
    return re.compile(rf"(?<![A-Za-z0-9_-]){re.escape(folder)}(?![A-Za-z0-9_-])")


def _id_key(ident: str) -> tuple[int, int, str]:
    return (0, int(ident), "") if ident.isdigit() else (1, 0, ident)


class KeywordSearch:
    """Answers the questions of both gold suites by searching the raw kep.yaml text."""

    def __init__(self, enhancements_dir: Path | str = DEFAULT_ENHANCEMENTS):
        paths = sorted((Path(enhancements_dir) / "keps").glob("*/*/kep.yaml"))
        if not paths:
            raise FileNotFoundError(f"no kep.yaml files under {Path(enhancements_dir) / 'keps'}")
        self.docs = [Doc(p.parent.parent.name, p.parent.name, p.read_text(encoding="utf-8")) for p in paths]
        self._handlers = {
            "owning_sig": self._owning_sig,
            "status": self._status,
            "has_successor": self._has_successor,
            "see_also_referrers": self._see_also_referrers,
            "sig_kep_count": self._sig_kep_count,
            "approvers": self._approvers,
            "hop2_set": self._hop2_set,
            "reachable_count_3hop": self._reachable_count,
            "stale_reach": self._stale_reach,
            "reachable_sigs": self._reachable_sigs,
        }

    def answer(self, kind: str, params: dict):
        """The baseline's answer, in the same shape as the gold: a string, a bool, an int, or a sorted list."""
        if kind not in self._handlers:
            raise KeyError(f"unknown question kind {kind!r}; known: {sorted(self._handlers)}")
        return self._handlers[kind](params)

    # primitive (a)
    def find_files(self, pattern: re.Pattern[str]) -> list[Doc]:
        return [doc for doc in self.docs if pattern.search(doc.text)]

    def _target(self, params: dict) -> Doc:
        number = str(int(params["kep_id"]))
        hits = self.find_files(re.compile(rf"^kep-number:[ \t]*[\"']?0*{number}[\"']?[ \t]*(#.*)?$", re.MULTILINE))
        if not hits:
            raise LookupError(f"no kep.yaml has kep-number {number}")
        return hits[0]

    @staticmethod
    def _ident(doc: Doc) -> str:
        number = first_line(doc, "kep-number") or ""
        if number.isdigit() and int(number) > 0:
            return str(int(number))
        return f"{doc.sig_dir}/{doc.folder}"

    def _mentioning(self, target: Doc, keys: tuple[str, ...]) -> list[Doc]:
        """Files (other than the target) whose `keys` blocks name the target's folder."""
        token = _mentions(target.folder)
        return [
            doc
            for doc in self.find_files(token)
            if doc is not target and any(token.search("\n".join(block(doc, key))) for key in keys)
        ]

    def _neighbourhood(self, target: Doc) -> list[Doc]:
        """The 1-hop neighbourhood: files that name the target in a reference block, plus the files that the target's
        own reference blocks name."""
        found = {(doc.sig_dir, doc.folder): doc for doc in self._mentioning(target, REF_KEYS)}
        for key in REF_KEYS:
            for line in block(target, key):
                for m in _REF_PATH.finditer(line):
                    folder = m.group(2).rstrip("/").removesuffix(".md")
                    for doc in self.docs:
                        if doc.folder == folder and doc is not target:
                            found[(doc.sig_dir, doc.folder)] = doc
        return list(found.values())

    @staticmethod
    def _is_active(doc: Doc) -> bool:
        return (first_line(doc, "status") or "").lower() in ACTIVE_RAW

    # lookups
    def _owning_sig(self, params: dict):
        return first_line(self._target(params), "owning-sig")

    def _status(self, params: dict):
        return (first_line(self._target(params), "status") or "").lower()

    def _has_successor(self, params: dict):
        target = self._target(params)
        if self._mentioning(target, ("replaces",)):
            return True
        return any(_REF_PATH.search(line) for line in block(target, "superseded-by"))

    def _see_also_referrers(self, params: dict):
        target = self._target(params)
        return sorted((self._ident(doc) for doc in self._mentioning(target, ("see-also",))), key=_id_key)

    def _sig_kep_count(self, params: dict):
        pattern = re.compile(rf"^owning-sig:[ \t]*[\"']?{re.escape(params['sig'])}[\"']?[ \t]*(#.*)?$", re.MULTILINE)
        return len(self.find_files(pattern))

    def _approvers(self, params: dict):
        items: list[str] = []
        for line in block(self._target(params), "approvers"):
            text = line.strip()
            if text.startswith("-"):  # a list item
                items.append(text[1:])
            elif text.startswith("["):  # an inline list: approvers: [@a, @b]
                items += text.strip("[]").split(",")
        handles = set()
        for item in items:
            parts = re.sub(r"\s+#.*$", "", item).strip().strip("\"'").lstrip("@").split()
            handle = parts[0].strip(_HANDLE_JUNK).lstrip("@").lower() if parts else ""
            if handle:
                handles.add(handle)
        return sorted(handles)

    # multi-hop: one retrieval round, so the 1-hop neighbourhood is all there is to reduce
    def _hop2_set(self, params: dict):
        return sorted((self._ident(doc) for doc in self._neighbourhood(self._target(params))), key=_id_key)

    def _reachable_count(self, params: dict):
        return len(self._neighbourhood(self._target(params)))

    def _active_neighbours(self, params: dict) -> list[Doc]:
        return [doc for doc in self._neighbourhood(self._target(params)) if self._is_active(doc)]

    def _stale_reach(self, params: dict):
        return sorted((self._ident(doc) for doc in self._active_neighbours(params)), key=_id_key)

    def _reachable_sigs(self, params: dict):
        return sorted({sig for doc in self._active_neighbours(params) if (sig := first_line(doc, "owning-sig"))})


def _same(answer, gold) -> bool:
    return sorted(answer) == sorted(gold) if isinstance(gold, list) else answer == gold


def selfcheck(enhancements_dir: Path) -> int:
    """Score the baseline against every gold file that exists; print correct/total per kind."""
    search = KeywordSearch(enhancements_dir)
    ran = 0
    for suite, path in GOLD_FILES.items():
        if not path.is_file():
            print(f"{suite}: no gold file at {path} (run `python -m evals.{suite}.make_gold`)")
            continue
        ran += 1
        questions = json.loads(path.read_text(encoding="utf-8"))["questions"]
        by_kind: dict[str, list[int]] = {}
        for q in questions:
            ok = _same(search.answer(q["kind"], q["params"]), q["gold"])
            tally = by_kind.setdefault(q["kind"], [0, 0])
            tally[0] += ok
            tally[1] += 1
        total_ok = sum(t[0] for t in by_kind.values())
        print(f"{suite}: {total_ok}/{len(questions)} correct")
        for kind, (ok, n) in sorted(by_kind.items()):
            print(f"  {kind:22s} {ok}/{n}")
    return 0 if ran else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Raw-YAML keyword-search baseline for the Quorum eval.")
    parser.add_argument("--selfcheck", action="store_true", help="score the baseline against both gold files")
    parser.add_argument("--enhancements", type=Path, default=DEFAULT_ENHANCEMENTS, help="path of the cloned corpus")
    args = parser.parse_args(argv)
    if not args.selfcheck:
        parser.print_help()
        return 0
    return selfcheck(args.enhancements)


if __name__ == "__main__":
    sys.exit(main())
