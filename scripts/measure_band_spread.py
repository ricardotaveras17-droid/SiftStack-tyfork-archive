"""measure_band_spread.py — measure the bedroom-band price spread for NJ zips from real solds.

WHY
    The dual-track ARV clamp (comp_analyzer.dual_track_arv) is implemented and tested, but the
    SIZE of the bedroom-band break in New Jersey has never been measured. nj-adjustments.md
    carries an empty table: "(none measured yet)". Until a pocket is measured, the clamp is
    borrowed from a Knoxville example. This script fills that table from sold data.

WHAT IT DOES, PER ZIP
    1. Pull 12 months of RECENTLY_SOLD single-family listings via the band-partitioned
       /search client (zillow_market_api.pull_sold with the NJ price bands).
    2. Keep the RETAIL pool: sold >= RETAIL_ZEST_RATIO (0.90) of Zestimate, the same rule
       comp_analyzer.classify_condition uses. Listings with no Zestimate are counted and
       reported, never silently folded into the pool.
    3. Group the retail pool by bedroom count. Per band: N, median price, median $/sf, p25, p75.
    4. Report EVERY adjacent break (2->3, 3->4, 4->5 ...) in dollars and percent — Edison and
       Woodbridge are suburban, so the interesting break may not be 2->3.
    5. A band with N < MIN_BAND_N is INSUFFICIENT: reported, never used for a spread. This is a
       population floor, deliberately above comp_analyzer's per-comp THIN_BAND_MIN_COMPS (3).
    6. If a band's interquartile range exceeds the gap to its neighbour, the pocket is flagged
       HETEROGENEOUS — the gap is real but the band is not one market. Next step is a
       bbox-clipped re-run, not a clean number in the vault.

OUTPUT
    output/band_spread_{zip}_{date}.json        one per zip, full detail
    output/band_spread_summary_{date}.md        one table matching nj-adjustments.md:50-52
    This script NEVER writes to the vault. Filling nj-adjustments.md is a separate, approved step.

COST
    $0.005 per /search call (cost_estimator.RATE_ZILLOW_PER_RECORD). 10 NJ base bands, but a
    saturated band splits RECURSIVELY, not once — measured 2026-09-02: 08817 Edison 82 calls
    ($0.41, 175s), 08820 70, 07095 52, 07067 48. Budget 50-90 calls and ~2-3 minutes per active
    Middlesex zip. The original "20-40" guess was off by ~3x because it counted one split per
    band; volume zips split three or four levels deep in the $400k-$700k bands.

USAGE
    python scripts/measure_band_spread.py --dry-run                 # plan + key check, 0 calls
    python scripts/measure_band_spread.py --zips 08817               # one zip
    python scripts/measure_band_spread.py                            # all four default zips
    python scripts/measure_band_spread.py --months 18 --min-n 12
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

# Add src/ to path so this script works run from project root (same pattern as estimate_run_cost.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import config  # noqa: E402  — importing config runs load_dotenv(); the key never leaves the process
from comp_analyzer import RETAIL_ZEST_RATIO, _percentile  # noqa: E402
from zillow_market_api import (  # noqa: E402
    NJ_BANDS, ZillowMarketAPI, bands_for,
)

# Edison + Woodbridge Twp, ranked by Market Finder homes_sold_last_month (extracted 2026-08-23).
# 08817 40/mo · 08820 39/mo · 07095 22/mo · 07067 17/mo. Everything else ≤ 17 and thin.
DEFAULT_ZIPS = {
    "08817": "Edison, NJ 08817",
    "08820": "Edison, NJ 08820",
    "07095": "Woodbridge, NJ 07095",
    "07067": "Colonia, NJ 07067",
}
MIN_BAND_N = 10          # population floor to report a spread off a band
MONTHS_BACK = 12
COST_PER_CALL = 0.005


class CountingAPI(ZillowMarketAPI):
    """Same client, but it counts metered calls so the real cost gets reported, not estimated."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls = 0

    def search(self, *a, **kw):
        self.calls += 1
        return super().search(*a, **kw)


def band_stats(listings: list) -> dict:
    prices = [l.price for l in listings]
    ppsf = [l.ppsf for l in listings if l.ppsf]
    return {
        "n": len(listings),
        "median_price": statistics.median(prices) if prices else 0.0,
        "p25_price": _percentile(prices, 0.25),
        "p75_price": _percentile(prices, 0.75),
        "median_ppsf": statistics.median(ppsf) if ppsf else 0.0,
        "median_sqft": statistics.median([l.sqft for l in listings]) if listings else 0,
        "sufficient": len(listings) >= MIN_BAND_N,
    }


