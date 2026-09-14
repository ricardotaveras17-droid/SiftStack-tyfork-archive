"""Seller Options Engine — price a cash purchase, a managed listing, and a
straight listing side by side, on the same net-to-seller math.

The point is a menu, not a number. A seller who says no to a cash offer usually
still transacts; they just transact with someone else. Showing all three routes
on identical arithmetic makes the cash offer legible instead of insulting, and
puts us in the deal on whichever route the seller actually wants.

Two audiences, deliberately kept apart:
  * THIS module is the SELLER's view — what each route nets them.
  * Exit-lane scoring (wholesale / wholetail / flip / BRRRR) is OUR view.
Never merge them onto one sheet. The buyer's exit and our hold are different
conversations, and blending them is how a seller ends up reading our margin.

THE MIDDLE ROUTE IS A MANAGED LISTING, NOT A NOVATION:
    Our version is not the investor "novation" (a three-party instrument
    substituting a new contract for the original). It is simpler: OUR OWN
    LICENSED AGENTS list the property, WE fund and run the prep, cleanout and
    repairs, and the seller keeps title until closing. That means we earn the
    commission and recover the renovation at closing rather than charging a
    separate fee — which is why the seller's net on this route is much higher
    than a fee-bearing novation would produce.

    It is deliberately the third option. Most sellers will decline it; it exists
    so the menu is real and the cash offer is legible beside it.

    This module still stops at pricing. It does not generate the listing
    agreement or the cost-recovery terms. See ``MANAGED_LISTING_NOTES``.

Usage:
  python src/seller_options.py --arv 400000 --as-is 300000 --rehab 102000 \
      --condition needs_work --state NJ
"""

import argparse
import logging
from dataclasses import dataclass, field

from deal_analyzer import calculate_transfer_tax

logger = logging.getLogger(__name__)

# ── Condition lanes ───────────────────────────────────────────────────
# Mirrors the lane math in the nj-real-estate-offer-plan skill. Keep the two in
# step: if the skill's percentages move, move them here in the same commit.
CONDITION_LANES = {
    "needs_work": {"label": "Needs Work / Poor", "move_in_pct": 0.85, "offer_pct": 0.57},
    "outdated":   {"label": "Outdated but Livable", "move_in_pct": 0.89, "offer_pct": 0.715},
    "good":       {"label": "Good / Clean", "move_in_pct": 0.92, "offer_pct": 0.77},
    "excellent":  {"label": "Excellent / Renovated", "move_in_pct": 0.95, "offer_pct": 0.795},
}

# ── Policy defaults ───────────────────────────────────────────────────
# BUSINESS TERMS, not market facts. Every one of these is a decision, and each
# is surfaced in the output as a stated assumption so nothing hides in a total.
NJ_LISTING_COMMISSION = 0.05      # total, both sides, on an outside listing
# Our own agents take the listing on the managed route AND we fund the prep, so
# the commission is discounted. It is a real differentiator in the pitch and it
# is our revenue to give up -- see internal_note() for what it costs us.
MANAGED_LISTING_COMMISSION = 0.04

# Concessions and post-inspection credits are SPECULATIVE -- they may not happen
# at all. Defaulting them above zero stacked worst-case costs onto the routes
# that compete with our cash offer while our own offer carried almost none,
# which quietly biased every comparison toward us. If a seller's own agent runs
# the numbers and lands higher than our sheet, we look like we shaded it.
# Default to zero and add them per deal when they are actually known.
SELLER_CONCESSIONS_PCT = 0.0
INSPECTION_CREDIT_PCT = 0.0
SELLER_ATTORNEY = 1_750.0       # NJ is an attorney-review state; seller pays their own
MONTHLY_INSURANCE = 150.0
MONTHLY_UTILITIES = 200.0
NJ_EFFECTIVE_TAX_PCT = 0.023    # NJ effective property tax, ~2.3%/yr of value

# Optional project fee on the managed-listing route, ON TOP of the commission
# our agents already earn. Defaults to zero: the base model is that we are paid
# by the commission and repaid the renovation at closing. Set it only if the
# route is ever actually taken and the terms say so — and note that at zero it
# is roughly break-even for us once the cost of capital on the renovation float
# is counted, which is consistent with it being a rarely-taken option.
MANAGED_LISTING_PROJECT_FEE = 0.0

# Days on market, low/high, per route.
TIMELINE_DAYS = {
    "cash": (14, 21),
    "managed_listing": (60, 120),
    "listing": (45, 90),
}

