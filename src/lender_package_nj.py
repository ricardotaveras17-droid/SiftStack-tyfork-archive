"""Lender Package Builder — the workbook handed to a private money lender.

Eight tabs, and the numbers are LIVE. Every figure that matters is an Excel
formula off a workbook defined name, so the lender (or Rick) can change a blue
input cell in Excel and the whole package recalculates. A workbook of frozen
literals is a PDF with extra steps; it cannot survive the negotiation where the
loan amount actually gets set.

Two safety mechanics, both learned from a real failure and both worth keeping:

  1. DAY ONE IS DERIVED, NEVER TYPED.  DayOne = Loan - RehabTotal. Upstream,
     typing the closing advance let financed closing costs land in the draw
     tranche, and the holdback disagreed with the repair budget by $1,608.
     ``test_lender_package.py`` asserts the cell holds a formula, not a number.

  2. READ THE WORKBOOK BACK BEFORE REGENERATING OVER IT.  Lenders and partners
     review in Excel and edit inputs directly, and they do not always mention
     every change they made. Rebuilding blind silently discards them. This
     module reads the existing input cells first, reports any that differ from
     the spec, and refuses to overwrite without --force.

New Jersey specifics: the realty transfer fee is written as a live bracket
formula (two tables, switching at $350,000, plus the graduated percent fee over
$1M), so it moves with the resale price instead of being frozen. The security
instrument is a MORTGAGE, not a deed of trust -- NJ is a lien-theory state.

Usage:
  python src/lender_package.py --spec deals/my_deal.json --out output/pkg.xlsx
"""

import argparse
import json
import logging
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.workbook.defined_name import DefinedName

logger = logging.getLogger(__name__)

MONEY = '"$"#,##0'
MONEY2 = '"$"#,##0.00'
PCT1, PCT2 = "0.0%", "0.00%"
MULT = '0.00"x"'
NUM = "#,##0"

NAVY = "14243E"
GOLD = "C9A44C"
INPUT_FILL = PatternFill("solid", fgColor="DCE9F7")   # blue = you may edit
CALC_FILL = PatternFill("solid", fgColor="F2F2F2")    # grey = derived
WARN_FONT = Font(bold=True, color="C00000")
H1 = Font(bold=True, size=15, color=NAVY)
H2 = Font(bold=True, size=11, color="FFFFFF")
BAND = PatternFill("solid", fgColor=NAVY)
LBL = Font(size=10)
BOLD = Font(bold=True, size=10)
THIN = Side(style="thin", color="D0D0D0")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

SHEET_ORDER = ["Deal Overview", "Term Sheet", "Your Investment", "Repair Costs",
               "Resale Value", "Comps", "Backups and Risk", "Next Steps"]

# Every input the lender may edit. name -> (sheet, cell, spec path, number fmt)
# read_back_inputs() checks exactly these, so anything editable must be listed.
INPUTS = [
    ("Purchase",     "Term Sheet", "B5",  "contract_price",        MONEY),
    ("RehabTotal",   "Term Sheet", "B6",  "work_budget",           MONEY),
    ("Loan",         "Term Sheet", "B7",  "loan.amount",           MONEY),
    ("Rate",         "Term Sheet", "B8",  "loan.rate",             PCT2),
    ("Points",       "Term Sheet", "B9",  "loan.points",           NUM),
    ("TermMonths",   "Term Sheet", "B10", "loan.term_months",      NUM),
    ("MinIntMonths", "Term Sheet", "B11", "loan.min_interest_months", NUM),
    ("Draws",        "Term Sheet", "B12", "loan.draws",            NUM),
    ("ARV",          "Resale Value", "B5", "resale.point",         MONEY),
    ("ARVLow",       "Resale Value", "B6", "resale.lo",            MONEY),
    ("ARVHigh",      "Resale Value", "B7", "resale.hi",            MONEY),
    ("AsIs",         "Resale Value", "B8", "as_is.point",          MONEY),
    ("TaxesAnnual",  "Your Investment", "B5", "holding.taxes_annual",   MONEY),
    ("InsAnnual",    "Your Investment", "B6", "holding.insurance_annual", MONEY),
    ("UtilMonthly",  "Your Investment", "B7", "holding.utilities_monthly", MONEY),
    ("BuyAttorney",  "Your Investment", "B8", "buying_costs.attorney", MONEY),
    ("BuyTitlePct",  "Your Investment", "B9", "buying_costs.title_pct", PCT2),
    ("SellCommPct",  "Your Investment", "B10", "selling_costs.realtor_pct", PCT2),
]

