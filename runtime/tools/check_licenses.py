"""Licence allow-list check for the installed Python environment (no third-party dependencies).

Reads every installed distribution through ``importlib.metadata`` and resolves its licence from, in order,
``License-Expression`` (PEP 639), the ``License`` field, and the ``License :: OSI Approved :: …`` classifiers.
SPDX expressions are understood: ``A AND B`` requires every part to be allowed, ``A OR B`` requires one.
The allow-list mirrors docs/LICENSES.md (class "разрешено без ограничений").

    python tools/check_licenses.py                 # exit 1 on the first disallowed licence
    python tools/check_licenses.py --md licenses.md --ignore calls
"""

from __future__ import annotations

import argparse
import re
import sys
from importlib import metadata
from pathlib import Path

ALLOWED = {
    # SPDX ids
    "mit", "mit-0", "bsd-2-clause", "bsd-3-clause", "0bsd", "apache-2.0", "psf-2.0", "isc", "mpl-2.0", "unlicense", "zlib",
    "cc0-1.0", "hpnd", "python-2.0", "bsd-3-clause-clear",
    # classifier / free-text spellings
    "mit license", "bsd", "bsd license", "new bsd", "new bsd license", "apache software license", "apache 2.0", "apache license 2.0",
    "apache license, version 2.0", "apache", "python software foundation license", "isc license (iscl)", "isc license",
    "mozilla public license 2.0 (mpl 2.0)", "the unlicense (unlicense)", "public domain", "psf", "psfl", "zlib/libpng license",
    "historical permission notice and disclaimer (hpnd)", "dual license",
}
IGNORED_TOKENS = {"dfsg approved", "osi approved", "", "unknown"}  # not licences – classifiers or missing metadata


def norm(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s.strip("()")


def licence_of(dist: metadata.Distribution) -> str:
    md = dist.metadata
    expr = md.get("License-Expression")
    if expr:
        return str(expr)
    classifiers = [c for c in md.get_all("Classifier", []) if c.startswith("License ::")]
    if classifiers:
        parts = [c.split("::")[-1].strip() for c in classifiers]
        return " AND ".join(parts) if len(parts) == 1 else " OR ".join(parts)  # several classifiers = dual licensing
    lic = md.get("License")
    if lic and len(lic) < 200 and "\n" not in lic:
        return str(lic)
    return "UNKNOWN"


def allowed(expr: str) -> tuple[bool, list[str]]:
    """Evaluate an SPDX-like expression; returns (ok, offending tokens)."""
    text = expr.replace("(", " ").replace(")", " ")
    bad: list[str] = []
    for alternative in re.split(r"\s+OR\s+|;", text, flags=re.I):  # any alternative suffices
        conj = [norm(t) for t in re.split(r"\s+AND\s+|,", alternative, flags=re.I)]
        conj = [t for t in conj if t not in IGNORED_TOKENS]
        if not conj:
            continue
        missing = [t for t in conj if t not in ALLOWED and t.removesuffix(" license") not in ALLOWED]
        if not missing:
            return True, []
        bad.extend(missing)
    if not bad and norm(text) in IGNORED_TOKENS:
        return False, ["UNKNOWN"]
    return False, sorted(set(bad))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ignore", nargs="*", default=[], help="distribution names to skip")
    ap.add_argument("--md", type=Path, default=None, help="write a markdown inventory")
    ap.add_argument("--allow-unknown", action="store_true", help="do not fail on distributions without licence metadata")
    ns = ap.parse_args(argv)

    rows = []
    failures = []
    seen = set()
    for dist in sorted(metadata.distributions(), key=lambda d: d.metadata["Name"].lower()):
        name = dist.metadata["Name"]
        if name.lower() in seen or name.lower() in {n.lower() for n in ns.ignore}:
            continue
        seen.add(name.lower())
        expr = licence_of(dist)
        ok, bad = allowed(expr)
        if expr == "UNKNOWN" and ns.allow_unknown:
            ok = True
        rows.append((name, dist.version, expr, ok))
        if not ok:
            failures.append(f"{name} {dist.version}: {expr} (not allowed: {', '.join(bad)})")

    width = max(len(r[0]) for r in rows) if rows else 10
    for name, ver, expr, ok in rows:
        print(f"{'OK ' if ok else 'BAD'} {name:{width}} {ver:12} {expr}")
    if ns.md:
        ns.md.parent.mkdir(parents=True, exist_ok=True)
        lines = ["| Package | Version | Licence | Allowed |", "|---|---|---|---|"]
        lines += [f"| {n} | {v} | {e} | {'✅' if ok else '❌'} |" for n, v, e, ok in rows]
        ns.md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if failures:
        print("\nDisallowed licences:\n  " + "\n  ".join(failures), file=sys.stderr)
        return 1
    print(f"\n{len(rows)} distributions, all licences allowed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