MANAGED_LISTING_NOTES = (
    "Agency disclosure: our agent represents the seller while our company funds "
    "the renovation and is repaid at closing. That is a financial interest in "
    "the transaction and has to be disclosed in writing, not just mentioned.",
    "Cost-recovery terms: put in writing what happens if the house does not "
    "sell, sells below the projection, or the seller withdraws mid-renovation. "
    "That is the term that decides who carries the risk, and it is the one "
    "worth having counsel review.",
    "The seller retains title and liability until closing and must understand "
    "that plainly.",
    "Scope agreement: the renovation scope and budget should be signed off "
    "before work starts, so the amount recovered at closing is not a surprise.",
)


@dataclass
class SellerOption:
    """One route to a sale, priced from the seller's side."""
    key: str = ""
    name: str = ""
    gross_price: float = 0.0
    deductions: dict = field(default_factory=dict)
    seller_net: float = 0.0
    days_low: int = 0
    days_high: int = 0
    certainty: str = ""
    seller_does: str = ""
    wins_when: str = ""
    caveats: list = field(default_factory=list)

    @property
    def total_deductions(self) -> float:
        return sum(self.deductions.values())


@dataclass
class SellerOptionsComparison:
    address: str = ""
    arv: float = 0.0
    as_is_value: float = 0.0
    rehab_cost: float = 0.0
    condition: str = ""
    state: str = ""
    options: list = field(default_factory=list)
    best_net: str = ""
    recommendation: str = ""
    assumptions: list = field(default_factory=list)

    def by_key(self, key: str) -> SellerOption | None:
        return next((o for o in self.options if o.key == key), None)


def _carrying_costs(value: float, months: float) -> float:
    """Taxes + insurance + utilities while the seller still owns it."""
    monthly_tax = value * NJ_EFFECTIVE_TAX_PCT / 12.0
    return (monthly_tax + MONTHLY_INSURANCE + MONTHLY_UTILITIES) * months


def cash_option(arv: float, condition: str, state: str) -> SellerOption:
    """Our as-is cash purchase. Seller pays transfer fee and their attorney."""
    lane = CONDITION_LANES[condition]
    offer = arv * lane["offer_pct"]
    ded = {
        "NJ realty transfer fee": calculate_transfer_tax(offer, state),
        "Seller attorney": SELLER_ATTORNEY,
    }
    lo, hi = TIMELINE_DAYS["cash"]
    return SellerOption(
        key="cash", name="Cash purchase (us)", gross_price=offer, deductions=ded,
        seller_net=offer - sum(ded.values()), days_low=lo, days_high=hi,
        certainty="Highest — no financing, no appraisal, no inspection contingency",
        seller_does="Nothing. No repairs, no showings, no cleanout.",
        wins_when="Speed or certainty matters more than top dollar, the house "
                  "needs work the seller cannot fund, or title/occupancy is messy.",
        caveats=["No commission and no repair credits, which is why the gross "
                 "number is lower than a retail sale."],
    )


def listing_option(as_is_value: float, state: str,
                   commission: float = NJ_LISTING_COMMISSION,
                   concessions: float = SELLER_CONCESSIONS_PCT,
                   inspection_credits: float = INSPECTION_CREDIT_PCT) -> SellerOption:
    """Straight listing, sold in current condition through an agent."""
    lo, hi = TIMELINE_DAYS["listing"]
    months = ((lo + hi) / 2.0) / 30.0
    ded = {"Agent commission": as_is_value * commission}
    if concessions > 0:
        ded["Seller concessions"] = as_is_value * concessions
    if inspection_credits > 0:
        ded["Post-inspection credits"] = as_is_value * inspection_credits
    ded.update({
        "Carrying while listed": _carrying_costs(as_is_value, months),
        "NJ realty transfer fee": calculate_transfer_tax(as_is_value, state),
        "Seller attorney": SELLER_ATTORNEY,
    })
    return SellerOption(
        key="listing", name="List it as-is (agent referral)", gross_price=as_is_value,
        deductions=ded, seller_net=as_is_value - sum(ded.values()),
        days_low=lo, days_high=hi,
        certainty="Medium — buyer financing, appraisal and inspection can all break it",
        seller_does="Cleanout, showings, inspection negotiation, and carrying "
                    "the house until it closes.",
        wins_when="The house is close to retail-ready and the seller has the "
                  "time and cash to carry it while it sells.",
        caveats=["Priced at as-is value, not ARV — a retail buyer discounts for "
                 "work they have to do themselves.",
                 "A buyer needing a mortgage may not be able to finance a house "
                 "with major system or roof issues at all."],
    )


