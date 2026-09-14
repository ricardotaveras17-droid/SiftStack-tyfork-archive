"""Lender Analysis — the private-money view of a deal.

Answers the two questions a private lender actually asks: how exposed am I
against the value, and what do I earn. Then answers the one the borrower asks:
what is left after the debt is paid.

Ported from the post-walkthrough Lender Analysis sheet and retargeted for New
Jersey. Two things changed in the port rather than being carried over:

  * Selling costs run through ``deal_analyzer.calculate_selling_costs``, so the
    NJ realty transfer fee is computed on its real bracket schedule instead of
    being folded into a flat 8% retail-exit assumption.
  * Buy-side closing constants are NJ figures, not the Tennessee ones.

Usage:
  python src/lender_analysis.py --contract 228000 --rehab 102000 --arv 400000 \
      --arv-low 380000 --rate 0.12 --points 2 --term 9 --ltc 0.85
"""

import argparse
import logging
from dataclasses import dataclass, field

from deal_analyzer import calculate_selling_costs

logger = logging.getLogger(__name__)

# ── NJ buy-side closing ───────────────────────────────────────────────
# CONFIRM: NJ figures, not bid-verified. The upstream values were Tennessee
# ($900 flat / 0.77% title) and would understate an NJ purchase.
NJ_BUY_ATTORNEY = 1_500.0     # buyer's attorney on the purchase
NJ_BUY_TITLE_PCT = 0.006      # title insurance + search + recording, buy side

HOLDING_COST_PCT_ANNUAL = 0.04   # carry, insurance, utilities as % of contract/yr

# Exposure thresholds. Above these a private lender is usually being asked to
# take equity risk on debt pricing, which is worth saying out loud.
LTARV_COMFORTABLE = 0.70
LTARV_STRETCHED = 0.75


@dataclass(frozen=True)
class LoanTerms:
    rate: float                 # annual interest, e.g. 0.12
    points: float               # origination points, e.g. 2.0
    term_months: int
    ltc: float                  # loan to cost, e.g. 0.85
    draws: int = 4
    lender: str = ""
    assumed: bool = True        # placeholder until a signed term sheet lands


@dataclass
class LenderAnalysis:
    # sources and uses
    contract: float = 0.0
    rehab: float = 0.0
    loan: float = 0.0
    points_cost: float = 0.0
    interest_full: float = 0.0
    buy_closing: float = 0.0
    cash_in: float = 0.0
    total_project: float = 0.0
    # draw schedule
    initial_draw: float = 0.0
    per_draw: float = 0.0
    draw_count: int = 0
    # exposure
    arv: float = 0.0
    arv_low: float = 0.0
    ltarv: float = 0.0
    ltarv_stress: float = 0.0
    equity_cushion: float = 0.0
    equity_cushion_stress: float = 0.0
    # payoff waterfall
    payoff: float = 0.0
    sell_net: float = 0.0
    sell_net_stress: float = 0.0
    coverage: float = 0.0
    coverage_stress: float = 0.0
    # returns
    lender_dollars: float = 0.0
    lender_annual_yield: float = 0.0
    borrower_net: float = 0.0
    borrower_cash_on_cash: float = 0.0
    terms_assumed: bool = True
    warnings: list = field(default_factory=list)


