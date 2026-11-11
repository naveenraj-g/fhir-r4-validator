#!/usr/bin/env python3
"""Differential test: send the same FHIR resources to the Java wrapper and to this service and compare.

Usage (both services running):

    python tools/compare_with_java.py --java http://localhost:4567 --py http://localhost:4568 \
        --dir /path/to/examples-json --limit 500

``--dir`` can hold any FHIR JSON files, e.g. the official examples (https://hl7.org/fhir/R4/examples-json.zip) or
the ``package/example`` folder of an IG tarball. Add ``--mutate`` to also corrupt each resource (drop each top-level
element, add an unknown property, break ``status``) and compare those too.

Prefer running both services the same way. If the Java service uses a terminology server, its answers depend on the
server's state, so start both with ``DISABLE_TX`` set for a clean comparison.

Only the standard library is used.
"""
from __future__ import annotations

import argparse
import collections
import copy
import glob
import json
import os
import re
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor


def post(base: str, body: bytes, profile: str | None, timeout: float) -> dict:
    url = base.rstrip("/") + "/validate" + (f"?profile={profile}" if profile else "")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=body, method="POST"), timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"issue": [{"severity": "HTTP", "code": str(e.code), "details": {"text": f"HTTP {e.code}"}}]}
    except Exception as e:  # noqa: BLE001
        return {"issue": [{"severity": "HTTP", "code": "error", "details": {"text": str(e)[:80]}}]}


def issues(oo: dict) -> list[tuple[str, str, str, str]]:
    out = []
    for i in oo.get("issue", []):
        text = (i.get("details") or {}).get("text") or i.get("diagnostics") or ""
        out.append((i["severity"], i.get("code", ""), (i.get("expression") or ["-"])[0], text))
    return out


def cases(directory: str, limit: int, mutate: bool):
    files = sorted(glob.glob(os.path.join(directory, "*.json")))
    n = 0
    for f in files:
        try:
            d = json.load(open(f))
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(d, dict) or "resourceType" not in d or os.path.getsize(f) > 200_000:
            continue
        yield os.path.basename(f), "as-is", d
        if mutate:
            for k in list(d):
                if k not in ("resourceType", "meta", "id"):
                    m = copy.deepcopy(d)
                    del m[k]
                    yield os.path.basename(f), f"drop {k}", m
            m = copy.deepcopy(d)
            m["zzUnknown"] = 1
            yield os.path.basename(f), "unknown property", m
            if isinstance(d.get("status"), str):
                m = copy.deepcopy(d)
                m["status"] = "zz-bad"
                yield os.path.basename(f), "bad status", m
        n += 1
        if n >= limit:
            return


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--java", default="http://localhost:4567")
    ap.add_argument("--py", default="http://localhost:4568")
    ap.add_argument("--dir", required=True)
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--mutate", action="store_true")
    ap.add_argument("--profile", default=None, help="profile URL(s) to pass to both services")
    ap.add_argument("--timeout", type=float, default=90)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--all-severities", action="store_true", help="compare warnings and information too, not just errors")
    ap.add_argument("--show", type=int, default=10, help="how many difference categories to list")
    a = ap.parse_args()

    work = list(cases(a.dir, a.limit, a.mutate))
    if not work:
        print("no FHIR JSON files found", file=sys.stderr)
        return 2

    def run(c):
        body = json.dumps(c[2]).encode()
        return c, issues(post(a.java, body, a.profile, a.timeout)), issues(post(a.py, body, a.profile, a.timeout))

    with ThreadPoolExecutor(a.threads) as ex:
        results = list(ex.map(run, work))

    levels = ("error", "fatal", "warning", "information") if a.all_severities else ("error", "fatal")
    pick = lambda L: {(x[2], x[3]) for x in L if x[0] in levels}
    usable = [r for r in results if not any(x[0] == "HTTP" for x in r[1])]
    skipped = len(results) - len(usable)
    presence = sum(1 for _, j, p in usable if bool(pick(j)) == bool(pick(p)))
    same = sum(1 for _, j, p in usable if pick(j) == pick(p))
    n = max(len(usable), 1)
    print(f"cases compared: {len(usable)} (java failed on {skipped})")
    print(f"agreement on 'has issues at this level': {100 * presence / n:.1f}%")
    print(f"identical issue sets (path + message): {100 * same / n:.1f}%")

    norm = lambda x: re.sub(r"\[\d+\]", "[n]", re.sub(r"'[^']*'", "'…'", x[1]))[:110]
    only_j, only_p = collections.Counter(), collections.Counter()
    ex_j, ex_p = {}, {}
    for c, j, p in usable:
        for x in pick(j) - pick(p):
            only_j[norm(x)] += 1
            ex_j.setdefault(norm(x), (c[0], c[1], x[0]))
        for x in pick(p) - pick(j):
            only_p[norm(x)] += 1
            ex_p.setdefault(norm(x), (c[0], c[1], x[0]))
    for title, cnt, exs in (("reported only by JAVA", only_j, ex_j), ("reported only by PYTHON", only_p, ex_p)):
        print(f"\n{title}:")
        for k, v in cnt.most_common(a.show):
            print(f"  {v:5d}  {k}\n         e.g. {exs[k]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