def managed_listing_option(arv: float, rehab_cost: float, state: str,
                           project_fee: float = MANAGED_LISTING_PROJECT_FEE,
                           commission: float = MANAGED_LISTING_COMMISSION,
                           concessions: float = SELLER_CONCESSIONS_PCT
                           ) -> SellerOption:
    """Our agents list it; we fund the prep and are repaid at closing.

    Not a novation. We are paid by the commission our own licensed agents earn,
    and we recover the renovation spend at closing -- there is no separate fee
    unless ``project_fee`` is set.
    """
    lo, hi = TIMELINE_DAYS["managed_listing"]
    months = ((lo + hi) / 2.0) / 30.0
    ded = {
        "Renovation + cleanout (we fund)": rehab_cost,
        f"Agent commission (our team, {commission:.1%})": arv * commission,
    }
    if concessions > 0:
        ded["Seller concessions"] = arv * concessions
    ded.update({
        "Carrying (we cover)": _carrying_costs(arv, months),
        "NJ realty transfer fee": calculate_transfer_tax(arv, state),
        "Seller attorney": SELLER_ATTORNEY,
    })
    if project_fee > 0:
        ded["Project management fee"] = project_fee
    return SellerOption(
        key="managed_listing", name="We list it for you (we fund the prep)",
        gross_price=arv, deductions=ded, seller_net=arv - sum(ded.values()),
        days_low=lo, days_high=hi,
        certainty="Medium — we fund and run the work, but the retail sale still "
                  "has to happen at the projected price",
        seller_does="Keeps title, allows access for the work, signs at closing. "
                    "Pays for none of the renovation up front.",
        wins_when="The house needs work the seller cannot fund, and they have "
                  "the patience to wait for a retail sale. Usually the highest "
                  "net of the three -- and usually still declined, because it "
                  "is the slowest and the seller keeps the risk.",
        caveats=["Seller retains title and liability until closing.",
                 "Net is a projection, not a guarantee — the house still has to "
                 "sell at the projected price.",
                 "Longest timeline of the three.",
                 "Our agent represents the seller while our company funds the "
                 "work; that financial interest gets disclosed in writing."],
    )


