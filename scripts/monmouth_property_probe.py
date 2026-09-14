"""Monmouth probate — can "decedent name + town" become a property?

*** DEMOTED 2026-08-26. Do not use this as the primary resolver. ***
A live county-data-cleaner run (njpropertyrecords current-owner search) over
this probe's first 5 samples resolved 3 and REFUTED one of its positives:

  - Long-held property has NO recorded sale, so the deed index cannot see it
    at all — and that is precisely the probate demographic. This probe
    reported "nothing at all" for a decedent whose $800k parcel an owner
    search found immediately.
  - A historical block/lot does not address a CURRENT assessor map. A real
    2003 deed naming the decedent at West Long Branch block 18 lot 3
    resolves today to a property she never owned; towns renumber parcels at
    revaluation. Never carry a deed block/lot into a record as an address
    without the named town's assessor confirming it.
  - Common names only collapse under a town-scoped current-owner search.

So this script is a cross-check for RECENT transfers, nothing more. Its
RESOLVED verdicts are not evidence and its negatives are not either.
Full detail in output/monmouth_recon/FINDINGS.txt.


The surrogate index (scripts/monmouth_surrogate_probe.py) gives us decedent
first/last, town, case type, DOD and docket — and nothing else. Unlike the
Bluestone counties there is NO executor/PR name and NO decedent mailing
address online; the "Select" link is a $10 mail-order request form. So every
Monmouth record would enter the pipeline with no address and no decision
maker, and both would have to be manufactured downstream.

Two candidate resolvers were checked live on 2026-08-26:

  taxrecords-nj.com  DEAD END for Monmouth. MONMOUTH (select_cc=1301) IS in
                     the county dropdown and district 1300 = ALL, but the free
                     `ctb` tier holds zero Monmouth rows: owner=SMITH,
                     p_loc=MAIN and a per-municipality sweep all return an
                     authoritative "0 Records Found" while the Middlesex
                     control returns 627. taxrecords-nj.com's own landing page
                     links Monmouth OUT to the county tax board instead of
                     serving it. Same shape as Essex.

  OPRS clerk index   VIABLE. Monmouth's Open Public Records Search exposes the
                     County Clerk's recorded-instrument index over plain HTTP
                     with a first/last name search, and the result grid carries
                     Town Name + Block + Lot. A decedent who appears as the
                     INDIRECT party (grantee) on a deed is the person the
                     property was conveyed TO — i.e. the owner.

So the measurable question is: for real recent-DOD Monmouth decedents, what
fraction resolve to exactly one block/lot in the town the surrogate named?
That rate is the ceiling on Monmouth probate as a lead source. This script
measures it and does nothing else — it writes no CRM record and uploads
nothing.

    PYTHONPATH=src python scripts/monmouth_property_probe.py \
        --csv output/monmouth_recon/decedents_20260826_120000.csv

Output: output/monmouth_recon/resolution_{ts}.csv + a printed rate breakdown.
"""
from __future__ import annotations

import argparse
import csv
import random
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import requests

OPRS_URL = "https://oprs.co.monmouth.nj.us/oprs/clerk/clerkhome.aspx?op=basic"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
OUT_DIR = Path(__file__).resolve().parents[1] / "output" / "monmouth_recon"
TIMEOUT = 90
P = "ctl00$ContentPlaceHolder1$"

# Verified 2026-08-26: the results grid is a plain <td> table whose first
# row is this header. Columns are read BY NAME off that row, so a reordering
# upstream shows up as a missing column rather than silently shifted data.
EXPECTED_COLS = ("Type", "Direct Party", "Indirect Party", "Instrument #",
                 "Recorded", "Town Name", "Block", "Lot")

# ddlShowRecTab1 = rows per page (20/50/100); ddlTotalRecTab1 = hard cap
# (500/1000/2000). Take the max of both so a cap hit means a real name
# collision, not a default we forgot to raise.
PAGE_SIZE, TOTAL_CAP = "100", "2000"

