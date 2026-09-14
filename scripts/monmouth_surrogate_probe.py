"""Monmouth County Surrogate index — sampler / feasibility probe.

Monmouth is NOT on the Bluestone platform we scrape for Middlesex/Somerset/
Ocean. It runs an in-house IIS/ASP.NET WebForms app:

    https://mcapps.co.monmouth.nj.us/Surrogate/SurrogateList.aspx

Verified live 2026-08-26 (see output/monmouth_recon/FINDINGS.txt): no
Cloudflare, no captcha, no auth, no Playwright — plain `requests` plus
ViewState is enough. One disclaimer postback gates the search form.

The form has NO date-of-death or filed-date filter: first name + last name
only, BOTH required, BOTH prefix-matched, no paging, no observed row cap.
So the only way to enumerate is a letter grid — 26x26 = 676 queries covering
every (first-initial, last-initial) pair — with the DOD window applied
client-side afterwards.

This script does NOT feed the pipeline. It samples the index so the sample
can be handed to scripts/monmouth_property_probe.py, which measures whether
"decedent name + town" can be turned into a property. That measurement is
the open question; the scrape itself is not.

    PYTHONPATH=src python scripts/monmouth_surrogate_probe.py --sample
    PYTHONPATH=src python scripts/monmouth_surrogate_probe.py --grid --days-back 120

Output: output/monmouth_recon/decedents_{ts}.csv + a printed summary.
"""
from __future__ import annotations

import argparse
import csv
import random
import re
import string
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

URL = "https://mcapps.co.monmouth.nj.us/Surrogate/SurrogateList.aspx"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
OUT_DIR = Path(__file__).resolve().parents[1] / "output" / "monmouth_recon"
TIMEOUT = 60

# Verified control-name prefix. The whole form lives under this.
P = "ctl00$ContentPlaceHolder1$"
AGREE_TARGET = P + "IAgree"

# The grid header row, used to recognise (and drop) the header.
HEADER = ("First Name", "Last Name", "Town", "Case Type", "Date of Death", "Docket")

# A small, deliberately-spread sample of letter pairs for --sample mode.
SAMPLE_PAIRS = [
    ("A", "B"), ("J", "S"), ("M", "R"), ("R", "G"),
    ("D", "M"), ("E", "C"), ("T", "W"), ("K", "H"),
]


def spread_pairs(per_letter: int = 3) -> list[tuple[str, str]]:
    """Every first initial A-Z, each paired with `per_letter` last initials
    rotating through the alphabet.

    --sample's eight hand-picked pairs are all common-initial combinations,
    which over-samples common surnames — exactly the names a name-based
    lookup struggles to disambiguate. This covers all 26 first initials on an
    offset stride instead, so a draw from the pool is not pre-loaded with
    hard cases.
    """
    letters = string.ascii_uppercase
    pairs = []
    for i, first in enumerate(letters):
        for k in range(per_letter):
            pairs.append((first, letters[(i * 7 + k * 9) % 26]))
    return pairs


class MonmouthProbeError(RuntimeError):
    """The portal did not behave the way the recon documented."""


def _hidden(html: str, name: str) -> str:
    m = re.search(r'id="%s"[^>]*value="([^"]*)"' % re.escape(name), html)
    return m.group(1) if m else ""


def _state(html: str) -> dict:
    return {
        "__VIEWSTATE": _hidden(html, "__VIEWSTATE"),
        "__VIEWSTATEGENERATOR": _hidden(html, "__VIEWSTATEGENERATOR"),
        "__EVENTVALIDATION": _hidden(html, "__EVENTVALIDATION"),
    }


def _rows(html: str) -> list[list[str]]:
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [
            re.sub(r"\s+", " ", re.sub(r"<[^>]*>", "", c)).replace("\xa0", "").strip()
            for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)
        ]
        if cells:
            out.append(cells)
    return out


def open_session() -> tuple[requests.Session, str]:
    """GET the disclaimer, POST 'I Agree', return the session + search page.

    Fails loud if the search form does not appear — a silent miss here would
    otherwise look identical to "the county has no records".
    """
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    r = s.get(URL, timeout=TIMEOUT)
    r.raise_for_status()
    if AGREE_TARGET not in r.text:
        raise MonmouthProbeError(
            "disclaimer gate not found — the portal changed. "
            f"Expected a postback target {AGREE_TARGET!r}."
        )
    data = _state(r.text)
    data.update({"__EVENTTARGET": AGREE_TARGET, "__EVENTARGUMENT": ""})
    r2 = s.post(URL, data=data, timeout=TIMEOUT)
    r2.raise_for_status()
    if P + "txtLastName" not in r2.text:
        raise MonmouthProbeError(
            "search form not reached after the I-Agree postback — the portal changed."
        )
    return s, r2.text


