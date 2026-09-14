"""Dual-track ARV — the bedroom band, the clamp, and the walkthrough gate.

    .venv/bin/python tests/test_dual_track_arv.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from comp_analyzer import (SubjectProperty, CompProperty, calculate_arv,   # noqa: E402
                           dual_track_arv, classify_condition)

fails = 0
def check(label, ok, detail=""):
    global fails
    print(f"  {'OK  ' if ok else 'FAIL'} — {label}{(': ' + detail) if detail else ''}")
    if not ok: fails += 1


def comp(addr, beds, sqft, price, zest=None, dom=30):
    c = CompProperty(address=addr, city="Newark", state="NJ", zip_code="07103",
                     sqft=sqft, bedrooms=beds, bathrooms=2.0, year_built=1955,
                     sold_price=price, sold_date="2026-06-01", days_on_market=dom,
                     zestimate=zest if zest is not None else price / 0.97)
    c.ppsf = round(price / sqft, 2) if sqft else 0.0
    return c


# A 2-bed subject in a pocket where 3-beds trade in a clearly higher band.
SUBJ_2BD = SubjectProperty(address="1 Test St", city="Newark", state="NJ",
                           zip_code="07103", sqft=1400, bedrooms=2,
                           bathrooms=2.0, year_built=1955)
COMPS = [
    comp("10 A St", 2, 1180, 232_000), comp("12 A St", 2, 1240, 248_000),
    comp("14 A St", 2, 1300, 258_000), comp("16 A St", 2, 1150, 226_000),
    comp("20 B St", 3, 1380, 318_000), comp("22 B St", 3, 1420, 336_000),
    comp("24 B St", 3, 1500, 352_000), comp("26 B St", 3, 1340, 305_000),
]

print("== condition bucketing ==")
check("sold at 97% of Zestimate reads renovated",
      classify_condition(comp("x", 3, 1400, 400_000)) == "RENOVATED/RETAIL")
check("sold at 60% reads distressed",
      classify_condition(comp("x", 3, 1400, 240_000, zest=400_000)) == "DISTRESSED")
check("no Zestimate reads unknown",
      classify_condition(comp("x", 3, 1400, 400_000, zest=0)) == "UNKNOWN")

print("\n== the case the rule exists for: 2-bed subject, 3-bed comps present ==")
r = calculate_arv(SUBJ_2BD, [c for c in COMPS], walkthrough_verified=False)
two_bed_prices = sorted(c.sold_price for c in COMPS if c.bedrooms == 2)
band_median = (two_bed_prices[1] + two_bed_prices[2]) / 2
check("base scored in the 2-bed band only", r.same_bed_count == 4, f"{r.same_bed_count} same-bed")
check("arv_mid IS the base track", r.arv_mid == r.arv_base, f"{r.arv_mid:,} vs {r.arv_base:,}")
check("base never exceeds the 2-bed band median",
      r.arv_base <= band_median + 1, f"base ${r.arv_base:,.0f} vs band median ${band_median:,.0f}")
check("upside is computed from the 3-bed band",
      r.arv_upside > r.arv_base, f"upside ${r.arv_upside:,.0f} vs base ${r.arv_base:,.0f}")
check("upside NOT credited without a walkthrough", r.upside_credited is False)
check("basis says why", "NOT credited" in r.basis)
print(f"      basis: {r.basis[:110]}")

print("\n== what a linear per-bedroom adjustment would have done ==")
three_bed_median = sorted(c.sold_price for c in COMPS if c.bedrooms == 3)
tb_med = (three_bed_median[1] + three_bed_median[2]) / 2
linear = band_median + 15_000            # top of the NJ per-bedroom range
check("clamp holds the subject in its own band",
      r.arv_base < tb_med - 40_000,
      f"clamped ${r.arv_base:,.0f} vs 3-bed band ${tb_med:,.0f}")
print(f"      linear +$15k/bed would price it ~${linear:,.0f}; "
      f"the 3-bed band is ${tb_med:,.0f} — the gap the clamp protects is "
      f"${tb_med - r.arv_base:,.0f}")

print("\n== walkthrough gate opens the upside ==")
r2 = calculate_arv(SUBJ_2BD, [c for c in COMPS], walkthrough_verified=True)
check("credited once verified", r2.upside_credited is True)
check("base is unchanged by the gate", r2.arv_base == r.arv_base,
      f"{r2.arv_base:,} vs {r.arv_base:,}")
check("basis says credited", "CREDITED" in r2.basis)

print("\n== thin same-bed set widens and discounts ==")
thin = [comp("30 C St", 2, 1200, 240_000)] + [c for c in COMPS if c.bedrooms == 3]
r3 = calculate_arv(SUBJ_2BD, thin, walkthrough_verified=False)
check("flags the widen", "THIN" in r3.band_flag, r3.band_flag[:70])
check("discount applied", "discounted 10%" in r3.basis)

print("\n== no Zestimates: flagged, not silent ==")
nz = [comp(c.address, c.bedrooms, c.sqft, c.sold_price, zest=0) for c in COMPS]
r4 = calculate_arv(SUBJ_2BD, nz, walkthrough_verified=False)
check("still returns an ARV", r4.arv_base > 0, f"${r4.arv_base:,.0f}")
check("says condition bucketing was skipped",
      "Zestimates" in r4.basis or "condition" in r4.basis, r4.band_flag[:70])

print(f"\n{'FAILURES: ' + str(fails) if fails else 'all checks passed'}")
sys.exit(1 if fails else 0)