# Municipality suffixes, mapped to a class. The surrogate index writes the
# full legal name ("LONG BRANCH CITY", "HAZLET TOWNSHIP"); the deed index
# writes a shorter form ("LONG BRANCH", "FREEHOLD TWP"). So the core name and
# the suffix CLASS are compared separately — stripping suffixes outright would
# merge NEPTUNE CITY into NEPTUNE TOWNSHIP, which are different municipalities.
_SUFFIX_CLASS = {
    "BOROUGH": "B", "BORO": "B",
    "TOWNSHIP": "T", "TWP": "T",
    "CITY": "C", "VILLAGE": "V", "TOWN": "W",
}
_SUFFIX = re.compile(r"\b(%s)\b" % "|".join(_SUFFIX_CLASS))
_NAME_NOISE = re.compile(r"\b(JR|SR|II|III|IV|MRS|MR|ESTATE|OF|THE)\b")


class ProbeError(RuntimeError):
    """The source did not behave the way the recon documented."""


# ── name / town normalisation ────────────────────────────────────────────
def norm_town(t: str) -> tuple[str, str]:
    """Split a municipality string into (core name, suffix class).

    "HAZLET TOWNSHIP"      -> ("HAZLET", "T");  "HAZLET" -> ("HAZLET", "")
    "NEPTUNE CITY"         -> ("NEPTUNE", "C"); "NEPTUNE TOWNSHIP" -> ("NEPTUNE", "T")
    "NEPTUNE CITY BOROUGH" -> ("NEPTUNE", "C")  — the legal form is dropped,
                                                  the naming half is kept.
    """
    tokens = re.sub(r"[^A-Z ]", " ", (t or "").upper()).split()
    popped = []
    while tokens and tokens[-1] in _SUFFIX_CLASS:
        popped.append(_SUFFIX_CLASS[tokens.pop()])
    # "NEPTUNE CITY BOROUGH" pops BOROUGH (the legal form) then CITY — and
    # CITY is the half that names the municipality, so the innermost suffix
    # popped is the one that distinguishes it from NEPTUNE TOWNSHIP.
    return " ".join(tokens), (popped[-1] if popped else "")


def towns_match(a: str, b: str) -> bool:
    """Same municipality? Cores must be equal; a missing suffix on either side
    is treated as compatible, but two DIFFERENT suffixes are not — so
    NEPTUNE CITY never matches NEPTUNE TOWNSHIP."""
    (core_a, cls_a), (core_b, cls_b) = norm_town(a), norm_town(b)
    if not core_a or core_a != core_b:
        return False
    return not (cls_a and cls_b) or cls_a == cls_b


def name_tokens(s: str) -> set[str]:
    s = re.sub(r"[^A-Z ]", " ", (s or "").upper())
    s = _NAME_NOISE.sub("", s)
    return {t for t in s.split() if len(t) > 1}


def party_is_decedent(party: str, first: str, last: str) -> bool:
    """OPRS normalises parties to LAST FIRST MIDDLE; require both names.

    Middle names and suffixes differ constantly between the surrogate index
    and the deed index, so this is a subset test, not equality.
    """
    p = name_tokens(party)
    want_last = name_tokens(last)
    want_first = {t for t in name_tokens(first) if len(t) > 1}
    if not want_last or not want_first:
        return False
    return want_last <= p and bool(want_first & p)


# ── OPRS ─────────────────────────────────────────────────────────────────
def _hidden(html: str, name: str) -> str:
    m = re.search(r'name="%s"[^>]*value="([^"]*)"' % re.escape(name), html)
    return m.group(1) if m else ""


def _cells(row_html: str) -> list[str]:
    return [
        re.sub(r"\s+", " ", re.sub(r"<[^>]*>", "", c)).replace("\xa0", "").strip()
        for c in re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S)
    ]