def measure_zip(api: CountingAPI, zip_code: str, location: str, months: int) -> dict:
    t0 = time.time()
    calls_before = api.calls
    solds = api.pull_sold(location, months_back=months, houses_only=True, state="NJ")
    calls = api.calls - calls_before
    elapsed = round(time.time() - t0, 1)

    # Pool accounting — every drop is counted, nothing is silent.
    n_all = len(solds)
    usable = [l for l in solds if l.beds > 0 and l.sqft > 0 and l.price > 0]
    n_no_zest = sum(1 for l in usable if not l.zestimate)
    with_zest = [l for l in usable if l.zestimate]
    retail = [l for l in with_zest if l.price / l.zestimate >= RETAIL_ZEST_RATIO]

    # Plan verification: the funnel must only shrink, and banded rows must be complete.
    assert n_all >= len(usable) >= len(with_zest) >= len(retail), "pool funnel grew — filter bug"
    assert all(l.beds > 0 and l.sqft > 0 and l.price > 0 for l in retail), "incomplete row in band"

    by_beds: dict[int, list] = defaultdict(list)
    for l in retail:
        by_beds[l.beds].append(l)
    bands = {b: band_stats(ls) for b, ls in sorted(by_beds.items())}

    # Adjacent breaks, only where BOTH bands clear the floor.
    breaks = []
    beds_sorted = sorted(bands)
    for lo, hi in zip(beds_sorted, beds_sorted[1:]):
        a, b = bands[lo], bands[hi]
        entry = {"from_beds": lo, "to_beds": hi, "n_from": a["n"], "n_to": b["n"]}
        if hi != lo + 1:
            entry["status"] = f"NOT ADJACENT ({lo}->{hi}); intermediate band missing"
        elif not (a["sufficient"] and b["sufficient"]):
            entry["status"] = f"INSUFFICIENT (N={a['n']} vs N={b['n']}, floor {MIN_BAND_N})"
        else:
            gap = b["median_price"] - a["median_price"]
            pct = gap / a["median_price"] if a["median_price"] else 0.0
            iqr_lo = a["p75_price"] - a["p25_price"]
            iqr_hi = b["p75_price"] - b["p25_price"]
            entry.update({
                "spread_usd": round(gap),
                "spread_pct": round(pct * 100, 1),
                "median_from": a["median_price"],
                "median_to": b["median_price"],
                "iqr_from": iqr_lo,
                "iqr_to": iqr_hi,
                "status": ("HETEROGENEOUS — IQR exceeds the band gap; bbox re-run before trusting"
                           if max(iqr_lo, iqr_hi) > abs(gap) else "OK"),
            })
        breaks.append(entry)

    return {
        "zip": zip_code,
        "location": location,
        "months_back": months,
        "measured_on": date.today().isoformat(),
        "source": "OpenWeb Ninja Zillow /search, RECENTLY_SOLD, SINGLE_FAMILY, band-partitioned",
        "retail_rule": f"sold >= {RETAIL_ZEST_RATIO:.0%} of Zestimate (comp_analyzer.RETAIL_ZEST_RATIO)",
        "min_band_n": MIN_BAND_N,
        "calls": calls,
        "cost_usd": round(calls * COST_PER_CALL, 3),
        "seconds": elapsed,
        "funnel": {
            "sold_sfr": n_all,
            "usable_beds_sqft_price": len(usable),
            "no_zestimate_excluded": n_no_zest,
            "with_zestimate": len(with_zest),
            "retail_pool": len(retail),
        },
        "bands": bands,
        "breaks": breaks,
    }


def fmt_money(v: float) -> str:
    return f"${v:,.0f}" if v else "—"