DEFAULTS = {
    "contract_price": 0.0, "work_budget": 0.0,
    "loan.rate": 0.12, "loan.points": 2.0, "loan.term_months": 9,
    "loan.min_interest_months": 3, "loan.draws": 4,
    "holding.taxes_annual": 0.0, "holding.insurance_annual": 1_800.0,
    "holding.utilities_monthly": 200.0,
    "buying_costs.attorney": 1_500.0, "buying_costs.title_pct": 0.006,
    "selling_costs.realtor_pct": 0.05,
}


def dig(spec: dict, path: str, default=None):
    cur = spec
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return DEFAULTS.get(path, default)
        cur = cur[part]
    return cur if cur is not None else DEFAULTS.get(path, default)


# ══ NJ realty transfer fee, as a LIVE formula ═════════════════════════
# Two bracket tables switching wholly at $350,000, plus the graduated percent
# fee over $1M applied to the entire consideration. Written as a formula rather
# than a computed literal so the fee tracks the resale price when a lender
# edits it. Mirrors deal_analyzer.calculate_transfer_tax(); the test asserts
# the two agree.
_RTF_TO_350K = ((150_000, 2.00), (200_000, 3.35), (350_000, 3.90))
_RTF_OVER_350K = ((150_000, 2.90), (200_000, 4.25), (550_000, 4.80),
                  (850_000, 5.30), (1_000_000, 5.80), (None, 6.05))


def _bracket_sum(ref: str, table) -> str:
    parts, lower = [], 0
    for upper, per500 in table:
        if upper is None:
            parts.append(f"{per500}*MAX(0,{ref}-{lower})/500")
        else:
            parts.append(f"{per500}*MAX(0,MIN({ref},{upper})-{lower})/500")
            lower = upper
    return "+".join(parts)


def nj_transfer_fee_formula(ref: str) -> str:
    """Excel expression for the NJ RTF on the value in ``ref``."""
    rounded = f"CEILING({ref},500)"
    under = _bracket_sum(rounded, _RTF_TO_350K)
    over = _bracket_sum(rounded, _RTF_OVER_350K)
    graduated = (f"IF({rounded}>1000000,{rounded}*"
                 f"IF({rounded}<=2000000,0.01,"
                 f"IF({rounded}<=2500000,0.02,"
                 f"IF({rounded}<=3000000,0.025,"
                 f"IF({rounded}<=3500000,0.03,0.035)))),0)")
    return f"IF({rounded}<=350000,{under},{over})+{graduated}"


# ══ formatting helpers ════════════════════════════════════════════════

def _title(ws, text, sub=""):
    ws["A1"] = text
    ws["A1"].font = H1
    if sub:
        ws["A2"] = sub
        ws["A2"].font = Font(size=9, italic=True, color="5A6878")
    return 4


def _band(ws, row, text):
    c = ws.cell(row=row, column=1, value=text)
    c.font, c.fill = H2, BAND
    for col in range(2, 5):
        ws.cell(row=row, column=col).fill = BAND
    return row + 1


def _kv(ws, row, label, value, fmt=None, fill=CALC_FILL, note=""):
    ws.cell(row=row, column=1, value=label).font = LBL
    c = ws.cell(row=row, column=2, value=value)
    c.fill, c.border = fill, BOX
    if fmt:
        c.number_format = fmt
    if note:
        n = ws.cell(row=row, column=3, value=note)
        n.font = Font(size=9, italic=True, color="5A6878")
    return row + 1