def oprs_search(last: str, first: str, from_date: str, to_date: str) -> tuple[list[dict], bool]:
    """One name search. Returns (rows as dicts keyed by column name, cap_hit)."""
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    r = s.get(OPRS_URL, timeout=TIMEOUT)
    r.raise_for_status()
    if P + "txtLastNameTab1" not in r.text:
        raise ProbeError("OPRS name-search form not found — the portal changed.")

    data = {
        "__EVENTTARGET": "", "__EVENTARGUMENT": "",
        "__VIEWSTATE": _hidden(r.text, "__VIEWSTATE"),
        "__VIEWSTATEGENERATOR": _hidden(r.text, "__VIEWSTATEGENERATOR"),
        "__EVENTVALIDATION": _hidden(r.text, "__EVENTVALIDATION"),
        P + "txtLastNameTab1": last,
        P + "txtFirstNameTab1": first,
        P + "txtFromTab1": from_date,
        P + "txtToTab1": to_date,
        P + "ddlShowRecTab1": PAGE_SIZE,
        P + "ddlTotalRecTab1": TOTAL_CAP,
        P + "ddlDocTypeTab1": "",       # all types; DEEDs filtered below
        P + "btnSearchTab1": "Search",
    }
    r2 = s.post(OPRS_URL, data=data, timeout=TIMEOUT)
    r2.raise_for_status()

    header: list[str] | None = None
    rows: list[dict] = []
    for raw in re.findall(r"<tr[^>]*>(.*?)</tr>", r2.text, re.S):
        c = _cells(raw)
        if len(c) < 8:
            continue
        if header is None:
            if tuple(c[:len(EXPECTED_COLS)]) == EXPECTED_COLS:
                header = c
            continue
        rows.append(dict(zip(header, c)))

    if header is None and rows:
        raise ProbeError("OPRS results table had no recognisable header row.")
    plain = re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", r2.text))
    m = re.search(r"([\d,]+)\s*record", plain, re.I)
    cap_hit = bool(m) and m.group(1).replace(",", "") == TOTAL_CAP
    return rows, cap_hit


