"""Move the registry's self-count to the number of claims it actually holds, in one command.

`claims.registered` is the claim whose subject is the registry itself, and every time a claim
is added or removed it drifts: the pages print the old number, the checker reports DRIFTED or
STALE_ALSO_PRINTED, and the fix is a hand edit in three places that is easy to get wrong. It
went wrong repeatedly on 2026-08-26, once badly enough to publish a corrupted needle
("53 probes, 53 caught"), and twice it left the whole registry red on a clean tree, which in
turn made the two mutation probes of `claims_check.py` INVALID because their covering test
could not be green.

This does the three edits together and nothing else: set `expected` and `printed_as` on
`claims.registered`, push the old phrase onto `superseded`, and rewrite the phrase on every
page the claim says it is quoted in. It reads the page list from the claim rather than from a
list typed here, so a new page is picked up automatically.

It is deliberately NOT part of `claims_check.py`. A checker that repairs the claim it is
checking cannot fail, and the whole point of the registry is that a number nobody maintained
goes red. This is a maintenance command a person runs, and the checker still has to agree
afterwards.

    .venv/bin/python evalx/sync_registry_count.py            # show what would change
    .venv/bin/python evalx/sync_registry_count.py --write    # do it

Then run `evalx/claims_check.py` as usual; this does not write claims-check.json.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
REGISTRY = _ROOT / "evalx" / "claims.json"
CLAIM_ID = "claims.registered"
PHRASE = re.compile(r"\b\d{1,4} claims registered")


def plan() -> dict:
    doc = json.loads(REGISTRY.read_text())
    claims = doc["claims"]
    total = len(claims)
    claim = next((c for c in claims if c["id"] == CLAIM_ID), None)
    if claim is None:
        raise SystemExit(f"{CLAIM_ID} is not in the registry; nothing to sync")
    wanted = f"{total} claims registered"
    pages = []
    for rel in claim.get("quoted_in", []):
        page = _ROOT / rel
        if not page.exists():
            pages.append({"page": rel, "status": "MISSING"})
            continue
        text = page.read_text()
        found = sorted(set(PHRASE.findall(text)))
        stale = [f for f in found if f != wanted]
        pages.append({"page": rel, "found": found, "stale": stale,
                      "status": "STALE" if stale else "OK"})
    return {"total": total, "expected_now": claim["expected"], "wanted": wanted,
            "claim_stale": claim["expected"] != str(total), "pages": pages,
            "doc": doc, "claim": claim}


def apply(p: dict) -> list:
    changed = []
    claim, total, wanted = p["claim"], p["total"], p["wanted"]
    if p["claim_stale"]:
        superseded = set(claim.get("superseded") or [])
        superseded.add(claim["printed_as"])
        superseded.discard(wanted)          # what is current is never also superseded
        claim["expected"] = str(total)
        claim["printed_as"] = wanted
        claim["superseded"] = sorted(superseded)
        REGISTRY.write_text(json.dumps(p["doc"], indent=1, ensure_ascii=False) + "\n")
        changed.append(f"registry: {CLAIM_ID} -> {total}")
    for row in p["pages"]:
        if row["status"] != "STALE":
            continue
        page = _ROOT / row["page"]
        page.write_text(PHRASE.sub(wanted, page.read_text()))
        changed.append(f"{row['page']}: {', '.join(row['stale'])} -> {wanted}")
    return changed


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="apply the edits")
    args = ap.parse_args(argv)
    p = plan()
    print(f"registry holds {p['total']} claims; {CLAIM_ID} says {p['expected_now']}")
    for row in p["pages"]:
        print(f"  {row['status']:<7} {row['page']}  {row.get('found', '')}")
    if not args.write:
        todo = p["claim_stale"] or any(r["status"] == "STALE" for r in p["pages"])
        print("\nin sync" if not todo else "\nout of sync; re-run with --write")
        return 0
    changed = apply(p)
    print("\n" + ("\n".join(changed) if changed else "nothing to change"))
    print("now run evalx/claims_check.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
