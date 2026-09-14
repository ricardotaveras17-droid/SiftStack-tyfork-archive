"""Market analyzer — corpus dedup, true statistics, and dead-factor handling.

Covers the four defects the Division 7 audit found in `src/market_analyzer.py`:
the output directory is an uncurated scratch area whose duplicate files were
counted as real notices, `Path.glob` order made the output irreproducible,
sheriff-sale rows incremented the total but landed in no type bucket, and three
of six scoring factors were constant and dragged every score toward 50.

    .venv/bin/python tests/test_market_analyzer.py
"""
import csv
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import market_analyzer as ma  # noqa: E402
from market_analyzer import (CorpusStats, ZipProfile, _load_notice_data,  # noqa: E402
                             _norm_address, score_zip_codes)

fails = 0
def check(label, ok, detail=""):
    global fails
    print(f"  {'OK  ' if ok else 'FAIL'} — {label}{(': ' + detail) if detail else ''}")
    if not ok: fails += 1


FIELDS = ["address", "city", "state", "zip", "notice_type", "county",
          "estimated_value", "equity_percent"]


def write_csv(directory, name, rows):
    with open(Path(directory) / name, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def row(addr, zip_code="08831", notice="probate", county="middlesex",
        value="", equity="", city="Monroe"):
    return {"address": addr, "city": city, "state": "NJ", "zip": zip_code,
            "notice_type": notice, "county": county,
            "estimated_value": value, "equity_percent": equity}


def load(rows_by_file, counties=("middlesex",)):
    """Load a synthetic corpus and return (profiles, stats)."""
    with tempfile.TemporaryDirectory() as tmp:
        for name, rows in rows_by_file.items():
            write_csv(tmp, name, rows)
        original = ma.config.OUTPUT_DIR
        ma.config.OUTPUT_DIR = Path(tmp)
        try:
            stats = CorpusStats()
            profiles = _load_notice_data(list(counties), stats=stats)
        finally:
            ma.config.OUTPUT_DIR = original
    return profiles, stats


print("== a property counts once per notice type, across every file ==")
# The real failure: one 1,187-record backfill present three times under
# different names, counted three times over.
p, s = load({
    "backfill.csv": [row("12 Main St"), row("14 Main St")],
    "backfill_HELD_FOR_CLEANING.csv": [row("12 Main St"), row("14 Main St")],
    "probate_backfill_Probate.csv": [row("12 Main St"), row("14 Main St")],
})
check("6 rows across 3 identical files collapse to 2 notices",
      p["08831"].total_notices == 2, f"got {p['08831'].total_notices}")
check("4 duplicates reported, not hidden", s.duplicates_dropped == 4,
      f"got {s.duplicates_dropped}")
check("every row is still accounted for", s.rows_scanned == 6, f"got {s.rows_scanned}")

print("\n== punctuation and case do not create phantom properties ==")
p, _ = load({"a.csv": [row("12 Main St."), row("12 MAIN ST"), row("12  main   st")]})
check("three spellings of one address count once", p["08831"].total_notices == 1,
      f"got {p['08831'].total_notices}")
check("_norm_address is the reason", _norm_address("12 Main St.") == "12 MAIN ST")

print("\n== distinct notices on one property are not collapsed ==")
p, _ = load({"a.csv": [row("12 Main St", notice="probate"),
                       row("12 Main St", notice="foreclosure"),
                       row("12 Main St", notice="sheriff_sale")]})
check("one property, three notice types, three notices",
      p["08831"].total_notices == 3, f"got {p['08831'].total_notices}")

print("\n== sheriff sales land in their own bucket (the TOTAL must reconcile) ==")
z = p["08831"]
check("sheriff_sale_count is populated", z.sheriff_sale_count == 1, f"got {z.sheriff_sale_count}")
by_type = (z.foreclosure_count + z.sheriff_sale_count + z.tax_sale_count +
           z.tax_delinquent_count + z.probate_count + z.eviction_count +
           z.code_violation_count)
check("type columns sum to TOTAL", by_type == z.total_notices,
      f"{by_type} vs {z.total_notices}")

print("\n== median value is a median, not a running mean ==")
# A running mean over 1/2/3/100 gives 26.5; the median is 2.5. The Excel header
# says "Median Value", so it had better be one.
p, _ = load({"a.csv": [row(f"{i} Main St", value=str(v))
                       for i, v in enumerate([1, 2, 3, 100], start=1)]})
check("median of 1/2/3/100 is 2.5", p["08831"].median_value == 2.5,
      f"got {p['08831'].median_value}")

print("\n== equity is a true mean, not a recency-weighted running average ==")
# The old form was (avg + pct) / 2, which weights the last row 50%. Over
# 100/0/0/0 that yields 12.5; the mean is 25.
p, _ = load({"a.csv": [row(f"{i} Main St", equity=str(e))
                       for i, e in enumerate([100, 0, 0, 0], start=1)]})
check("mean of 100/0/0/0 is 25", p["08831"].avg_equity_pct == 25.0,
      f"got {p['08831'].avg_equity_pct}")

print("\n== aggregates do not depend on which file the OS hands back first ==")
a = [row("1 A St", value="100", equity="90"), row("2 A St", value="200", equity="10")]
b = [row("3 B St", value="300", equity="50")]
first, _ = load({"aaa.csv": a, "zzz.csv": b})
second, _ = load({"zzz.csv": a, "aaa.csv": b})   # same rows, opposite file order
check("median identical under either file order",
      first["08831"].median_value == second["08831"].median_value,
      f"{first['08831'].median_value} vs {second['08831'].median_value}")
check("equity identical under either file order",
      first["08831"].avg_equity_pct == second["08831"].avg_equity_pct,
      f"{first['08831'].avg_equity_pct} vs {second['08831'].avg_equity_pct}")

print("\n== ZIPs that cannot belong to the county are rejected, with a reason ==")
p, s = load({"a.csv": [
    row("1 Real St", zip_code="08831"),
    row("2 NY St", zip_code="12590", city="Wappingers Falls"),
    row("3 NC St", zip_code="28879", city="Sayreville"),
    row("4 Short St", zip_code="0706", city="Woodbridge"),
]})
check("only the in-state ZIP survives", set(p) == {"08831"}, f"got {sorted(p)}")
check("out-of-state ZIPs counted", s.out_of_state_zips_dropped == 2,
      f"got {s.out_of_state_zips_dropped}")
check("malformed ZIP counted separately", s.malformed_zips_dropped == 1,
      f"got {s.malformed_zips_dropped}")
check("each rejection carries a readable reason", len(s.rejected_zips) == 3,
      f"got {list(s.rejected_zips.values())}")

print("\n== a junk row in a triplicated file is reported once, not three times ==")
junk = [row("2 NY St", zip_code="12590", city="Wappingers Falls")]
_, s = load({"a.csv": junk, "b.csv": junk, "c.csv": junk})
check("three copies, one reported reason", len(s.rejected_zips) == 1,
      f"got {len(s.rejected_zips)}")

print("\n== a constant factor is dropped, not scored as a flat 50 ==")
profiles = {
    "08831": ZipProfile(zip_code="08831", total_notices=100, median_value=200_000,
                        avg_equity_pct=80),
    "08854": ZipProfile(zip_code="08854", total_notices=50, median_value=300_000,
                        avg_equity_pct=50),
    "07111": ZipProfile(zip_code="07111", total_notices=1, median_value=400_000,
                        avg_equity_pct=10),
}
log = []
scored = score_zip_codes({k: v for k, v in profiles.items()}, factor_log=log)
dropped = {f["factor"] for f in log if f["status"] == "DROPPED"}
check("the three unpopulated factors are dropped",
      dropped == {"Tax delinquency", "Competition", "Days on market"}, f"got {dropped}")
applied = sum(f["weight_applied"] for f in log)
check("surviving weights renormalize to 1.00", abs(applied - 1.0) < 1e-9, f"got {applied}")
check("the full range is reachable again",
      scored[0].score == 100.0 and scored[-1].score == 0.0,
      f"{scored[-1].score:.2f} - {scored[0].score:.2f}")
check("the best zip can earn an A", scored[0].grade == "A", f"got {scored[0].grade}")

print("\n== with the dead factors left in, that same zip could not reach an A ==")
# score = 0.65 * live + 17.5 caps everything at 82.5, and an A needs 75.
capped = 0.65 * 100.0 + 0.35 * 50.0
check("the old transform tops out at 82.5", abs(capped - 82.5) < 1e-9, f"{capped}")

print("\n== a live factor is never dropped ==")
check("distress/value/equity all survive",
      {f["factor"] for f in log if f["status"] == "live"} ==
      {"Distress density", "Median value", "Equity"})

print("\n== nothing to rank fails loudly rather than emitting a ranking ==")
flat = {z: ZipProfile(zip_code=z, total_notices=5) for z in ("08831", "08854")}
try:
    score_zip_codes(flat)
    check("constant-everything raises", False, "returned a ranking instead")
except ValueError as e:
    check("constant-everything raises ValueError", "nothing to rank" in str(e))

print("\n== empty input is not an error ==")
check("no profiles returns no ranking", score_zip_codes({}) == [])

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'}")
sys.exit(1 if fails else 0)
