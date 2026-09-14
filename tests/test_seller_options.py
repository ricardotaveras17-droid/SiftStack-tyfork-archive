"""Scenario sweep for the Seller Options Engine.

Re-run this after ANY change to lane percentages, policy defaults, or the
transfer-fee schedule. It is the fastest way to see whether the three routes
still rank sensibly across the condition range.

    .venv/bin/python tests/test_seller_options.py

Edit SCENARIOS to add real deals as they close — a scenario built from a deal
whose actual net is known is worth more than any number of invented ones.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from seller_options import CONDITION_LANES, compare_options  # noqa: E402

# (label, ARV, as-is value, rehab cost, condition)
SCENARIOS = [
    ("Newark gut job",     400_000, 245_000, 102_000, "needs_work"),
    ("Irvington heavy",    350_000, 205_000,  95_000, "needs_work"),
    ("Union wide spread",  500_000, 330_000,  80_000, "needs_work"),
    ("Cranford dated",     525_000, 430_000,  60_000, "outdated"),
    ("Westfield clean",    650_000, 600_000,  30_000, "good"),
    ("Summit renovated",   800_000, 775_000,  12_000, "excellent"),
]


def sweep(state: str = "NJ") -> int:
    print(f"{'scenario':<21}{'ARV':>8}{'cash':>11}{'we-list':>11}{'as-is':>11}"
          f"  {'winner':<16}{'vs cash':>10}")
    print("-" * 90)
    failures = 0
    for label, arv, as_is, rehab, cond in SCENARIOS:
        try:
            c = compare_options(arv=arv, as_is_value=as_is, rehab_cost=rehab,
                                condition=cond, state=state)
        except ValueError as exc:
            print(f"{label:<21}  ERROR: {exc}")
            failures += 1
            continue
        cash = c.by_key("cash").seller_net
        best = c.by_key(c.best_net)
        print(f"{label:<21}{arv/1000:>7.0f}k{cash:>11,.0f}"
              f"{c.by_key('managed_listing').seller_net:>11,.0f}"
              f"{c.by_key('listing').seller_net:>11,.0f}"
              f"  {c.best_net:<16}{best.seller_net - cash:>+10,.0f}")
    return failures


def gates() -> int:
    """Every one of these must refuse. A comparison built on an impossible
    input is worse than no comparison — it reads as authoritative."""
    base = dict(arv=400_000, as_is_value=245_000, rehab_cost=102_000,
                condition="needs_work", state="NJ")
    cases = [
        ("as-is at/above ARV-minus-rehab", {**base, "as_is_value": 300_000}),
        ("missing rehab",                  {**base, "rehab_cost": 0}),
        ("missing as-is",                  {**base, "as_is_value": 0}),
        ("missing ARV",                    {**base, "arv": 0}),
        ("unknown condition",              {**base, "condition": "mediocre"}),
        ("uncalibrated state",             {**base, "state": "PA"}),
    ]
    failures = 0
    for label, kw in cases:
        try:
            compare_options(**kw)
            print(f"  FAIL — {label} did not raise")
            failures += 1
        except ValueError as exc:
            print(f"  OK   — {label}: {type(exc).__name__}")
    return failures


if __name__ == "__main__":
    print("== scenario sweep ==")
    f = sweep()
    print(f"\n== validation gates ({len(CONDITION_LANES)} condition lanes defined) ==")
    f += gates()
    print(f"\n{'FAILURES: ' + str(f) if f else 'all checks passed'}")
    sys.exit(1 if f else 0)
