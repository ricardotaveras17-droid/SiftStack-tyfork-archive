"""Region calibration — TN must not move, NJ must resolve, unknowns must raise.

The TN numbers below are GOLDEN. They were the engine's output before the
labor/materials split was introduced, and the split was built specifically so
they would not change: every TN region carries labor == materials, and the
round-then-split arithmetic was preserved. If one of these drifts, the split
has leaked into a market it was never meant to touch.

    .venv/bin/python tests/test_region_calibration.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from rehab_estimator import (REGIONAL_RATES, UnknownRegionError,   # noqa: E402
                             estimate_rehab, resolve_region)

CASES = [(1400, 3, 2.0, 1955, 2, "full"), (2100, 4, 2.5, 1978, 3, "full"),
         (900, 2, 1.0, 1930, 1, "full"), (1600, 3, 2.0, 1965, 4, "wholetail")]

# The wholetail rows moved on 2026-08-21 and that was INTENTIONAL: the
# wholetail model was corrected after a real closed job showed the engine
# charging a full kitchen and full baths while excluding HVAC, plumbing and
# electrical. Full-scope rows are unchanged. If a FULL row ever moves, that is
# drift and it is a bug.
# (region, sqft, tier, scope) -> (materials, labor, grand_total)
GOLDEN = {
    ("knoxville", 1400, 2, "full"): (43349, 28679, 81392),
    ("knoxville", 2100, 3, "full"): (81378, 45862, 143781),
    ("knoxville", 900, 1, "full"): (17144, 13114, 34192),
    ("knoxville", 1600, 4, "wholetail"): (55238, 31064, 97521),
    ("blount", 1400, 2, "full"): (42364, 28027, 79542),
    ("blount", 2100, 3, "full"): (79528, 44819, 140512),
    ("blount", 900, 1, "full"): (16755, 12816, 33415),
    ("blount", 1600, 4, "wholetail"): (53984, 30357, 95305),
    ("nashville", 1400, 2, "full"): (46798, 30961, 87868),
    ("nashville", 2100, 3, "full"): (87852, 49510, 155219),
    ("nashville", 900, 1, "full"): (18510, 14157, 36914),
    ("nashville", 1600, 4, "wholetail"): (59631, 33535, 105278),
    ("chattanooga", 1400, 2, "full"): (44334, 29331, 83241),
    ("chattanooga", 2100, 3, "full"): (83227, 46903, 147047),
    ("chattanooga", 900, 1, "full"): (17534, 13412, 34969),
    ("chattanooga", 1600, 4, "wholetail"): (56493, 31770, 99737),
    ("national", 1400, 2, "full"): (49260, 32590, 92491),
    ("national", 2100, 3, "full"): (92475, 52115, 163387),
    ("national", 900, 1, "full"): (19482, 14902, 38854),
    ("national", 1600, 4, "wholetail"): (62770, 35300, 110819),
}

fails = 0
def check(label, ok, detail=""):
    global fails
    print(f"  {'OK  ' if ok else 'FAIL'} — {label}{(': ' + detail) if detail else ''}")
    if not ok: fails += 1

print("== TN golden values must not move ==")
moved = []
for (region, sqft, tier, scope), want in GOLDEN.items():
    case = next(c for c in CASES if c[0] == sqft and c[4] == tier and c[5] == scope)
    sq, bd, ba, yr, t, sc = case
    e = estimate_rehab(sqft=sq, bedrooms=bd, bathrooms=ba, year_built=yr,
                       tier=t, scope=sc, region=region)
    got = (e.total_materials, e.total_labor, e.grand_total)
    if got != want:
        moved.append(f"{region} {sqft}sf t{tier}: {got} != {want}")
check(f"{len(GOLDEN)} golden values unchanged", not moved, "; ".join(moved[:3]))

print("\n== TN regions hold labor == materials ==")
for r in ("knoxville", "blount", "nashville", "chattanooga", "national"):
    rr = REGIONAL_RATES[r]
    check(f"{r}", rr.labor == rr.materials, f"{rr.labor} / {rr.materials}")

print("\n== NJ is calibrated above national, and flagged unverified ==")
for r in ("essex", "union", "middlesex", "somerset"):
    rr = REGIONAL_RATES[r]
    check(f"{r} labor above national", rr.labor > 1.0, f"{rr.labor}")
    check(f"{r} marked unverified", rr.verified is False)

print("\n== NJ rehabs cost more than TN ==")
base = dict(sqft=1400, bedrooms=3, bathrooms=2.0, year_built=1955, tier=2, scope="full")
kx = estimate_rehab(region="knoxville", **base).grand_total
for r in ("somerset", "middlesex", "union", "essex"):
    nj = estimate_rehab(region=r, **base).grand_total
    check(f"{r} > knoxville", nj > kx, f"${nj:,} vs ${kx:,} (+{(nj/kx-1)*100:.0f}%)")

print("\n== region resolution ==")
for state, city, county, want in [("NJ", "Newark", "", "essex"),
                                  ("NJ", "Elizabeth", "", "union"),
                                  ("NJ", "", "Middlesex", "middlesex"),
                                  ("NJ", "", "Somerset County", "somerset"),
                                  ("TN", "Knoxville", "", "knoxville")]:
    check(f"{state} {city or county} -> {want}",
          resolve_region(state=state, city=city, county=county) == want)

print("\n== fail loud, never a silent national fallback ==")
for label, kw in [("unknown region key", dict(region="essexx", **base)),
                  ("nothing to resolve from", dict(**base)),
                  ("city not in the table", dict(state="NJ", city="Hoboken", **base))]:
    try:
        estimate_rehab(**kw); check(label, False, "did not raise")
    except UnknownRegionError:
        check(label, True)
try:
    estimate_rehab(region="essex", **{**base, "sqft": 0})
    check("missing sqft", False, "did not raise")
except ValueError:
    check("missing sqft", True)

print(f"\n{'FAILURES: ' + str(fails) if fails else 'all checks passed'}")
sys.exit(1 if fails else 0)
