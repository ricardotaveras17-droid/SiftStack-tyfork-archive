"""Exit Strategy Engine — score every lane against the conservative ARV and say
why each one is in or out.

Five lanes: wholesale assignment, wholetail, flip same-config, flip reconfig,
BRRRR. Each is GATED. A lane that does not clear its gate is named and
explained rather than quietly dropped, and if nothing clears, the two closest
misses render under an explicit banner instead of a fabricated recommendation.
The template's lane slots are not a quota.

The underwriting math is the calibrated model from the flip-profit-analyzer
skill (18 closed deals), not the 75%-rule arithmetic in deal_analyzer:

    ExpectedRehab       = QuotedRehab x (1 + OverrunFactor[scope])
    AllInCosts          = ARV x (0.081 + 0.006 x HoldMonths)
    ProfitTarget        = max(Floor% x ARV, Floor$, 12000 x HoldMonths) x Risk
    MAO                 = ARV - ExpectedRehab - AllInCosts - ProfitTarget
    ActualProfitAtOffer = ARV - (ExpectedRehab + AllInCosts) - Offer

That last line is the one that matters. Profit is NOT
``ARV - (AllCostsExceptPurchase + Offer)`` -- that expression returns Surplus,
which is $0 at MAO by construction, so every deal would fail its own MAO.

Lanes score against BASE ARV. Only flip-reconfig uses the upside ARV, and only
after a walkthrough has verified the layout actually converts.

Usage:
  python src/exit_strategy.py --arv 400000 --as-is 245000 --rehab 102000 \\
      --offer 228000 --scope med --state NJ
"""

import argparse
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── Calibrated scope table (flip-profit-analyzer, n=18) ───────────────
SCOPES = {
    "lite":       {"overrun": 0.45, "hold_days": 103, "floor_pct": 0.0725, "floor_usd": 35_000},
    "med":        {"overrun": 0.38, "hold_days": 148, "floor_pct": 0.0855, "floor_usd": 47_000},
    "heavy":      {"overrun": 0.42, "hold_days": 260, "floor_pct": 0.1000, "floor_usd": 60_000},
    "extensive":  {"overrun": 0.12, "hold_days": 240, "floor_pct": 0.1200, "floor_usd": 75_000},
    "assignment": {"overrun": 0.00, "hold_days": 0,   "floor_pct": 0.0300, "floor_usd": 15_000},
}
SIGNED_CONTRACT_OVERRUN = 0.15   # only when a signed fixed-price contract exists

ALLIN_BASE = 0.081
ALLIN_PER_MONTH = 0.006
DURATION_FLOOR_PER_MONTH = 12_000.0

# Gates
MOS_GATE = 0.09                  # ActualProfitAtOffer / ARV
PROFIT_PER_MONTH_GATE = 12_000.0
# A financed flip must clear the wholesale floor on profit AFTER debt service.
# Financing is not free: points plus interest come out of the same profit, and a
# flip that nets less after debt than simply assigning the contract would is a
# lot of work for less money. Gating on the pre-debt profit makes this floor
# dead code, because the MOS gate (9% of ARV) always binds first.
WHOLESALE_FLOOR = 10_000.0
HIGH_RISK_SCOPES = ("heavy", "extensive")
HIGH_RISK_PROFIT_CUSHION = 15_000.0
TIGHT_MAO_PCT = 0.85

# Verdict bands by % over MAO
VERDICTS = ((0.00, "BUY"), (0.05, "BORDERLINE"), (0.15, "PASS"))

# Wholetail sells below a full renovation. Policy default, not a market fact.
WHOLETAIL_PCT_OF_ARV = 0.90
BRRRR_REFI_LTV = 0.75
BRRRR_MIN_DSCR = 1.20