def compare_options(arv: float, as_is_value: float, rehab_cost: float,
                    condition: str, state: str, address: str = "",
                    project_fee: float = MANAGED_LISTING_PROJECT_FEE,
                    commission: float = NJ_LISTING_COMMISSION,
                    our_commission: float = MANAGED_LISTING_COMMISSION,
                    concessions: float = SELLER_CONCESSIONS_PCT,
                    inspection_credits: float = INSPECTION_CREDIT_PCT
                    ) -> SellerOptionsComparison:
    """Price all three routes on the same math and say which one wins."""
    if condition not in CONDITION_LANES:
        raise ValueError(
            f"Unknown condition {condition!r}. Use one of: "
            f"{', '.join(CONDITION_LANES)}.")
    for label, val in (("arv", arv), ("as_is_value", as_is_value)):
        if val <= 0:
            raise ValueError(
                f"{label} is required and must be positive. This engine compares "
                "routes on real numbers; it will not fill one in with a guess.")
    if rehab_cost <= 0:
        raise ValueError(
            "rehab_cost is required. Without it the managed-listing lane cannot "
            "be priced, and a comparison missing a lane is worse than none.")

    # Coherence gate. A retail buyer who pays `as_is` and then spends `rehab`
    # ends up owning a house worth `arv`. If as_is >= arv - rehab that buyer is
    # underwater the moment they close, so no such buyer exists and the listing
    # route is fiction. The arithmetic still produces a confident-looking number,
    # which is exactly why this has to be an error and not a footnote: the
    # engine would otherwise advise a seller to list a house nobody can buy.
    headroom = arv - rehab_cost
    if as_is_value >= headroom:
        raise ValueError(
            f"Incoherent inputs: as-is ${as_is_value:,.0f} is not below "
            f"ARV minus rehab (${headroom:,.0f}). A retail buyer paying as-is "
            f"and spending ${rehab_cost:,.0f} would own a ${arv:,.0f} house at a "
            "loss, so the listing route cannot be priced. One of the three "
            "numbers is wrong — usually as-is is optimistic, or the rehab "
            "figure is for a fuller scope than the ARV comps reflect."
        )

    # Soft check: a retail buyer taking on the work needs a discount for the
    # hassle and risk, conventionally a chunk of the rehab number. If as-is sits
    # right up against the ceiling, it is probably optimistic.
    thin_margin = as_is_value > headroom - 0.15 * rehab_cost

    opts = [cash_option(arv, condition, state),
            managed_listing_option(arv, rehab_cost, state, project_fee,
                                   our_commission, concessions),
            listing_option(as_is_value, state, commission, concessions,
                           inspection_credits)]

    best = max(opts, key=lambda o: o.seller_net)
    cash = next(o for o in opts if o.key == "cash")
    gap = best.seller_net - cash.seller_net

    if best.key == "cash":
        rec = ("Cash is both the fastest and the highest net here — the retail "
               "routes lose more to commission, credits and carrying than the "
               "higher gross price recovers.")
    elif gap < 0.02 * arv:
        rec = (f"{best.name} nets about ${gap:,.0f} more than cash, under 2% of "
               "value. That is inside the noise of a retail sale; lead with cash "
               "on speed and certainty and let the seller choose.")
    elif best.key == "managed_listing":
        # This route wins on paper almost every time, because at no project fee
        # the seller captures the whole renovation uplift and we are paid only
        # by commission. Saying so keeps the comparison honest: sellers decline
        # it for time, risk and trust, not because the number is worse.
        rec = (f"On paper {best.name} nets roughly ${gap:,.0f} more than cash — "
               "and it usually will, because the seller keeps the full "
               "renovation uplift while we are paid by commission. The trade is "
               f"real though: {best.days_low}-{best.days_high} days, the seller "
               "keeps title and the market risk, and the net is a projection "
               "rather than a guarantee. Most sellers still take the cash. "
               "Present all three and let them decide rather than steering.")
    else:
        rec = (f"{best.name} nets roughly ${gap:,.0f} more than cash. Present it "
               "as the seller's best-dollar route and be honest that it takes "
               f"{best.days_low}-{best.days_high} days and carries market risk.")

    if thin_margin:
        listing = next(o for o in opts if o.key == "listing")
        listing.caveats.insert(0,
            "As-is value sits close to ARV minus rehab, leaving a retail buyer "
            "almost no discount for taking on the work. Treat this listing net "
            "as an optimistic ceiling and re-check the as-is comps.")

    assumptions = [
        f"Commission {commission:.1%} on an outside listing; {our_commission:.1%} "
        "when our own agents take it — we discount it because we are also "
        "funding the prep",
        ("No seller concessions or post-inspection credits assumed. They are "
         "speculative and are left out rather than stacked onto the routes that "
         "compete with our offer; add them with --concessions / "
         "--inspection-credits when a deal actually has them.")
        if concessions == 0 and inspection_credits == 0 else
        f"Seller concessions {concessions:.1%}; post-inspection credits "
        f"{inspection_credits:.1%} on the as-is listing route",
        f"Seller attorney ${SELLER_ATTORNEY:,.0f}",
        f"Carrying: NJ effective property tax {NJ_EFFECTIVE_TAX_PCT:.1%}/yr plus "
        f"${MONTHLY_INSURANCE + MONTHLY_UTILITIES:,.0f}/mo insurance and utilities",
        "Managed listing: we earn the commission and recover the renovation at "
        "closing; no separate fee unless --project-fee is set",
        "NJ realty transfer fee computed on the actual bracket schedule, per route",
        "Mortgage payoff is excluded: it is identical on all three routes and "
        "would cancel out of the comparison",
    ]

    return SellerOptionsComparison(
        address=address, arv=arv, as_is_value=as_is_value, rehab_cost=rehab_cost,
        condition=condition, state=state, options=opts, best_net=best.key,
        recommendation=rec, assumptions=assumptions)