# ── taxrecords-nj guard ──────────────────────────────────────────────────
def taxrecords_still_empty() -> bool:
    """Re-check the 2026-08-26 finding that Monmouth holds no rows.

    Returns True if still empty (expected). If this ever returns False the
    county was loaded and the far cheaper owner-name path is back on the
    table — so it prints loudly rather than being swallowed.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import nj_taxrecords as tr

    tr.COUNTY_CODES["Monmouth"] = "1301"      # verified in the live dropdown
    tr.COUNTY_ALL_DISTRICT["Monmouth"] = "1300"   # 1300 = ALL
    try:
        hits = tr.lookup_by_owner_name("SMITH", "Monmouth", page_size=10)
    except tr.TaxRecordsUnavailable as e:
        print(f"  taxrecords-nj unreachable ({e}) — guard inconclusive.")
        return True
    return not hits


# ── classification ───────────────────────────────────────────────────────
def classify(rec: dict, rows: list[dict], cap_hit: bool) -> dict:
    """Bucket one decedent.

    A town mismatch is NOT disqualifying. The surrogate town is where the
    decedent lived at death; the deed town is where the property sits, and
    those legitimately differ (they moved, or died at a relative's address or
    in care). An out-of-town parcel is still a mailable lead — it just carries
    more same-name risk, so it gets its own tier instead of being discarded.
    """
    first, last = rec["first_name"], rec["last_name"]

    deeds = [r for r in rows if "DEED" in (r.get("Type") or "").upper()]
    as_grantee = [r for r in deeds if party_is_decedent(r.get("Indirect Party", ""), first, last)]
    as_grantor = [r for r in deeds if party_is_decedent(r.get("Direct Party", ""), first, last)]

    def parcel_key(r):
        return (norm_town(r.get("Town Name", ""))[0], r.get("Block", ""), r.get("Lot", ""))

    located = [r for r in as_grantee if r.get("Block") and r.get("Lot")]
    parcels = {parcel_key(r) for r in located}
    in_town = [r for r in located if towns_match(r.get("Town Name", ""), rec["town"])]
    town_parcels = {parcel_key(r) for r in in_town}

    if cap_hit:
        verdict = "NAME_COLLISION"       # cap hit: cannot attribute a deed to them
    elif not deeds:
        verdict = "NO_DEED"
    elif not as_grantee:
        verdict = "GRANTOR_ONLY"         # conveyed it away; not the current owner
    elif not parcels:
        verdict = "NO_BLOCK_LOT"
    elif len(town_parcels) == 1:
        verdict = "RESOLVED_IN_TOWN"     # one parcel, in the town they died in
    elif len(parcels) == 1:
        verdict = "RESOLVED_OTHER_TOWN"  # one parcel, elsewhere in Monmouth
    else:
        verdict = "AMBIGUOUS"

    chosen = sorted(town_parcels)[0] if len(town_parcels) == 1 else (
        sorted(parcels)[0] if len(parcels) == 1 else ("", "", ""))
    pool = in_town or located
    return {
        **rec,
        "verdict": verdict,
        "deed_rows": len(deeds),
        "as_grantee": len(as_grantee),
        "as_grantor": len(as_grantor),
        "in_town": len(in_town),
        "distinct_parcels": len(parcels),
        "matched_town": chosen[0], "block": chosen[1], "lot": chosen[2],
        "latest_deed": max((r.get("Recorded", "") for r in pool), default=""),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--csv", required=True, help="decedent CSV from monmouth_surrogate_probe.py")
    ap.add_argument("--limit", type=int, default=40, help="how many decedents to probe (default 40)")
    ap.add_argument("--from-date", default="01/01/1990", help="deed search lower bound MM/DD/YYYY")
    ap.add_argument("--to-date", default="", help="deed search upper bound MM/DD/YYYY")
    ap.add_argument("--delay", type=float, default=2.5, help="seconds between OPRS queries")
    ap.add_argument("--skip-taxrecords-guard", action="store_true")
    args = ap.parse_args(argv)

    src = Path(args.csv)
    if not src.exists():
        print(f"no such CSV: {src}", file=sys.stderr)
        return 1
    with src.open() as fh:
        records = list(csv.DictReader(fh))
    if not records:
        print(f"{src} has no rows — run monmouth_surrogate_probe.py first.", file=sys.stderr)
        return 1
    records = records[:args.limit]

    print(f"Monmouth property-resolution probe — {len(records)} decedents from {src.name}\n")

    if not args.skip_taxrecords_guard:
        print("taxrecords-nj Monmouth guard (expect: still empty)")
        if taxrecords_still_empty():
            print("  confirmed empty — OPRS deed index remains the only name->property path.\n")
        else:
            print("  *** CHANGED: taxrecords-nj NOW RETURNS MONMOUTH ROWS ***")
            print("  Add Monmouth to nj_taxrecords.COUNTY_CODES ('1301') and")
            print("  COUNTY_ALL_DISTRICT ('1300'); lookup_by_owner_name becomes viable.\n")

    results = []
    errors = 0
    for i, rec in enumerate(records, 1):
        label = f"{rec['first_name']} {rec['last_name']} ({rec['town']})"
        try:
            rows, cap_hit = oprs_search(rec["last_name"], rec["first_name"],
                                        args.from_date, args.to_date)
        except (ProbeError, requests.RequestException) as e:
            errors += 1
            print(f"  [{i}/{len(records)}] {label}: ERROR {e}")
            results.append({**rec, "verdict": "ERROR", "deed_rows": 0, "as_grantee": 0,
                            "as_grantor": 0, "in_town": 0, "distinct_parcels": 0,
                            "block": "", "lot": "", "matched_town": "", "latest_deed": ""})
            time.sleep(args.delay * 2)
            continue
        out = classify(rec, rows, cap_hit)
        results.append(out)
        extra = f" -> block {out['block']} lot {out['lot']} in {out['matched_town']}" \
            if out["verdict"].startswith("RESOLVED") else ""
        print(f"  [{i}/{len(records)}] {label}: {out['verdict']}"
              f" ({out['deed_rows']} deeds, {out['in_town']} in-town){extra}")
        time.sleep(args.delay * random.uniform(0.75, 1.25))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = OUT_DIR / f"resolution_{ts}.csv"
    with out_path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)

    tally = Counter(r["verdict"] for r in results)
    n = len(results)
    print("\n" + "=" * 58)
    print(f"{n} decedents probed" + (f", {errors} ERRORED" if errors else ""))
    for verdict, count in tally.most_common():
        print(f"  {verdict:16} {count:4}  {count / n:6.1%}")
    in_town = tally["RESOLVED_IN_TOWN"]
    other = tally["RESOLVED_OTHER_TOWN"]
    print("-" * 58)
    print(f"  RESOLUTION RATE  {(in_town + other) / n:.1%}  "
          f"({in_town + other}/{n} -> exactly one block/lot in Monmouth)")
    print(f"    of which in the surrogate's own town: {in_town}"
          f" ({in_town / n:.1%} of all) — highest confidence")
    print(f"    elsewhere in the county:              {other}"
          f" ({other / n:.1%} of all) — plausible, more same-name risk")
    if errors:
        print("  NOTE: errors are counted in the denominator — the rate is a floor.")
    print(f"\n-> {out_path}")
    print("\nRead the rate against the pipeline's honest ceilings: NJ probate is only")
    print("worth building when a record can be mailed. Compare with Bluestone counties,")
    print("where the court hands us the executor and the mailing address outright.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