@dataclass
class Lane:
    key: str = ""
    name: str = ""
    basis: str = ""            # which ARV this lane scored against
    sale_price: float = 0.0
    expected_rehab: float = 0.0
    all_in: float = 0.0
    profit_target: float = 0.0
    binding_floor: str = ""
    mao: float = 0.0
    actual_profit: float = 0.0
    mos: float = 0.0
    profit_per_month: float = 0.0
    hold_months: float = 0.0
    pct_over_mao: float = 0.0
    financing_cost: float = 0.0
    profit_after_debt: float = 0.0
    verdict: str = ""
    passed: bool = False
    reasons: list = field(default_factory=list)   # why it failed, or notes


@dataclass
class ExitAnalysis:
    address: str = ""
    offer: float = 0.0
    base_arv: float = 0.0
    upside_arv: float = 0.0
    lanes: list = field(default_factory=list)
    suggested: list = field(default_factory=list)
    ruled_out: list = field(default_factory=list)
    headline: str = ""
    near_misses: list = field(default_factory=list)


def _verdict(pct_over_mao: float) -> str:
    for threshold, label in VERDICTS:
        if pct_over_mao <= threshold:
            return label
    return "DANGER"


def _score(name: str, key: str, sale_price: float, quoted_rehab: float,
           offer: float, scope: str, basis: str, risk_multiplier: float,
           signed_contract: bool, hold_days_override: int = 0) -> Lane:
    """Core underwriting for one lane. Pure flip-profit-analyzer arithmetic."""
    sc = SCOPES[scope]
    overrun = SIGNED_CONTRACT_OVERRUN if (signed_contract and quoted_rehab) else sc["overrun"]
    hold_days = hold_days_override or sc["hold_days"]
    hold_months = hold_days / 30.0

    expected_rehab = quoted_rehab * (1 + overrun)
    all_in = sale_price * (ALLIN_BASE + ALLIN_PER_MONTH * hold_months)

    floor_pct_amt = sc["floor_pct"] * sale_price
    floor_usd = sc["floor_usd"]
    duration_floor = DURATION_FLOOR_PER_MONTH * hold_months
    raw_floor = max(floor_pct_amt, floor_usd, duration_floor)
    binding = ("percent" if raw_floor == floor_pct_amt else
               "dollar" if raw_floor == floor_usd else "duration")
    profit_target = raw_floor * risk_multiplier

    non_profit_costs = expected_rehab + all_in
    mao = sale_price - non_profit_costs - profit_target
    actual_profit = sale_price - non_profit_costs - offer
    mos = actual_profit / sale_price if sale_price else 0.0
    ppm = actual_profit / hold_months if hold_months else actual_profit
    pct_over = ((offer - mao) / mao) if mao > 0 else float("inf")

    return Lane(key=key, name=name, basis=basis, sale_price=sale_price,
                expected_rehab=round(expected_rehab), all_in=round(all_in),
                profit_target=round(profit_target), binding_floor=binding,
                mao=round(mao), actual_profit=round(actual_profit), mos=mos,
                profit_per_month=ppm, hold_months=hold_months,
                pct_over_mao=pct_over, verdict=_verdict(pct_over))


def estimate_financing_cost(offer: float, rehab: float, hold_months: float,
                            rate: float = 0.12, points: float = 2.0,
                            ltc: float = 0.85) -> float:
    """Points + interest on a typical private-money facility for this deal.

    Mirrors lender_analysis.analyse() so the two modules agree; passed in
    explicitly when real terms are known.
    """
    loan = (offer + rehab) * ltc
    return loan * points / 100.0 + loan * rate * hold_months / 12.0


