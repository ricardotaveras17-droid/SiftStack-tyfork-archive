"""Exit Strategy Engine — lane-gate behaviour.

    .venv/bin/python tests/test_exit_strategy.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from exit_strategy import analyse, SCOPES  # noqa: E402


def line(label, **kw):
    a = analyse(**kw)
    clears = ", ".join(l.key for l in a.suggested) or "-none-"
    print(f"  {label:<38} clears: {clears}")
    return a


print("== offer sensitivity (ARV 400k, rehab 102k, med scope) ==")
for offer in (150_000, 175_000, 200_000, 228_000):
    a = line(f"offer ${offer:,}", base_arv=400_000, quoted_rehab=102_000,
             offer=offer, scope="med")

print("\n== reconfig lane is gated on the walkthrough ==")
a = line("upside ARV, NOT walked", base_arv=400_000, quoted_rehab=102_000,
         offer=170_000, scope="med", upside_arv=470_000)
rc = next(l for l in a.lanes if l.key == "flip_reconfig")
print(f"      reconfig reason: {rc.reasons[0][:78]}...")
a = line("upside ARV, walked+verified", base_arv=400_000, quoted_rehab=102_000,
         offer=170_000, scope="med", upside_arv=470_000, walkthrough_verified=True)

print("\n== financed flip must also clear the $10k wholesale floor ==")
a = line("cash", base_arv=400_000, quoted_rehab=102_000, offer=196_000, scope="med")
a = line("financed", base_arv=400_000, quoted_rehab=102_000, offer=196_000,
         scope="med", financed=True)
fl = next(l for l in a.lanes if l.key == "flip")
if fl.reasons:
    print(f"      flip reason: {fl.reasons[-1][:78]}...")

print("\n== nothing clears -> named misses, no fabricated recommendation ==")
a = analyse(base_arv=400_000, quoted_rehab=102_000, offer=310_000, scope="heavy")
print(f"  headline: {a.headline[:76]}...")
print(f"  suggested: {len(a.suggested)}   near misses shown: {[l.key for l in a.near_misses]}")

print("\n== BRRRR needs rent, then gets scored ==")
a = line("no rent", base_arv=400_000, quoted_rehab=102_000, offer=170_000, scope="med")
a = line("rent $3,800/mo", base_arv=400_000, quoted_rehab=102_000, offer=170_000,
         scope="med", monthly_rent=3_800)
br = next(l for l in a.lanes if l.key == "brrrr")
print(f"      BRRRR: {'clears' if br.passed else br.reasons[0][:70]}")

print("\n== signed fixed-price contract drops the overrun factor ==")
for signed in (False, True):
    a = analyse(base_arv=400_000, quoted_rehab=102_000, offer=196_000, scope="med",
                signed_contract=signed)
    fl = next(l for l in a.lanes if l.key == "flip")
    print(f"  signed={str(signed):<5} expected rehab ${fl.expected_rehab:>8,} "
          f"MAO ${fl.mao:>8,}  profit ${fl.actual_profit:>8,}")

print("\n== validation ==")
for label, kw in [("bad scope", dict(base_arv=400_000, quoted_rehab=1, offer=1, scope="huge")),
                  ("no ARV", dict(base_arv=0, quoted_rehab=1, offer=1)),
                  ("no offer", dict(base_arv=400_000, quoted_rehab=1, offer=0))]:
    try:
        analyse(**kw); print(f"  FAIL — {label} did not raise")
    except ValueError:
        print(f"  OK   — {label} raises")
