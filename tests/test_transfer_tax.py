"""NJ realty transfer fee — brackets, the $350k cliff, and fail-loud states.

Hand-checked against the NJ Division of Taxation schedule
(https://www.nj.gov/treasury/taxation/realty.shtml, verified 2026-08-20).
The $2.2M figure matches NJ REALTORS' published worked example.

    .venv/bin/python tests/test_transfer_tax.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from deal_analyzer import (DEFAULT_TRANSFER_TAX_PCT, UnknownTransferTaxError,   # noqa: E402
                           calculate_selling_costs, calculate_transfer_tax)

fails = 0
def check(label, ok, detail=""):
    global fails
    print(f"  {'OK  ' if ok else 'FAIL'} — {label}{(': ' + detail) if detail else ''}")
    if not ok: fails += 1

print("== hand-checked brackets ==")
EXPECT = {150_000: 600.00, 200_000: 935.00, 250_000: 1_325.00, 350_000: 2_105.00,
          400_000: 3_215.00, 700_000: 6_245.00, 1_000_000: 9_575.00}
for price, want in EXPECT.items():
    got = calculate_transfer_tax(price, "NJ")
    check(f"${price:,}", abs(got - want) < 0.01, f"${got:,.2f} vs ${want:,.2f}")

print("\n== the $350k cliff: two tables, not one ==")
a, b = calculate_transfer_tax(350_000, "NJ"), calculate_transfer_tax(350_500, "NJ")
check("crossing $350k costs a step up", b - a > 600, f"+${b - a:,.2f} for $500 more")

print("\n== graduated percent fee applies to the FULL consideration ==")
grad = calculate_transfer_tax(2_200_000, "NJ") - 24_095.0
check("$2.2M graduated portion is $44,000", abs(grad - 44_000) < 1,
      f"${grad:,.0f} (published worked example: $44,000)")

print("\n== NJ costs more than the old TN flat rate ==")
for price in (250_000, 400_000, 700_000):
    nj, tn = calculate_transfer_tax(price, "NJ"), price * DEFAULT_TRANSFER_TAX_PCT
    check(f"${price:,} understated by the TN rate", nj > tn,
          f"NJ ${nj:,.0f} vs TN ${tn:,.0f} (+${nj - tn:,.0f})")

print("\n== state handling ==")
check("TN unchanged", abs(calculate_transfer_tax(400_000, "TN") - 1_480.0) < 0.01)
for spelling in ("nj", "New Jersey", "NEW JERSEY"):
    check(f"{spelling!r} normalises", abs(calculate_transfer_tax(400_000, spelling) - 3_215.0) < 0.01)
for bad in ("PA", ""):
    try:
        calculate_transfer_tax(400_000, bad); check(f"{bad!r} raises", False, "did not raise")
    except UnknownTransferTaxError:
        check(f"{bad!r} raises", True)

print("\n== selling costs carry it through ==")
sc = calculate_selling_costs(400_000, state="NJ")
check("transfer tax lands in selling costs", sc.transfer_tax == 3215, f"${sc.transfer_tax:,}")

print(f"\n{'FAILURES: ' + str(fails) if fails else 'all checks passed'}")
sys.exit(1 if fails else 0)