def _apply_gates(lane: Lane, scope: str, financed: bool) -> None:
    """Gate the lane. Every failure is recorded with its reason."""
    fails = []
    if lane.mao <= 0:
        fails.append(f"MAO is ${lane.mao:,.0f} — the lane does not support any purchase price")
    if lane.mos < MOS_GATE:
        fails.append(f"margin of safety {lane.mos:.1%} is under the {MOS_GATE:.0%} gate")
    if lane.hold_months and lane.profit_per_month < PROFIT_PER_MONTH_GATE:
        fails.append(f"profit per month ${lane.profit_per_month:,.0f} is under the "
                     f"${PROFIT_PER_MONTH_GATE:,.0f} gate")
    if lane.verdict in ("PASS", "DANGER"):
        fails.append(f"offer is {lane.pct_over_mao:.0%} over MAO ({lane.verdict})")
    if financed:
        after_debt = lane.actual_profit - lane.financing_cost
        lane.profit_after_debt = round(after_debt)
        if after_debt < WHOLESALE_FLOOR:
            fails.append(
                f"financed: ${lane.financing_cost:,.0f} of points and interest leaves "
                f"${after_debt:,.0f} after debt, under the ${WHOLESALE_FLOOR:,.0f} "
                "wholesale floor — assigning the contract beats doing the work")
    if scope in HIGH_RISK_SCOPES:
        cushion = lane.actual_profit - lane.profit_target
        if cushion < HIGH_RISK_PROFIT_CUSHION:
            fails.append(f"{scope} scope needs ${HIGH_RISK_PROFIT_CUSHION:,.0f} above target; "
                         f"cushion is ${cushion:,.0f}")
    if lane.sale_price and lane.mao >= TIGHT_MAO_PCT * lane.sale_price:
        fails.append(f"MAO is {lane.mao / lane.sale_price:.0%} of sale price — tight-MAO flag")

    lane.reasons = fails
    lane.passed = not fails