def _para(ws, row, text, bold=False, color=None, span=4):
    c = ws.cell(row=row, column=1, value=text)
    c.font = Font(size=10, bold=bold, color=color or "000000")
    c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=span)
    ws.row_dimensions[row].height = max(15, 13 * (len(text) // 95 + 1))
    return row + 1


def _widths(ws, widths):
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _defname(wb, name, sheet, cell):
    ref = f"'{sheet}'!${cell[0]}${cell[1:]}"
    wb.defined_names.add(DefinedName(name, attr_text=ref))


# ══ Python twin of the sheet formulas ═════════════════════════════════

def compute(spec: dict) -> dict:
    """Mirror of the workbook formulas, for tests, logs and lender_docs.

    If this and the workbook ever disagree, the workbook is authoritative for
    the lender and this is the bug -- the test exists to keep them together.
    """
    from deal_analyzer import calculate_transfer_tax

    purchase = float(dig(spec, "contract_price", 0))
    rehab = float(dig(spec, "work_budget", 0))
    loan = float(dig(spec, "loan.amount", 0))
    rate = float(dig(spec, "loan.rate"))
    points = float(dig(spec, "loan.points"))
    term = int(dig(spec, "loan.term_months"))
    min_int_months = int(dig(spec, "loan.min_interest_months"))
    draws = int(dig(spec, "loan.draws"))
    arv = float(dig(spec, "resale.point", 0))
    arv_lo = float(dig(spec, "resale.lo", arv) or arv)
    as_is = float(dig(spec, "as_is.point", 0) or 0)
    state = dig(spec, "property.state", "NJ") or "NJ"

    if purchase <= 0 or rehab <= 0 or loan <= 0 or arv <= 0:
        raise ValueError(
            "contract_price, work_budget, loan.amount and resale.point are all "
            "required and must be positive. A lender package with a missing "
            "input is not a package.")

    hold_monthly = (dig(spec, "holding.taxes_annual") / 12.0
                    + dig(spec, "holding.insurance_annual") / 12.0
                    + dig(spec, "holding.utilities_monthly"))
    hold_total = hold_monthly * term
    buy_costs = dig(spec, "buying_costs.attorney") + purchase * dig(spec, "buying_costs.title_pct")

    points_cost = loan * points / 100.0
    interest = loan * rate * term / 12.0
    min_interest = loan * rate * min_int_months / 12.0
    payoff = loan + interest
    fin_costs = points_cost + interest

    # DERIVED. Never typed. See the module docstring.
    day_one = loan - rehab
    rehab_tranche = max(loan - day_one, 0.0)
    per_draw = rehab_tranche / draws if draws else 0.0

    comm = arv * dig(spec, "selling_costs.realtor_pct")
    rtf = calculate_transfer_tax(arv, state)
    rtf_lo = calculate_transfer_tax(arv_lo, state)
    sell_costs = comm + rtf
    sell_costs_lo = arv_lo * dig(spec, "selling_costs.realtor_pct") + rtf_lo

    net_proceeds = arv - sell_costs
    net_proceeds_lo = arv_lo - sell_costs_lo
    total_cost = purchase + rehab + buy_costs + hold_total + fin_costs
    borrower_cash = max(purchase + rehab + buy_costs + hold_total - loan, 0.0)
    net_profit = net_proceeds - total_cost

    return {
        "purchase": round(purchase), "rehab": round(rehab), "loan": round(loan),
        "rate": rate, "points": points, "term": term, "draws": draws,
        "min_interest_months": min_int_months, "min_interest": round(min_interest),
        "points_cost": round(points_cost), "interest": round(interest),
        "payoff": round(payoff), "fin_costs": round(fin_costs),
        "day_one": round(day_one), "rehab_tranche": round(rehab_tranche),
        "per_draw": round(per_draw), "hold_monthly": round(hold_monthly),
        "hold_total": round(hold_total), "buy_costs": round(buy_costs),
        "comm": round(comm), "rtf": round(rtf), "sell_costs": round(sell_costs),
        "arv": round(arv), "arv_lo": round(arv_lo), "as_is": round(as_is),
        "net_proceeds": round(net_proceeds), "net_proceeds_lo": round(net_proceeds_lo),
        "total_cost": round(total_cost), "borrower_cash": round(borrower_cash),
        "net_profit": round(net_profit),
        "ltarv": loan / arv if arv else 0.0,
        "ltarv_stress": loan / arv_lo if arv_lo else 0.0,
        "equity_cushion": round(arv - payoff),
        "coverage": net_proceeds / payoff if payoff else 0.0,
        "coverage_stress": net_proceeds_lo / payoff if payoff else 0.0,
        "day_one_cover": as_is / day_one if day_one else 0.0,
        "ann_yield": rate + (points / 100.0) * (12.0 / term) if term else rate,
        "lender_dollars": round(points_cost + interest),
    }


# ══ sheets ════════════════════════════════════════════════════════════

def sheet_terms(wb, spec):
    ws = wb.create_sheet("Term Sheet")
    _widths(ws, [34, 18, 46, 14])
    r = _title(ws, "Term Sheet",
               "Blue cells are inputs — change one and the whole package recalculates.")
    r = _band(ws, r, "LOAN")
    r = _kv(ws, r, "Purchase price", float(dig(spec, "contract_price", 0)), MONEY, INPUT_FILL)
    r = _kv(ws, r, "Renovation budget", float(dig(spec, "work_budget", 0)), MONEY, INPUT_FILL)
    r = _kv(ws, r, "Loan amount", float(dig(spec, "loan.amount", 0)), MONEY, INPUT_FILL,
            "The one input that sets the deal")
    r = _kv(ws, r, "Interest rate", float(dig(spec, "loan.rate")), PCT2, INPUT_FILL)
    r = _kv(ws, r, "Points", float(dig(spec, "loan.points")), NUM, INPUT_FILL)
    r = _kv(ws, r, "Term (months)", int(dig(spec, "loan.term_months")), NUM, INPUT_FILL)
    r = _kv(ws, r, "Minimum interest (months)", int(dig(spec, "loan.min_interest_months")),
            NUM, INPUT_FILL)
    r = _kv(ws, r, "Draws", int(dig(spec, "loan.draws")), NUM, INPUT_FILL)
    r += 1
    r = _band(ws, r, "WHAT THAT COSTS")
    r = _kv(ws, r, "Origination points", "=Loan*Points/100", MONEY)
    r = _kv(ws, r, "Interest, full term", "=Loan*Rate*TermMonths/12", MONEY)
    r = _kv(ws, r, "Minimum interest guarantee", "=Loan*Rate*MinIntMonths/12", MONEY,
            note="Earned even on an early payoff")
    r = _kv(ws, r, "Payoff at maturity", "=Loan+Loan*Rate*TermMonths/12", MONEY,
            note="Principal + interest; points are paid at closing")
    r = _kv(ws, r, "Lender total return", "=Loan*Points/100+Loan*Rate*TermMonths/12", MONEY)
    r = _kv(ws, r, "Annualised yield", "=Rate+(Points/100)*(12/TermMonths)", PCT2)
    r += 1
    r = _band(ws, r, "ADVANCE AND DRAWS")
    # DERIVED. Never a typed literal -- see module docstring and the test.
    r = _kv(ws, r, "Day one advance at closing", "=Loan-RehabTotal", MONEY,
            note="DERIVED = Loan - Renovation. Never typed.")
    r = _kv(ws, r, "Renovation holdback", "=Loan-(Loan-RehabTotal)", MONEY,
            note="Ties to the repair budget by construction")
    r = _kv(ws, r, "Per draw", "=IF(Draws>0,(Loan-(Loan-RehabTotal))/Draws,0)", MONEY)
    r += 1
    r = _band(ws, r, "SECURITY")
    lien = dig(spec, "loan.lien", "First lien, mortgage recorded with the County Clerk")
    r = _kv(ws, r, "Lien position", lien, fill=CALC_FILL)
    r = _kv(ws, r, "Payment", dig(spec, "loan.payment",
            "No monthly payments. Principal and accrued interest due at payoff."), fill=CALC_FILL)
    _para(ws, r + 1,
          "New Jersey is a lien-theory state: the security instrument is a MORTGAGE recorded "
          "with the County Clerk, not a deed of trust. The note, mortgage and any guaranty are "
          "drawn by New Jersey counsel — this workbook prices the loan, it does not paper it.",
          color="C00000")
    return ws


def sheet_resale(wb, spec):
    ws = wb.create_sheet("Resale Value")
    _widths(ws, [34, 18, 46, 14])
    r = _title(ws, "Resale Value", "What it sells for, and what the lender is covered by.")
    r = _band(ws, r, "VALUE")
    r = _kv(ws, r, "After-repair value (point)", float(dig(spec, "resale.point", 0)), MONEY, INPUT_FILL)
    r = _kv(ws, r, "Band floor (stress case)", float(dig(spec, "resale.lo", 0) or 0), MONEY, INPUT_FILL)
    r = _kv(ws, r, "Band ceiling", float(dig(spec, "resale.hi", 0) or 0), MONEY, INPUT_FILL)
    r = _kv(ws, r, "As-is value today", float(dig(spec, "as_is.point", 0) or 0), MONEY, INPUT_FILL,
            "Covers the day-one advance before a nail is swung")
    r += 1
    r = _band(ws, r, "SELLING COSTS (live)")
    r = _kv(ws, r, "Agent commission", "=ARV*SellCommPct", MONEY)
    r = _kv(ws, r, "NJ realty transfer fee", "=" + nj_transfer_fee_formula("ARV"), MONEY,
            note="Live bracket schedule — moves with the resale price")
    r = _kv(ws, r, "Net sale proceeds", "=ARV-ARV*SellCommPct-(" +
            nj_transfer_fee_formula("ARV") + ")", MONEY)
    r = _kv(ws, r, "Net proceeds at band floor",
            "=ARVLow-ARVLow*SellCommPct-(" + nj_transfer_fee_formula("ARVLow") + ")", MONEY)
    r += 1
    r = _band(ws, r, "LENDER COVER")
    r = _kv(ws, r, "Loan to ARV", "=Loan/ARV", PCT1)
    r = _kv(ws, r, "Loan to ARV at band floor", "=Loan/ARVLow", PCT1)
    r = _kv(ws, r, "Equity cushion", "=ARV-(Loan+Loan*Rate*TermMonths/12)", MONEY)
    r = _kv(ws, r, "Coverage",
            "=(ARV-ARV*SellCommPct-(" + nj_transfer_fee_formula("ARV") +
            "))/(Loan+Loan*Rate*TermMonths/12)", MULT)
    r = _kv(ws, r, "Coverage at band floor",
            "=(ARVLow-ARVLow*SellCommPct-(" + nj_transfer_fee_formula("ARVLow") +
            "))/(Loan+Loan*Rate*TermMonths/12)", MULT,
            note="Below 1.00x the lender is short before the borrower sees a dollar")
    r = _kv(ws, r, "Day-one advance covered by as-is", "=IF(Loan-RehabTotal>0,AsIs/(Loan-RehabTotal),0)",
            MULT, note="Cover on the money advanced before any work")
    return ws


def sheet_investment(wb, spec):
    ws = wb.create_sheet("Your Investment")
    _widths(ws, [34, 18, 46, 14])
    r = _title(ws, "Your Investment", "Full cost of the project and where our own cash sits.")
    r = _band(ws, r, "CARRY AND CLOSING (inputs)")
    r = _kv(ws, r, "Property taxes, annual", float(dig(spec, "holding.taxes_annual")), MONEY, INPUT_FILL,
            "NJ taxes are the heaviest carry line — use the actual bill")
    r = _kv(ws, r, "Insurance, annual", float(dig(spec, "holding.insurance_annual")), MONEY, INPUT_FILL)
    r = _kv(ws, r, "Utilities, monthly", float(dig(spec, "holding.utilities_monthly")), MONEY, INPUT_FILL)
    r = _kv(ws, r, "Buy-side attorney", float(dig(spec, "buying_costs.attorney")), MONEY, INPUT_FILL)
    r = _kv(ws, r, "Buy-side title %", float(dig(spec, "buying_costs.title_pct")), PCT2, INPUT_FILL)
    r = _kv(ws, r, "Selling commission %", float(dig(spec, "selling_costs.realtor_pct")), PCT2, INPUT_FILL)
    r += 1
    r = _band(ws, r, "PROJECT COST")
    r = _kv(ws, r, "Purchase price", "=Purchase", MONEY)
    r = _kv(ws, r, "Renovation budget", "=RehabTotal", MONEY)
    r = _kv(ws, r, "Buy-side closing", "=BuyAttorney+Purchase*BuyTitlePct", MONEY)
    r = _kv(ws, r, "Holding, monthly", "=TaxesAnnual/12+InsAnnual/12+UtilMonthly", MONEY)
    r = _kv(ws, r, "Holding, full term", "=(TaxesAnnual/12+InsAnnual/12+UtilMonthly)*TermMonths", MONEY)
    r = _kv(ws, r, "Financing (points + interest)",
            "=Loan*Points/100+Loan*Rate*TermMonths/12", MONEY)
    r = _kv(ws, r, "TOTAL PROJECT COST",
            "=Purchase+RehabTotal+BuyAttorney+Purchase*BuyTitlePct"
            "+(TaxesAnnual/12+InsAnnual/12+UtilMonthly)*TermMonths"
            "+Loan*Points/100+Loan*Rate*TermMonths/12", MONEY)
    r += 1
    r = _band(ws, r, "OUR SKIN IN THE GAME")
    r = _kv(ws, r, "Cash we put in",
            "=MAX(0,Purchase+RehabTotal+BuyAttorney+Purchase*BuyTitlePct"
            "+(TaxesAnnual/12+InsAnnual/12+UtilMonthly)*TermMonths-Loan)", MONEY,
            note="Our capital ahead of the lender's")
    r = _kv(ws, r, "Projected net profit",
            "=ARV-ARV*SellCommPct-(" + nj_transfer_fee_formula("ARV") + ")"
            "-(Purchase+RehabTotal+BuyAttorney+Purchase*BuyTitlePct"
            "+(TaxesAnnual/12+InsAnnual/12+UtilMonthly)*TermMonths"
            "+Loan*Points/100+Loan*Rate*TermMonths/12)", MONEY)
    _para(ws, r + 1,
          "Financed profit reads as cash-on-cash, not as a margin on value. The lender is repaid "
          "in full before we see a dollar.")
    return ws


def sheet_overview(wb, spec):
    ws = wb.create_sheet("Deal Overview")
    _widths(ws, [34, 18, 46, 14])
    prop = spec.get("property", {})
    r = _title(ws, "Private Lender Package",
               f"{prop.get('full_address', '[ADDRESS]')} · prepared "
               f"{spec.get('date', '')}")
    if spec.get("terms_assumed", True):
        r = _para(ws, r, "TERMS ARE ASSUMED PLACEHOLDERS. Rate, points, term and loan amount "
                         "below are modelling assumptions. Replace them with the signed term "
                         "sheet and re-issue before this goes to a lender.", bold=True,
                  color="C00000") + 1
    r = _band(ws, r, "THE ASK")
    r = _kv(ws, r, "Loan requested", "=Loan", MONEY)
    r = _kv(ws, r, "Purchase price", "=Purchase", MONEY)
    r = _kv(ws, r, "Renovation budget", "=RehabTotal", MONEY)
    r = _kv(ws, r, "After-repair value", "=ARV", MONEY)
    r = _kv(ws, r, "Loan to ARV", "=Loan/ARV", PCT1)
    r = _kv(ws, r, "Term", '=TEXT(TermMonths,"0")&" months at "&TEXT(Rate,"0.00%")&'
                           '" plus "&TEXT(Points,"0")&" points"', fill=CALC_FILL)
    r += 1
    r = _band(ws, r, "WHAT PROTECTS YOU")
    # Live prose: these sentences recalculate with the inputs.
    r = _kv(ws, r, "First position",
            '="First lien mortgage recorded with the "&"'
            + str(prop.get("county", "[COUNTY]")).replace('"', "'")
            + ' County Clerk."', fill=CALC_FILL)
    r = _kv(ws, r, "Equity cushion",
            '="You are lending "&TEXT(Loan/ARV,"0.0%")&" of a "&TEXT(ARV,"$#,##0")&'
            '" resale, leaving "&TEXT(ARV-(Loan+Loan*Rate*TermMonths/12),"$#,##0")&'
            '" of cushion above the payoff."', fill=CALC_FILL)
    r = _kv(ws, r, "Stress case",
            '="If it only makes the band floor of "&TEXT(ARVLow,"$#,##0")&", coverage is still "&'
            'TEXT((ARVLow-ARVLow*SellCommPct-(' + nj_transfer_fee_formula("ARVLow") +
            '))/(Loan+Loan*Rate*TermMonths/12),"0.00")&"x."', fill=CALC_FILL)
    r = _kv(ws, r, "Our capital first",
            '="We are in for "&TEXT(MAX(0,Purchase+RehabTotal+BuyAttorney+Purchase*BuyTitlePct'
            '+(TaxesAnnual/12+InsAnnual/12+UtilMonthly)*TermMonths-Loan),"$#,##0")&'
            '" of our own money, which is lost before yours is touched."', fill=CALC_FILL)
    r = _kv(ws, r, "Day-one exposure",
            '="Only "&TEXT(Loan-RehabTotal,"$#,##0")&" funds at closing against an as-is value of "&'
            'TEXT(AsIs,"$#,##0")&". The rest releases against completed work."', fill=CALC_FILL)
    r += 1
    r = _band(ws, r, "YOUR RETURN")
    r = _kv(ws, r, "Points + interest", "=Loan*Points/100+Loan*Rate*TermMonths/12", MONEY)
    r = _kv(ws, r, "Annualised yield", "=Rate+(Points/100)*(12/TermMonths)", PCT2)
    r = _kv(ws, r, "Minimum interest", "=Loan*Rate*MinIntMonths/12", MONEY,
            note="Yours even if we pay off early")
    return ws


def sheet_repairs(wb, spec):
    ws = wb.create_sheet("Repair Costs")
    _widths(ws, [40, 16, 16, 44])
    r = _title(ws, "Repair Costs", "The scope the renovation holdback funds.")
    lines = spec.get("repair_lines") or []
    r = _band(ws, r, "SCOPE")
    hdr = ("Item", "Cost", "", "Notes")
    for i, h in enumerate(hdr, start=1):
        c = ws.cell(row=r, column=i, value=h)
        c.font = BOLD
    r += 1
    first = r
    for ln in lines:
        ws.cell(row=r, column=1, value=ln.get("item", "")).font = LBL
        c = ws.cell(row=r, column=2, value=float(ln.get("cost", 0)))
        c.number_format, c.fill, c.border = MONEY, INPUT_FILL, BOX
        ws.cell(row=r, column=4, value=ln.get("note", "")).font = Font(size=9, color="5A6878")
        r += 1
    if lines:
        ws.cell(row=r, column=1, value="TOTAL FROM SCOPE").font = BOLD
        t = ws.cell(row=r, column=2, value=f"=SUM(B{first}:B{r-1})")
        t.number_format, t.font, t.fill = MONEY, BOLD, CALC_FILL
        r += 1
        chk = ws.cell(row=r, column=1,
                      value="Check: scope total vs the renovation budget on the Term Sheet")
        chk.font = Font(size=9, italic=True)
        d = ws.cell(row=r, column=2, value=f"=SUM(B{first}:B{r-2})-RehabTotal")
        d.number_format, d.fill = MONEY, CALC_FILL
        ws.cell(row=r, column=4,
                value="Must be $0. Anything else means the scope and the holdback disagree."
                ).font = WARN_FONT
        r += 2
    else:
        r = _para(ws, r, "No line-item scope supplied. The renovation budget on the Term Sheet "
                         "is the only figure backing the holdback — attach the estimator output "
                         "before this goes out.", color="C00000") + 1
    r = _kv(ws, r, "Renovation budget (Term Sheet)", "=RehabTotal", MONEY)
    r = _kv(ws, r, "Renovation holdback", "=Loan-(Loan-RehabTotal)", MONEY)
    r = _kv(ws, r, "Per draw", "=IF(Draws>0,(Loan-(Loan-RehabTotal))/Draws,0)", MONEY)
    return ws


def sheet_comps(wb, spec):
    ws = wb.create_sheet("Comps")
    _widths(ws, [34, 14, 12, 12, 14, 30])
    r = _title(ws, "Comps", "What the resale number is built on.")
    for i, h in enumerate(("Address", "Sold price", "Sqft", "Beds/Baths", "Sold date", "Note"),
                          start=1):
        ws.cell(row=r, column=i, value=h).font = BOLD
    r += 1
    comps = spec.get("comps") or []
    for cp in comps:
        ws.cell(row=r, column=1, value=cp.get("address", ""))
        c = ws.cell(row=r, column=2, value=cp.get("sold_price"))
        c.number_format = MONEY
        ws.cell(row=r, column=3, value=cp.get("sqft"))
        ws.cell(row=r, column=4, value=cp.get("beds_baths", ""))
        ws.cell(row=r, column=5, value=cp.get("sold_date", ""))
        ws.cell(row=r, column=6, value=cp.get("note", "")).font = Font(size=9, color="5A6878")
        r += 1
    if not comps:
        _para(ws, r, "No comps supplied. A lender package without the comp set behind the "
                     "resale number is asking for trust instead of evidence — attach them.",
              color="C00000")
    return ws


def sheet_risk(wb, spec):
    ws = wb.create_sheet("Backups and Risk")
    _widths(ws, [96, 14, 14, 14])
    r = _title(ws, "Backups and Risk", "What happens if the plan does not.")
    r = _band(ws, r, "IF THE RESALE DOES NOT LAND")
    for line in (
        '="Band floor: at "&TEXT(ARVLow,"$#,##0")&" the net still covers the payoff '
        'at "&TEXT((ARVLow-ARVLow*SellCommPct-(' + nj_transfer_fee_formula("ARVLow") +
        '))/(Loan+Loan*Rate*TermMonths/12),"0.00")&"x."',
        '="Rent instead of sell: the loan can be refinanced onto a term product; '
        'the payoff is "&TEXT(Loan+Loan*Rate*TermMonths/12,"$#,##0")&"."',
        '="Wholesale the contract: if the numbers move against us before we start, '
        'we assign rather than draw."',
    ):
        c = ws.cell(row=r, column=1, value=line)
        c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[r].height = 28
        r += 1
    r += 1
    r = _band(ws, r, "RISKS WE ARE NAMING")
    for b in (spec.get("plan", {}).get("risk_bullets") or [
            "Renovation overrun — the budget carries the estimator's calibrated overrun factor, "
            "not a flat contingency.",
            "Days on market — a comp set with median DOM over 60 is the single cleanest loss "
            "predictor we track.",
            "Permit or inspection delay in the municipality.",
            "NJ property taxes are the heaviest carry line; the holding figure uses the actual bill."]):
        r = _para(ws, r, "• " + b)
    return ws


def sheet_next(wb, spec):
    ws = wb.create_sheet("Next Steps")
    _widths(ws, [96, 14, 14, 14])
    r = _title(ws, "Next Steps", "What happens after you say yes.")
    steps = spec.get("plan", {}).get("next_steps") or [
        "You confirm the loan amount and terms. That is the one input that sets the deal.",
        "New Jersey counsel draws the note, the mortgage and any guaranty. We do not draft them.",
        "Closing is scheduled with " + str(spec.get("closing_agent", "[CLOSING ATTORNEY]")) + ".",
        "You wire the day-one advance; the mortgage records in first position with the County Clerk.",
        "Renovation draws release against completed work, inspected before each release.",
        "The property lists, sells, and you are paid principal plus interest at closing.",
    ]
    for i, s in enumerate(steps, start=1):
        r = _para(ws, r, f"{i}. {s}")
    r += 1
    _para(ws, r, "This workbook prices the loan. It is not a commitment, an offer of securities, "
                 "or legal advice, and it does not paper the loan — the note, mortgage and "
                 "guaranty come from New Jersey counsel.", bold=True, color="C00000")
    return ws


# ══ read-back protection ══════════════════════════════════════════════

def read_back_inputs(path: str) -> dict:
    """Read the input cells out of an existing package.

    Reviewers edit inputs in Excel and do not always mention every change.
    Rebuilding blind discards them silently, so the builder reads them back
    first and reports anything that moved.
    """
    wb = load_workbook(path, data_only=False)
    out = {}
    for name, sheet, cell, _path, _fmt in INPUTS:
        if sheet in wb.sheetnames:
            out[name] = wb[sheet][cell].value
    wb.close()
    return out


def diff_against_spec(existing: dict, spec: dict) -> list:
    """Inputs in the workbook that disagree with the spec, as readable lines."""
    diffs = []
    for name, _sheet, _cell, path, _fmt in INPUTS:
        if name not in existing:
            continue
        was, now = existing[name], dig(spec, path, 0)
        try:
            if was is None or abs(float(was) - float(now)) < 0.005:
                continue
            diffs.append(f"{name}: workbook has {float(was):,.4g}, spec has {float(now):,.4g}")
        except (TypeError, ValueError):
            if str(was) != str(now):
                diffs.append(f"{name}: workbook has {was!r}, spec has {now!r}")
    return diffs


# ══ build ═════════════════════════════════════════════════════════════

BUILDERS = (sheet_overview, sheet_terms, sheet_investment, sheet_repairs,
            sheet_resale, sheet_comps, sheet_risk, sheet_next)


def build_workbook(spec: dict, out_path: str, force: bool = False) -> str:
    model = compute(spec)          # validates, and raises before anything is written

    out = Path(out_path)
    if out.exists() and not force:
        try:
            diffs = diff_against_spec(read_back_inputs(str(out)), spec)
        except Exception as exc:                      # unreadable / not ours
            logger.warning("Could not read back %s (%s); treating as new.", out, exc)
            diffs = []
        if diffs:
            raise RuntimeError(
                "Refusing to overwrite: the existing workbook has edited inputs that the "
                "spec does not carry. Someone reviewed this in Excel.\n  "
                + "\n  ".join(diffs)
                + "\nFold the changes into the spec, or pass --force to discard them.")

    wb = Workbook()
    wb.remove(wb.active)
    for fn in BUILDERS:
        fn(wb, spec)
    for name, sheet, cell, _path, _fmt in INPUTS:
        _defname(wb, name, sheet, cell)
    wb._sheets.sort(key=lambda s: SHEET_ORDER.index(s.title)
                    if s.title in SHEET_ORDER else 99)
    for ws in wb.worksheets:
        ws.sheet_view.showGridLines = False
        ws.freeze_panes = "A4"
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out))
    logger.info("Lender package: %s", out)
    logger.info("  loan $%s | LTARV %.1f%% | coverage %.2fx (%.2fx at floor) | yield %.2f%%",
                f"{model['loan']:,}", model["ltarv"] * 100, model["coverage"],
                model["coverage_stress"], model["ann_yield"] * 100)
    return str(out)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Build the private lender package workbook")
    ap.add_argument("--spec", required=True, help="Deal spec JSON")
    ap.add_argument("--out", required=True, help="Output .xlsx path")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite even if the existing workbook has edited inputs")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    spec = json.loads(Path(a.spec).read_text(encoding="utf-8"))
    try:
        build_workbook(spec, a.out, force=a.force)
    except (ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