def summary_markdown(results: list[dict]) -> str:
    """Matches the empty table at nj-adjustments.md:50-52 so rows can be pasted after review."""
    today = date.today().isoformat()
    lines = [
        f"# Bedroom-band spread — Middlesex middle markets — {today}",
        "",
        "Rows below are shaped to drop into `nj-adjustments.md` under **Band spread is NOT yet measured for NJ**.",
        "Nothing here has been written to the vault. Review, then paste the rows you accept.",
        "",
        "| Pocket / zip | Measured band spread (2bd band vs 3bd band) | Date measured | Source |",
        "|---|---|---|---|",
    ]
    for r in results:
        z = r["zip"]
        two_three = next((b for b in r["breaks"] if b["from_beds"] == 2 and b["to_beds"] == 3), None)
        if two_three and two_three.get("status") == "OK":
            cell = (f"2bd med {fmt_money(two_three['median_from'])} → 3bd med "
                    f"{fmt_money(two_three['median_to'])} = **+{fmt_money(two_three['spread_usd'])} "
                    f"(+{two_three['spread_pct']}%)**, N={two_three['n_from']}/{two_three['n_to']}")
        elif two_three:
            cell = f"2→3 {two_three['status']}"
        else:
            cell = "no 2-bed band in retail pool"
        lines.append(f"| {z} ({r['location'].split(',')[0]}) | {cell} | {today} | {r['source']} |")

    lines += ["", "## Every adjacent break, all zips", "",
              "| Zip | Break | N | Median from → to | Spread | IQR from / to | Status |",
              "|---|---|---|---|---|---|---|"]
    for r in results:
        for b in r["breaks"]:
            if "spread_usd" in b:
                lines.append(
                    f"| {r['zip']} | {b['from_beds']}→{b['to_beds']} | {b['n_from']}/{b['n_to']} | "
                    f"{fmt_money(b['median_from'])} → {fmt_money(b['median_to'])} | "
                    f"+{fmt_money(b['spread_usd'])} (+{b['spread_pct']}%) | "
                    f"{fmt_money(b['iqr_from'])} / {fmt_money(b['iqr_to'])} | {b['status']} |")
            else:
                lines.append(f"| {r['zip']} | {b['from_beds']}→{b['to_beds']} | {b['n_from']}/{b['n_to']} | — | — | — | {b['status']} |")

    lines += ["", "## Pool funnel and cost", "",
              "| Zip | Sold SFR | Usable | No Zestimate | Retail pool | Calls | Cost | Seconds |",
              "|---|---|---|---|---|---|---|---|"]
    for r in results:
        f = r["funnel"]
        lines.append(f"| {r['zip']} | {f['sold_sfr']} | {f['usable_beds_sqft_price']} | "
                     f"{f['no_zestimate_excluded']} | {f['retail_pool']} | {r['calls']} | "
                     f"${r['cost_usd']:.2f} | {r['seconds']} |")
    total_cost = sum(r["cost_usd"] for r in results)
    total_calls = sum(r["calls"] for r in results)
    lines += ["", f"**Total: {total_calls} calls, ${total_cost:.2f}.**", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    global MIN_BAND_N   # declared first: it is read for the argparse default below, then reassigned
    ap = argparse.ArgumentParser(description="Measure NJ bedroom-band spread from sold data")
    ap.add_argument("--zips", nargs="*", default=list(DEFAULT_ZIPS),
                    help="zip codes to measure (default: 08817 08820 07095 07067)")
    ap.add_argument("--months", type=int, default=MONTHS_BACK)
    ap.add_argument("--min-n", type=int, default=MIN_BAND_N)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the band plan and confirm the key resolves; make ZERO calls")
    args = ap.parse_args(argv)

    MIN_BAND_N = args.min_n

    unknown = [z for z in args.zips if z not in DEFAULT_ZIPS]
    if unknown:
        ap.error(f"no location string for zip(s) {unknown}; add them to DEFAULT_ZIPS with a 'Town, NJ zip' label")

    # Constructing the client makes no request; it only resolves the key and raises if absent.
    api = CountingAPI()
    # Never print any part of the key — presence and length only.
    print(f"key: present ({len(config.OPENWEBNINJA_API_KEY)} chars)")

    bands = bands_for("NJ")
    print(f"price bands (NJ): {len(bands)} base bands, "
          f"{fmt_money(bands[0][0])}–{fmt_money(bands[-1][1])}")
    print(f"retail rule: sold >= {RETAIL_ZEST_RATIO:.0%} of Zestimate · band floor N>={MIN_BAND_N} · "
          f"{args.months} months")
    for z in args.zips:
        print(f"  {z}: {DEFAULT_ZIPS[z]!r}  est. {len(bands)}–{len(bands)*4} calls "
              f"(${len(bands)*COST_PER_CALL:.2f}–${len(bands)*4*COST_PER_CALL:.2f})")

    if args.dry_run:
        print("\ndry run — no calls made.")
        return 0

    assert bands is NJ_BANDS, "state bands did not resolve to NJ_BANDS — would over-split and over-spend"

    results = []
    out_dir = config.OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()

    for z in args.zips:
        print(f"\n== {z} {DEFAULT_ZIPS[z]} ==")
        r = measure_zip(api, z, DEFAULT_ZIPS[z], args.months)
        results.append(r)
        p = out_dir / f"band_spread_{z}_{today}.json"
        p.write_text(json.dumps(r, indent=2, default=str))
        f = r["funnel"]
        print(f"  {r['calls']} calls · ${r['cost_usd']:.2f} · {r['seconds']}s · "
              f"sold {f['sold_sfr']} → retail {f['retail_pool']} (no-zest {f['no_zestimate_excluded']})")
        for b, s in r["bands"].items():
            tag = "" if s["sufficient"] else "  INSUFFICIENT"
            print(f"    {b}bd  N={s['n']:<3} med {fmt_money(s['median_price'])}  "
                  f"p25 {fmt_money(s['p25_price'])}  p75 {fmt_money(s['p75_price'])}  "
                  f"${s['median_ppsf']:.0f}/sf{tag}")
        for b in r["breaks"]:
            if "spread_usd" in b:
                print(f"    {b['from_beds']}→{b['to_beds']}: +{fmt_money(b['spread_usd'])} "
                      f"(+{b['spread_pct']}%)  [{b['status']}]")
            else:
                print(f"    {b['from_beds']}→{b['to_beds']}: {b['status']}")
        print(f"  wrote {p.name}")

    md = out_dir / f"band_spread_summary_{today}.md"
    md.write_text(summary_markdown(results))
    print(f"\nwrote {md}")
    print(f"total: {api.calls} calls, ${api.calls * COST_PER_CALL:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