def analyse(base_arv: float, quoted_rehab: float, offer: float,
            as_is: float = 0.0,
            scope: str = "med", state: str = "NJ", upside_arv: float = 0.0,
            walkthrough_verified: bool = False, monthly_rent: float = 0.0,
            wholetail_price: float = 0.0, end_buyer_mao: float = 0.0,
            risk_multiplier: float = 1.0, signed_contract: bool = False,
            financed: bool = False, financing_cost: float = 0.0,
            address: str = "") -> ExitAnalysis:
    _ = as_is   # accepted for symmetry with the other engines; wholetail
                # pricing uses --wholetail-price or the ARV percentage default
    if scope not in SCOPES:
        raise ValueError(f"Unknown scope {scope!r}. Use one of: {', '.join(SCOPES)}.")
    for label, v in (("base_arv", base_arv), ("quoted_rehab", quoted_rehab), ("offer", offer)):
        if v <= 0:
            raise ValueError(f"{label} is required and must be positive.")

    lanes = []

    # 1. Wholesale assignment — we never own the rehab risk.
    if end_buyer_mao <= 0:
        # Derive it: what a Med-scope investor could pay for the same house.
        derived = _score("Wholesale assignment", "assignment", base_arv, quoted_rehab,
                         0, "med", "base ARV", risk_multiplier, signed_contract)
        end_buyer_mao = derived.mao
    fee = end_buyer_mao - offer
    assign = Lane(key="assignment", name="Wholesale assignment", basis="base ARV",
                  sale_price=end_buyer_mao, actual_profit=round(fee),
                  mao=round(end_buyer_mao), hold_months=0.0,
                  verdict="BUY" if fee >= WHOLESALE_FLOOR else "PASS")
    assign.reasons = ([] if fee >= WHOLESALE_FLOOR else
                      [f"assignment fee ${fee:,.0f} is under the ${WHOLESALE_FLOOR:,.0f} floor "
                       f"(end buyer's MAO ${end_buyer_mao:,.0f} vs our ${offer:,.0f})"])
    assign.passed = not assign.reasons
    lanes.append(assign)

    # 2. Wholetail — light cosmetic, sells below a full renovation.
    wt_price = wholetail_price or base_arv * WHOLETAIL_PCT_OF_ARV
    wt_rehab = quoted_rehab * 0.30      # cosmetic slice of the full scope
    wt = _score("Wholetail", "wholetail", wt_price, wt_rehab, offer, "lite",
                f"{WHOLETAIL_PCT_OF_ARV:.0%} of base ARV", risk_multiplier, signed_contract)
    wt.financing_cost = round(financing_cost or estimate_financing_cost(
        offer, wt_rehab, wt.hold_months)) if financed else 0.0
    _apply_gates(wt, "lite", financed)
    lanes.append(wt)

    # 3. Flip, same configuration — the default underwrite.
    flip = _score("Flip (same config)", "flip", base_arv, quoted_rehab, offer, scope,
                  "base ARV", risk_multiplier, signed_contract)
    flip.financing_cost = round(financing_cost or estimate_financing_cost(
        offer, quoted_rehab, flip.hold_months)) if financed else 0.0
    _apply_gates(flip, scope, financed)
    lanes.append(flip)

    # 4. Flip with reconfiguration — upside ARV, gated on the walkthrough.
    if upside_arv > 0:
        rc = _score("Flip (reconfigured)", "flip_reconfig", upside_arv,
                    quoted_rehab * 1.25, offer, scope, "UPSIDE ARV",
                    risk_multiplier, signed_contract)
        rc.financing_cost = round(financing_cost or estimate_financing_cost(
            offer, quoted_rehab * 1.25, rc.hold_months)) if financed else 0.0
        _apply_gates(rc, scope, financed)
        if not walkthrough_verified:
            rc.passed = False
            rc.reasons.insert(0, "walkthrough has not verified the layout converts "
                                 "(plumbing runs, window egress, framing, ceiling heights) — "
                                 "upside is the buyer's, not our underwrite")
        lanes.append(rc)
    else:
        lanes.append(Lane(key="flip_reconfig", name="Flip (reconfigured)",
                          basis="UPSIDE ARV", verdict="n/a", passed=False,
                          reasons=["no upside ARV supplied — run the bedroom-band "
                                   "split in the comping step first"]))

    # 5. BRRRR — needs a rent number to be scored at all.
    if monthly_rent > 0:
        arv_refi = base_arv * BRRRR_REFI_LTV
        all_in_cost = offer + quoted_rehab * (1 + SCOPES[scope]["overrun"])
        cash_left_in = all_in_cost - arv_refi
        noi = monthly_rent * 12 * 0.55        # 45% opex load, NJ taxes are heavy
        debt_service = arv_refi * 0.075
        dscr = noi / debt_service if debt_service else 0.0
        br = Lane(key="brrrr", name="BRRRR", basis="base ARV",
                  sale_price=base_arv, mao=round(arv_refi),
                  actual_profit=round(-cash_left_in), hold_months=0.0,
                  verdict="BUY" if (cash_left_in <= 0 and dscr >= BRRRR_MIN_DSCR) else "PASS")
        rs = []
        if cash_left_in > 0:
            rs.append(f"${cash_left_in:,.0f} of cash stays in after a "
                      f"{BRRRR_REFI_LTV:.0%} refinance")
        if dscr < BRRRR_MIN_DSCR:
            rs.append(f"DSCR {dscr:.2f} is under {BRRRR_MIN_DSCR:.2f}")
        br.reasons = rs
        br.passed = not rs
        lanes.append(br)
    else:
        lanes.append(Lane(key="brrrr", name="BRRRR", basis="base ARV", verdict="n/a",
                          passed=False, reasons=["no monthly rent supplied — cannot be scored"]))

    suggested = sorted([l for l in lanes if l.passed],
                       key=lambda l: l.actual_profit, reverse=True)
    ruled_out = [l for l in lanes if not l.passed]

    if suggested:
        best = suggested[0]
        headline = (f"{best.name} clears its gates at ${best.actual_profit:,.0f} profit "
                    f"({best.verdict}, {len(suggested)} of {len(lanes)} lanes clear).")
        near = []
    else:
        scoreable = [l for l in ruled_out if l.verdict != "n/a"]
        near = sorted(scoreable, key=lambda l: l.actual_profit, reverse=True)[:2]
        headline = ("NO LANE CLEARS ITS GATE at this offer. Nothing is recommended. "
                    "The two closest misses are shown so the gap is visible — they are "
                    "not suggestions.")

    return ExitAnalysis(address=address, offer=offer, base_arv=base_arv,
                        upside_arv=upside_arv, lanes=lanes, suggested=suggested,
                        ruled_out=ruled_out, headline=headline, near_misses=near)