def analyse(contract: float, rehab: float, arv: float, terms: LoanTerms,
            state: str, arv_low: float = 0.0) -> LenderAnalysis:
    """Run the lender view. ``arv_low`` drives the band-floor stress case."""
    if contract <= 0 or rehab <= 0 or arv <= 0:
        raise ValueError(
            "contract, rehab and arv are all required and must be positive — a "
            "lender package built on a missing input is not a package.")
    if not arv_low:
        arv_low = arv * 0.95

    loan = round((contract + rehab) * terms.ltc)
    points_cost = round(loan * terms.points / 100.0)
    interest_full = round(loan * terms.rate * terms.term_months / 12.0)
    buy_closing = round(NJ_BUY_ATTORNEY + contract * NJ_BUY_TITLE_PCT)
    initial_draw = round(contract * terms.ltc)
    per_draw = round((loan - initial_draw) / max(terms.draws, 1))
    holding = round(contract * HOLDING_COST_PCT_ANNUAL * terms.term_months / 12.0)

    total_project = contract + buy_closing + rehab + points_cost + interest_full
    cash_in = points_cost + buy_closing + max(round(contract + rehab - loan), 0)

    # Real NJ selling costs, not a flat retail-exit percentage.
    sell_net = arv - calculate_selling_costs(arv, state=state).total
    sell_net_stress = arv_low - calculate_selling_costs(arv_low, state=state).total

    payoff = loan + interest_full
    ltarv = loan / arv if arv else 0.0
    ltarv_stress = loan / arv_low if arv_low else 0.0

    lender_dollars = points_cost + interest_full
    # Points are earned once over the term, so annualise them across it.
    ann_yield = terms.rate + (terms.points / 100.0) * (12.0 / max(terms.term_months, 1))

    borrower_net = round(sell_net - payoff - cash_in - holding)
    coc = (borrower_net / cash_in) if cash_in else 0.0

    warn = []
    if terms.assumed:
        warn.append(
            "TERMS ARE ASSUMED. Rate, points, term and LTC are modelling "
            "placeholders. Replace with the signed term sheet before this goes "
            "to a lender.")
    if ltarv > LTARV_STRETCHED:
        warn.append(
            f"Loan-to-ARV is {ltarv:.0%}, above {LTARV_STRETCHED:.0%}. At that "
            "level the lender is taking equity risk at debt pricing; expect "
            "them to push back or reprice.")
    elif ltarv > LTARV_COMFORTABLE:
        warn.append(
            f"Loan-to-ARV is {ltarv:.1%} — workable but not comfortable. Under "
            f"{LTARV_COMFORTABLE:.0%} is an easier conversation.")
    if coverage_stress_fail := (sell_net_stress < payoff):
        warn.append(
            f"STRESS CASE FAILS: at the band floor of ${arv_low:,.0f} the net "
            f"sale (${sell_net_stress:,.0f}) does not cover the payoff "
            f"(${payoff:,.0f}). The lender is short ${payoff - sell_net_stress:,.0f} "
            "before the borrower sees a dollar.")
    if borrower_net < 0:
        warn.append(
            f"Borrower net is negative (${borrower_net:,.0f}). The deal does not "
            "service its own debt at this contract price.")
    _ = coverage_stress_fail

    return LenderAnalysis(
        contract=contract, rehab=rehab, loan=loan, points_cost=points_cost,
        interest_full=interest_full, buy_closing=buy_closing, cash_in=cash_in,
        total_project=round(total_project), initial_draw=initial_draw,
        per_draw=per_draw, draw_count=terms.draws, arv=arv, arv_low=arv_low,
        ltarv=ltarv, ltarv_stress=ltarv_stress,
        equity_cushion=round(arv - payoff), equity_cushion_stress=round(arv_low - payoff),
        payoff=payoff, sell_net=round(sell_net), sell_net_stress=round(sell_net_stress),
        coverage=(sell_net / payoff if payoff else 0.0),
        coverage_stress=(sell_net_stress / payoff if payoff else 0.0),
        lender_dollars=lender_dollars, lender_annual_yield=ann_yield,
        borrower_net=borrower_net, borrower_cash_on_cash=coc,
        terms_assumed=terms.assumed, warnings=warn)