def render_comparison(cmp_: SellerOptionsComparison) -> str:
    """Plain-text block a rep can paste in front of a seller."""
    L = []
    if cmp_.address:
        L.append(f"YOUR OPTIONS — {cmp_.address}")
    else:
        L.append("YOUR OPTIONS")
    lane = CONDITION_LANES[cmp_.condition]["label"]
    L.append(f"Condition: {lane}   After-repair value: ${cmp_.arv:,.0f}   "
             f"As-is value: ${cmp_.as_is_value:,.0f}")
    L.append("")
    for o in cmp_.options:
        star = "  ★ HIGHEST NET" if o.key == cmp_.best_net else ""
        L.append(f"{o.name.upper()}{star}")
        L.append(f"  Sale price                ${o.gross_price:>11,.0f}")
        for k, v in o.deductions.items():
            L.append(f"    less {k:<34} -${v:>10,.0f}")
        L.append(f"  YOU NET                   ${o.seller_net:>11,.0f}")
        L.append(f"  Timeline                  {o.days_low}-{o.days_high} days")
        L.append(f"  Certainty                 {o.certainty}")
        L.append(f"  You handle                {o.seller_does}")
        L.append("")
    L.append("RECOMMENDATION")
    L.append(f"  {cmp_.recommendation}")
    L.append("")
    L.append("ASSUMPTIONS")
    for a in cmp_.assumptions:
        L.append(f"  - {a}")
    return "\n".join(L)


def internal_note(cmp_: SellerOptionsComparison,
                  commission: float = MANAGED_LISTING_COMMISSION,
                  cost_of_capital: float = 0.10) -> str:
    """OUR side of the managed-listing route. Never show this to a seller.

    The seller-facing block deliberately hides our position; this exists so the
    thinness of the route is visible to us before we offer it.
    """
    ml = cmp_.by_key("managed_listing")
    if ml is None:
        return ""
    months = ((ml.days_low + ml.days_high) / 2.0) / 12.0 / 30.0 * 12.0
    gross_commission = cmp_.arv * commission
    float_cost = cmp_.rehab_cost * cost_of_capital * (months / 12.0)
    fee = ml.deductions.get("Project management fee", 0.0)
    net = gross_commission + fee - float_cost
    return "\n".join([
        "INTERNAL — NOT FOR THE SELLER",
        f"  Managed-listing route, our position:",
        f"    Commission earned          ${gross_commission:>10,.0f}  "
        f"(at our discounted {commission:.1%})",
        f"    Project fee                ${fee:>10,.0f}",
        f"    Cost of floating the reno  -${float_cost:>9,.0f}  "
        f"(${cmp_.rehab_cost:,.0f} for ~{months:.1f} mo at {cost_of_capital:.0%})",
        f"    Approx. net to us          ${net:>10,.0f}",
        "  Excludes our project-management time and the risk that the resale "
        "lands under the projection.",
        "  If this route is ever actually taken, set --project-fee to something "
        "that reflects the work.",
    ])


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Price cash / managed listing / straight listing for a seller")
    ap.add_argument("--arv", type=float, required=True, help="After-repair value")
    ap.add_argument("--as-is", type=float, required=True, dest="as_is",
                    help="As-is value in current condition")
    ap.add_argument("--rehab", type=float, required=True, help="Rehab cost to reach ARV")
    ap.add_argument("--condition", required=True, choices=sorted(CONDITION_LANES))
    ap.add_argument("--state", default="NJ")
    ap.add_argument("--address", default="")
    ap.add_argument("--project-fee", type=float,
                    default=MANAGED_LISTING_PROJECT_FEE,
                    help="Optional fee on the managed-listing route, on top of commission")
    ap.add_argument("--commission", type=float, default=NJ_LISTING_COMMISSION,
                    help="Commission on an outside listing (default 5%%)")
    ap.add_argument("--our-commission", type=float, default=MANAGED_LISTING_COMMISSION,
                    help="Commission when our own agents list it (default 4%%)")
    ap.add_argument("--concessions", type=float, default=SELLER_CONCESSIONS_PCT,
                    help="Seller concessions as a decimal, e.g. 0.02. Default 0")
    ap.add_argument("--inspection-credits", type=float, default=INSPECTION_CREDIT_PCT,
                    help="Post-inspection credits on the as-is route. Default 0")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        cmp_ = compare_options(arv=a.arv, as_is_value=a.as_is, rehab_cost=a.rehab,
                               condition=a.condition, state=a.state,
                               address=a.address, project_fee=a.project_fee,
                               commission=a.commission,
                               our_commission=a.our_commission,
                               concessions=a.concessions,
                               inspection_credits=a.inspection_credits)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)
    print(render_comparison(cmp_))
    print()
    print(internal_note(cmp_, commission=a.our_commission))
    print("\nMANAGED LISTING — SETTLE THESE BEFORE RUNNING ONE:")
    for n in MANAGED_LISTING_NOTES:
        print(f"  - {n}")


if __name__ == "__main__":
    main()