def render(a: ExitAnalysis) -> str:
    L = [f"EXIT STRATEGY{(' — ' + a.address) if a.address else ''}",
         f"Offer ${a.offer:,.0f} | base ARV ${a.base_arv:,.0f}"
         + (f" | upside ARV ${a.upside_arv:,.0f}" if a.upside_arv else ""),
         "", a.headline, ""]

    def block(l: Lane, mark: str) -> None:
        L.append(f"{mark} {l.name.upper()}  [{l.basis}]")
        if l.sale_price:
            L.append(f"    Sale price        ${l.sale_price:>10,.0f}")
        if l.expected_rehab:
            L.append(f"    Expected rehab    ${l.expected_rehab:>10,.0f}   "
                     f"(quoted + overrun)")
        if l.all_in:
            L.append(f"    All-in costs      ${l.all_in:>10,.0f}   "
                     f"({l.hold_months:.1f} mo hold)")
        if l.profit_target:
            L.append(f"    Profit target     ${l.profit_target:>10,.0f}   "
                     f"({l.binding_floor} floor binds)")
        if l.mao:
            L.append(f"    MAO               ${l.mao:>10,.0f}")
        L.append(f"    Profit at offer   ${l.actual_profit:>10,.0f}")
        if l.financing_cost:
            L.append(f"    less financing    -${l.financing_cost:>9,.0f}   "
                     f"(points + interest)")
            L.append(f"    Profit after debt ${l.profit_after_debt:>10,.0f}")
        if l.hold_months:
            L.append(f"    MOS {l.mos:>6.1%}  |  ${l.profit_per_month:,.0f}/mo  |  "
                     f"{l.pct_over_mao:+.0%} vs MAO  |  {l.verdict}")
        for r in l.reasons:
            L.append(f"    - {r}")
        L.append("")

    for l in a.suggested:
        block(l, "  [CLEARS]")
    if a.near_misses:
        L.append("  CLOSEST MISSES — shown for the gap, NOT recommended")
        L.append("")
        for l in a.near_misses:
            block(l, "  [MISS]  ")
    L.append("  RULED OUT")
    for l in a.ruled_out:
        if l in a.near_misses:
            continue
        why = l.reasons[0] if l.reasons else "did not clear"
        L.append(f"    {l.name}: {why}")
    L.append("")
    L.append("  Novation is not modelled as an exit lane. Seller-facing routes "
             "live in seller_options.py — the buyer's exit and our hold are "
             "different conversations.")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description="Score every exit lane against conservative ARV")
    ap.add_argument("--arv", type=float, required=True, dest="base_arv")
    ap.add_argument("--as-is", type=float, default=0.0, dest="as_is")
    ap.add_argument("--rehab", type=float, required=True, dest="quoted_rehab")
    ap.add_argument("--offer", type=float, required=True)
    ap.add_argument("--scope", default="med", choices=sorted(SCOPES))
    ap.add_argument("--state", default="NJ")
    ap.add_argument("--upside-arv", type=float, default=0.0)
    ap.add_argument("--walkthrough-verified", action="store_true")
    ap.add_argument("--rent", type=float, default=0.0, dest="monthly_rent")
    ap.add_argument("--wholetail-price", type=float, default=0.0)
    ap.add_argument("--end-buyer-mao", type=float, default=0.0)
    ap.add_argument("--risk-multiplier", type=float, default=1.0)
    ap.add_argument("--signed-contract", action="store_true")
    ap.add_argument("--financed", action="store_true",
                    help="Deal uses private money; gates profit AFTER debt service")
    ap.add_argument("--financing-cost", type=float, default=0.0,
                    help="Points + interest. Omitted, it is estimated at 12%%/2pts/85%% LTC")
    ap.add_argument("--address", default="")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        res = analyse(**vars(a))
    except ValueError as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)
    print(render(res))


if __name__ == "__main__":
    main()