def search(s: requests.Session, page_html: str, first: str, last: str) -> tuple[list[list[str]], str]:
    """One name search. Returns (data rows, fresh page html for the next call).

    Both fields are required — a blank first name returns 'No data found'
    regardless of the last name, which is why the caller sweeps a grid.
    """
    data = _state(page_html)
    data.update({
        "__EVENTTARGET": "", "__EVENTARGUMENT": "",
        P + "txtFirstName": first,
        P + "txtLastName": last,
        P + "cmdSearch": "Search",
    })
    r = s.post(URL, data=data, timeout=TIMEOUT)
    r.raise_for_status()
    rows = _rows(r.text)
    data_rows = [
        c for c in rows
        if len(c) >= 6 and tuple(c[:6]) != HEADER and "No data found" not in c[0]
    ]
    return data_rows, r.text


def _parse_dod(raw: str) -> date | None:
    try:
        return datetime.strptime(raw.strip(), "%m-%d-%Y").date()
    except ValueError:
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--grid", action="store_true",
                      help="full 26x26 sweep (676 queries, the real enumeration)")
    mode.add_argument("--sample", action="store_true", default=True,
                      help="8 hand-picked letter pairs — quickest yield check (default)")
    mode.add_argument("--spread", type=int, metavar="N", default=0,
                      help="all 26 first initials x N rotating last initials "
                           "(26*N queries) — use this when the pool feeds a "
                           "random draw and must not be biased to common names")
    ap.add_argument("--days-back", type=int, default=90,
                    help="keep rows whose DOD falls within this many days (default 90)")
    ap.add_argument("--delay", type=float, default=2.0,
                    help="seconds between queries, jittered +/-25%% (default 2.0)")
    ap.add_argument("--out", default="", help="CSV path (default: output/monmouth_recon/decedents_{ts}.csv)")
    args = ap.parse_args(argv)

    letters = list(string.ascii_uppercase)
    if args.grid:
        pairs = [(f, l) for f in letters for l in letters]
    elif args.spread:
        pairs = spread_pairs(args.spread)
    else:
        pairs = SAMPLE_PAIRS
    cutoff = date.today() - timedelta(days=args.days_back)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = Path(args.out) if args.out else OUT_DIR / f"decedents_{ts}.csv"

    print(f"Monmouth surrogate probe — {len(pairs)} queries, DOD >= {cutoff}")
    try:
        s, page = open_session()
    except (MonmouthProbeError, requests.RequestException) as e:
        print(f"FAILED to open the portal: {e}", file=sys.stderr)
        return 1

    seen: set[str] = set()
    kept: list[dict] = []
    total_rows = 0
    failures = 0

    for i, (first, last) in enumerate(pairs, 1):
        try:
            rows, page = search(s, page, first, last)
        except requests.RequestException as e:
            failures += 1
            print(f"  [{i}/{len(pairs)}] {first}/{last}: REQUEST FAILED ({e})")
            time.sleep(args.delay * 2)
            continue
        total_rows += len(rows)
        fresh = 0
        for c in rows:
            docket = c[5]
            dod = _parse_dod(c[4])
            if docket in seen:
                continue          # the index really does repeat rows
            if dod is None or dod < cutoff:
                continue
            seen.add(docket)
            fresh += 1
            kept.append({
                "first_name": c[0], "last_name": c[1], "town": c[2],
                "case_type": c[3], "dod": dod.isoformat(), "docket": docket,
            })
        print(f"  [{i}/{len(pairs)}] {first}/{last}: {len(rows)} rows -> {fresh} in window")
        time.sleep(args.delay * random.uniform(0.75, 1.25))

    with out_path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["first_name", "last_name", "town",
                                           "case_type", "dod", "docket"])
        w.writeheader()
        w.writerows(sorted(kept, key=lambda r: r["dod"], reverse=True))

    print(f"\n{len(pairs) - failures}/{len(pairs)} queries OK"
          + (f" ({failures} FAILED)" if failures else ""))
    print(f"{total_rows} rows seen, {len(kept)} unique with DOD >= {cutoff}")
    print(f"-> {out_path}")
    if failures:
        print("NOTE: failed queries mean the sweep is incomplete — yield is a floor, not a count.")
    print("\nNext: PYTHONPATH=src python scripts/monmouth_property_probe.py "
          f"--csv {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