def render(a: LenderAnalysis, terms: LoanTerms, address: str = "") -> str:
    L = []
    head = f"PRIVATE LENDER ANALYSIS{(' — ' + address) if address else ''}"
    L.append(head)
    if terms.lender:
        L.append(f"Lender: {terms.lender}")
    L.append("")
    for w in a.warnings:
        L.append(f"  !! {w}")
    if a.warnings:
        L.append("")
    L.append("TERMS")
    L.append(f"  Rate {terms.rate:.2%} | {terms.points:g} points | "
             f"{terms.term_months} months | LTC {terms.ltc:.0%} | {terms.draws} draws")
    L.append("")
    L.append("SOURCES AND USES")
    for label, v in (("Contract price", a.contract), ("Renovation budget", a.rehab),
                     ("Buy-side closing", a.buy_closing), ("Origination points", a.points_cost),
                     ("Interest, full term", a.interest_full)):
        L.append(f"  {label:<26} ${v:>10,.0f}")
    L.append(f"  {'TOTAL PROJECT':<26} ${a.total_project:>10,.0f}")
    L.append(f"  {'Loan amount':<26} ${a.loan:>10,.0f}")
    L.append(f"  {'Borrower cash in':<26} ${a.cash_in:>10,.0f}")
    L.append("")
    L.append("DRAW SCHEDULE")
    L.append(f"  {'Initial advance at closing':<26} ${a.initial_draw:>10,.0f}")
    L.append(f"  {str(a.draw_count) + ' rehab draws of':<26} ${a.per_draw:>10,.0f}")
    L.append("")
    L.append("EXPOSURE VS VALUE")
    L.append(f"  {'Loan to ARV':<26} {a.ltarv:>10.1%}   (ARV ${a.arv:,.0f})")
    L.append(f"  {'Loan to ARV, band floor':<26} {a.ltarv_stress:>10.1%}   "
             f"(floor ${a.arv_low:,.0f})")
    L.append(f"  {'Equity cushion':<26} ${a.equity_cushion:>10,.0f}")
    L.append(f"  {'Equity cushion at floor':<26} ${a.equity_cushion_stress:>10,.0f}")
    L.append("")
    L.append("PAYOFF WATERFALL")
    L.append(f"  {'Net sale proceeds':<26} ${a.sell_net:>10,.0f}")
    L.append(f"  {'less lender payoff':<26} -${a.payoff:>9,.0f}   "
             f"(principal + interest)")
    L.append(f"  {'Coverage':<26} {a.coverage:>10.2f}x")
    L.append(f"  {'Coverage at band floor':<26} {a.coverage_stress:>10.2f}x   "
             f"(net ${a.sell_net_stress:,.0f})")
    L.append("")
    L.append("LENDER RETURN")
    L.append(f"  {'Points + interest':<26} ${a.lender_dollars:>10,.0f}")
    L.append(f"  {'Annualised yield':<26} {a.lender_annual_yield:>10.2%}")
    L.append("")
    L.append("BORROWER POSITION")
    L.append(f"  {'Net after debt':<26} ${a.borrower_net:>10,.0f}")
    L.append(f"  {'Cash-on-cash':<26} {a.borrower_cash_on_cash:>10.1%}   "
             f"(on ${a.cash_in:,.0f} in)")
    L.append("")
    L.append("  Note: financed profit reads as cash-on-cash, not as a margin on "
             "value. A financed flip must still clear the wholesale floor to be "
             "worth doing.")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description="Private lender view of a deal")
    ap.add_argument("--contract", type=float, required=True)
    ap.add_argument("--rehab", type=float, required=True)
    ap.add_argument("--arv", type=float, required=True)
    ap.add_argument("--arv-low", type=float, default=0.0,
                    help="Band floor for the stress case (default 95%% of ARV)")
    ap.add_argument("--rate", type=float, default=0.12)
    ap.add_argument("--points", type=float, default=2.0)
    ap.add_argument("--term", type=int, default=9, dest="term_months")
    ap.add_argument("--ltc", type=float, default=0.85)
    ap.add_argument("--draws", type=int, default=4)
    ap.add_argument("--lender", default="")
    ap.add_argument("--signed", action="store_true",
                    help="Terms come from a signed term sheet, not placeholders")
    ap.add_argument("--state", default="NJ")
    ap.add_argument("--address", default="")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    terms = LoanTerms(rate=a.rate, points=a.points, term_months=a.term_months,
                      ltc=a.ltc, draws=a.draws, lender=a.lender,
                      assumed=not a.signed)
    try:
        res = analyse(a.contract, a.rehab, a.arv, terms, a.state, a.arv_low)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)
    print(render(res, terms, a.address))


if __name__ == "__main__":
    main()
