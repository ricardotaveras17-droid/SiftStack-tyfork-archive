"""Post-Walkthrough Package — the one workbook you build the hour after a walk.

Nine sheets. It does not re-derive anything: it COMPOSES the engines that were
built and tested separately, so a number shown here is the same number the
underwriting used.

    comp_analyzer      base / upside ARV via the bedroom band
    rehab_estimator    NJ-calibrated scope and cost
    exit_strategy      five gated lanes off the conservative ARV
    lender_analysis    exposure, coverage, payoff, borrower position

THE WALKTHROUGH JSON IS THE HUMAN LAYER AND IT WINS. Anything set in it
overrides the live record, because the person who stood in the house knows
things the data does not. `reconfig_verified` is the gate that decides whether
the bedroom-band upside is credited at all.

EXACT NUMBERS, NOT RANGES. A band is not an answer you can take to a seller or
a buyer. The lo/hi still drives the math, and it surfaces as a single
"If it moves" sensitivity line rather than smearing every cell into a range.

Usage:
  python src/post_walkthrough_nj.py --walk deals/123_main_walk.json \\
      --out output/123_main_post_walk.xlsx
"""

import argparse
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

logger = logging.getLogger(__name__)

MONEY = '"$"#,##0'
PCT1 = "0.0%"
MULT = '0.00"x"'
NAVY, GOLD, GREEN, RED = "14243E", "C9A44C", "1B7A3C", "C00000"
BAND_FILL = PatternFill("solid", fgColor=NAVY)
HUMAN_FILL = PatternFill("solid", fgColor="FFF4D6")   # came from the walk
CALC_FILL = PatternFill("solid", fgColor="F2F2F2")
H1 = Font(bold=True, size=15, color=NAVY)
H2 = Font(bold=True, size=11, color="FFFFFF")
LBL = Font(size=10)
BOLD = Font(bold=True, size=10)
WARN = Font(bold=True, size=10, color=RED)
THIN = Side(style="thin", color="D0D0D0")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

SHEET_ORDER = ["Overview", "Exit Strats", "Comps", "Active-Pending",
               "Repair Logic", "Repair Numbers", "Buyer Targets",
               "Outreach", "Lender Analysis"]


@dataclass
class WalkPack:
    """Everything the nine sheets render from, assembled once."""
    walk: dict = field(default_factory=dict)
    subject: object = None
    arv: object = None
    rehab_full: object = None
    rehab_wholetail: object = None
    exits: object = None
    lender: object = None
    lender_terms: object = None
    comps: list = field(default_factory=list)
    actives: list = field(default_factory=list)
    buyers: list = field(default_factory=list)
    notes: list = field(default_factory=list)


# ── sheet helpers ─────────────────────────────────────────────────────

def _title(ws, text, sub=""):
    ws["A1"] = text
    ws["A1"].font = H1
    if sub:
        ws["A2"] = sub
        ws["A2"].font = Font(size=9, italic=True, color="5A6878")
    return 4


def _band(ws, row, text, span=6):
    c = ws.cell(row=row, column=1, value=text)
    c.font = H2
    for col in range(1, span + 1):
        ws.cell(row=row, column=col).fill = BAND_FILL
    return row + 1


def _kv(ws, row, label, value, fmt=None, human=False, note=""):
    ws.cell(row=row, column=1, value=label).font = LBL
    c = ws.cell(row=row, column=2, value=value)
    c.fill = HUMAN_FILL if human else CALC_FILL
    c.border = BOX
    if fmt:
        c.number_format = fmt
    if note:
        n = ws.cell(row=row, column=3, value=note)
        n.font = Font(size=9, italic=True, color="5A6878")
    return row + 1


def _para(ws, row, text, font=None, span=6):
    c = ws.cell(row=row, column=1, value=text)
    c.font = font or Font(size=10)
    c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=span)
    ws.row_dimensions[row].height = max(15, 13 * (len(text) // 110 + 1))
    return row + 1


def _hdr(ws, row, headers):
    for i, h in enumerate(headers, start=1):
        c = ws.cell(row=row, column=i, value=h)
        c.font = BOLD
        c.border = BOX
    return row + 1


def _widths(ws, widths):
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _money(v) -> str:
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return str(v or "")


# ── assembly ──────────────────────────────────────────────────────────

def build_pack(walk: dict, subject=None, comps=None, actives=None,
               buyers=None) -> WalkPack:
    """Run every engine once, in the order they depend on each other.

    Nothing is recomputed inside a sheet. If a sheet wants a number it comes
    from here, so the Overview and the Exit Strats sheet cannot disagree.
    """
    from comp_analyzer import SubjectProperty, calculate_arv
    from exit_strategy import analyse as analyse_exits
    from lender_analysis import LoanTerms, analyse as analyse_lender
    from rehab_estimator import estimate_rehab, estimate_wholetail

    prop = walk.get("property", {})
    if subject is None:
        subject = SubjectProperty(
            address=prop.get("address", ""), city=prop.get("city", ""),
            state=prop.get("state", "NJ"), zip_code=prop.get("zip", ""),
            sqft=int(prop.get("sqft") or 0), bedrooms=int(prop.get("beds") or 0),
            bathrooms=float(prop.get("baths") or 0),
            year_built=int(prop.get("year_built") or 0))

    notes = []

    # The walk is the human layer and it overrides the record.
    verified = bool(walk.get("reconfig_verified"))
    if not verified:
        notes.append("Reconfigure upside is NOT credited: the walk did not verify "
                     "the layout converts. It shows as the buyer's room, not ours.")

    arv = calculate_arv(subject, list(comps or []), walkthrough_verified=verified)
    if not arv.arv_base:
        notes.append("NO ARV — no usable comps were supplied. Every value on this "
                     "package is unsupported until the comp pull is attached.")

    tier = int(walk.get("finish_tier") or 2)
    scope = walk.get("scope") or "full"
    rehab_full = estimate_rehab(
        address=subject.address, sqft=subject.sqft, bedrooms=subject.bedrooms,
        bathrooms=subject.bathrooms, year_built=subject.year_built,
        tier=tier, scope=scope, state=subject.state,
        county=prop.get("county", ""), city=subject.city)
    rehab_wt = estimate_wholetail(
        address=subject.address, sqft=subject.sqft, bedrooms=subject.bedrooms,
        bathrooms=subject.bathrooms, year_built=subject.year_built,
        state=subject.state, county=prop.get("county", ""), city=subject.city)
    if not rehab_full.region_verified:
        notes.append(f"Rehab cost basis for {rehab_full.region.title()} is UNVERIFIED "
                     f"({rehab_full.region_calibration}).")

    offer = float(walk.get("contract_price") or 0) or float(walk.get("target_offer") or 0)
    exits = None
    if offer and arv.arv_base:
        fin = walk.get("financing") or {}
        exits = analyse_exits(
            base_arv=arv.arv_base, quoted_rehab=rehab_full.grand_total, offer=offer,
            as_is=float(walk.get("as_is_value") or 0),
            scope=walk.get("exit_scope", "med"), state=subject.state,
            upside_arv=arv.arv_upside if arv.upside_credited else 0,
            walkthrough_verified=arv.upside_credited,
            monthly_rent=float(walk.get("monthly_rent") or 0),
            end_buyer_mao=float(walk.get("assignment_price") or 0) or 0,
            financed=bool(fin), address=subject.address)

    lender = lender_terms = None
    fin = walk.get("financing") or {}
    if fin and offer and not arv.arv_base:
        notes.append("Financing block present but no ARV could be derived, so the "
                     "lender view is not rendered. Pricing debt against a value we "
                     "do not have is how a package gets built on nothing.")
    if fin and offer and arv.arv_base:
        lender_terms = LoanTerms(
            rate=float(fin.get("rate", 0.12)), points=float(fin.get("points", 2)),
            term_months=int(fin.get("term_months", 9)), ltc=float(fin.get("ltc", 0.85)),
            draws=int(fin.get("draws", 4)), lender=fin.get("lender", ""),
            assumed=bool(fin.get("assumed", True)))
        lender = analyse_lender(offer, rehab_full.grand_total, arv.arv_base,
                                lender_terms, subject.state, arv.arv_low)
        notes.extend(lender.warnings)

    return WalkPack(walk=walk, subject=subject, arv=arv, rehab_full=rehab_full,
                    rehab_wholetail=rehab_wt, exits=exits, lender=lender,
                    lender_terms=lender_terms, comps=list(comps or []),
                    actives=list(actives or []), buyers=list(buyers or []),
                    notes=notes)


# ── 1. Overview ───────────────────────────────────────────────────────

def sheet_overview(wb, p: WalkPack):
    ws = wb.create_sheet("Overview")
    _widths(ws, [34, 18, 52, 14, 14, 14])
    w, s, a = p.walk, p.subject, p.arv
    r = _title(ws, "Post-Walkthrough Package",
               f"{s.address}, {s.city} {s.state} {s.zip_code} · walked "
               f"{w.get('walk_date', '?')}")
    for n in p.notes:
        r = _para(ws, r, "!! " + n, font=WARN)
    if p.notes:
        r += 1

    r = _band(ws, r, "THE HOUSE")
    r = _kv(ws, r, "Config", f"{s.bedrooms}bd / {s.bathrooms}ba · {s.sqft:,} sqft · "
                             f"built {s.year_built or '?'}")
    if w.get("layout"):
        r = _kv(ws, r, "Layout", w["layout"], human=True)
    if w.get("distress"):
        r = _kv(ws, r, "Distress", w["distress"], human=True)
    if w.get("unfinanceable"):
        r = _kv(ws, r, "Financeable", "NO — cash or hard money only", human=True,
                note="Cuts the retail buyer pool to cash")

    r += 1
    r = _band(ws, r, "VALUE — EXACT NUMBERS")
    r = _kv(ws, r, "Base ARV (underwrite this)", a.arv_base, MONEY,
            note=a.basis[:80] if a.basis else "")
    if a.arv_upside:
        r = _kv(ws, r, "Upside ARV (reconfigured)", a.arv_upside, MONEY,
                note="CREDITED — walk verified" if a.upside_credited
                     else "NOT credited — the buyer's room, not our underwrite")
    r = _kv(ws, r, "As-is value", float(w.get("as_is_value") or 0), MONEY, human=True)
    r = _kv(ws, r, "Full rehab", p.rehab_full.grand_total, MONEY,
            note=f"{p.rehab_full.region.title()} · tier {p.rehab_full.tier}")
    r = _kv(ws, r, "Wholetail rehab", p.rehab_wholetail.grand_total, MONEY)

    r += 1
    r = _band(ws, r, "IF IT MOVES")
    swing = a.arv_high - a.arv_low
    r = _para(ws, r, f"The comp band runs {_money(a.arv_low)} to {_money(a.arv_high)}, "
                     f"a {_money(swing)} swing. Every number above uses the base "
                     f"{_money(a.arv_base)}. At the floor the deal loses "
                     f"{_money(a.arv_base - a.arv_low)} of headroom; at the ceiling it "
                     f"gains {_money(a.arv_high - a.arv_base)}. Underwrite the floor, "
                     f"sell the ceiling.")
    return ws


# ── 2. Exit Strats ────────────────────────────────────────────────────

def sheet_exits(wb, p: WalkPack):
    ws = wb.create_sheet("Exit Strats")
    _widths(ws, [30, 16, 16, 16, 16, 44])
    r = _title(ws, "Exit Strategies", "Every lane scored off the BASE ARV. "
                                      "A lane that misses is named, not hidden.")
    if p.exits is None:
        _para(ws, r, "No offer price on the walk, so no lane can be scored. Set "
                     "contract_price or target_offer and re-render.", font=WARN)
        return ws

    r = _para(ws, r, p.exits.headline, font=BOLD)
    r += 1
    r = _hdr(ws, r, ("Lane", "Sale price", "Profit", "MOS", "$/month", "Verdict / why not"))
    for lane in p.exits.lanes:
        clears = lane.passed
        ws.cell(row=r, column=1, value=("CLEARS  " if clears else "out  ") + lane.name).font = (
            Font(bold=True, size=10, color=GREEN) if clears else Font(size=10))
        for col, (val, fmt) in enumerate(
                ((lane.sale_price or None, MONEY), (lane.actual_profit, MONEY),
                 (lane.mos if lane.hold_months else None, PCT1),
                 (lane.profit_per_month if lane.hold_months else None, MONEY)), start=2):
            if val is not None:
                c = ws.cell(row=r, column=col, value=val)
                c.number_format = fmt
        why = lane.verdict if clears else (lane.reasons[0] if lane.reasons else "—")
        ws.cell(row=r, column=6, value=why).font = Font(size=9, color="5A6878")
        r += 1

    r += 1
    r = _band(ws, r, "WHAT WAS RULED OUT, AND WHY")
    for lane in p.exits.ruled_out:
        for reason in (lane.reasons or ["did not clear"]):
            r = _para(ws, r, f"{lane.name}: {reason}")
    if p.exits.near_misses:
        r += 1
        r = _band(ws, r, "CLOSEST MISSES — shown for the gap, NOT recommendations")
        for lane in p.exits.near_misses:
            r = _para(ws, r, f"{lane.name}: profit {_money(lane.actual_profit)}, "
                             f"{lane.pct_over_mao:+.0%} vs MAO")
    r += 1
    _para(ws, r, "Novation is not an exit lane here. Seller-facing routes — cash, "
                 "managed listing, straight listing — live in the Seller Options "
                 "comparison. The buyer's exit and our hold are different conversations.")
    return ws


# ── 3. Comps ──────────────────────────────────────────────────────────

def sheet_comps(wb, p: WalkPack):
    ws = wb.create_sheet("Comps")
    _widths(ws, [32, 12, 10, 8, 12, 12, 12, 26])
    a = p.arv
    r = _title(ws, "Comps", "Scored inside the bedroom band. Higher-bed comps feed "
                            "the upside track only.")
    r = _kv(ws, r, "Base ARV", a.arv_base, MONEY)
    if a.arv_upside:
        r = _kv(ws, r, "Upside ARV", a.arv_upside, MONEY,
                note="credited" if a.upside_credited else "NOT credited")
    r = _kv(ws, r, "Band median", a.band_median, MONEY,
            note="the clamp — an oversized subject cannot pass it" if a.band_clamped else "")
    r = _kv(ws, r, "Same-bed comps", a.same_bed_count)
    r = _kv(ws, r, "Confidence", f"{a.confidence} — {a.confidence_reason}")
    if a.band_flag:
        r = _para(ws, r, "!! " + a.band_flag, font=WARN)
    r += 1
    r = _hdr(ws, r, ("Address", "Sold", "Sqft", "Bd", "$/sqft", "Adjusted",
                     "Distance", "Bucket / condition"))
    for c in p.comps[:20]:
        vals = [c.address, c.sold_price, c.sqft, c.bedrooms, c.ppsf,
                c.adjusted_price or None, c.distance_miles,
                f"{c.bucket or '-'} / {c.condition or '-'}"]
        for col, v in enumerate(vals, start=1):
            cell = ws.cell(row=r, column=col, value=v)
            if col in (2, 6):
                cell.number_format = MONEY
            if col == 5:
                cell.number_format = '"$"#,##0.00'
        r += 1
    if not p.comps:
        _para(ws, r, "No comps supplied. The ARV above cannot be defended without "
                     "them — attach the comp pull before this goes to anyone.", font=WARN)
    return ws


# ── 4. Active-Pending ─────────────────────────────────────────────────

def sheet_active(wb, p: WalkPack):
    ws = wb.create_sheet("Active-Pending")
    _widths(ws, [32, 12, 10, 8, 12, 12, 30])
    r = _title(ws, "Active and Pending",
               "What we are selling against. Sold comps say what happened; "
               "actives say what we have to beat.")
    if not p.actives:
        _para(ws, r, "No active listings supplied. Sold comps alone tell you where the "
                     "market WAS. Without the actives you are pricing an exit into "
                     "competition you have not looked at.", font=WARN)
        return ws
    r = _hdr(ws, r, ("Address", "List price", "Sqft", "Bd", "$/sqft", "DOM", "Note"))
    for l in p.actives[:25]:
        for col, v in enumerate([getattr(l, "address", ""), getattr(l, "price", 0),
                                 getattr(l, "sqft", 0), getattr(l, "beds", 0),
                                 getattr(l, "ppsf", 0), getattr(l, "days_on_zillow", 0),
                                 getattr(l, "home_status", "")], start=1):
            cell = ws.cell(row=r, column=col, value=v)
            if col == 2:
                cell.number_format = MONEY
        r += 1
    return ws


# ── 5. Repair Logic ───────────────────────────────────────────────────

def sheet_repair_logic(wb, p: WalkPack):
    ws = wb.create_sheet("Repair Logic")
    _widths(ws, [30, 16, 60, 14, 14, 14])
    w = p.walk
    r = _title(ws, "Repair Logic", "WHY the scope is what it is. The numbers live "
                                   "on the next sheet; this is the reasoning.")
    r = _band(ws, r, "WHAT THE WALK FOUND")
    for item in (w.get("work_done") or []):
        r = _para(ws, r, f"DONE — {item}")
    for item in (w.get("still_open") or []):
        r = _para(ws, r, f"OPEN — {item}")
    for flag in (w.get("flags") or []):
        r = _para(ws, r, f"FLAG — {flag}", font=WARN)
    if not any((w.get("work_done"), w.get("still_open"), w.get("flags"))):
        r = _para(ws, r, "The walk recorded no findings. That is a gap, not a clean "
                         "house — fill in work_done / still_open / flags.", font=WARN)
    r += 1
    r = _band(ws, r, "SCOPE BASIS")
    r = _kv(ws, r, "Finish tier", f"Tier {p.rehab_full.tier}")
    r = _kv(ws, r, "Scope", p.rehab_full.scope)
    r = _kv(ws, r, "Cost region", p.rehab_full.region.title(),
            note=("BID-CALIBRATED" if p.rehab_full.region_verified
                  else "UNVERIFIED — " + p.rehab_full.region_calibration))
    r = _kv(ws, r, "Labor multiplier", p.rehab_full.labor_multiplier, MULT)
    r = _kv(ws, r, "Materials multiplier", p.rehab_full.materials_multiplier, MULT)
    r = _kv(ws, r, "Materials source", getattr(p.rehab_full, "materials_source", "engine"))
    r += 1
    if w.get("target_config"):
        tc = w["target_config"]
        r = _band(ws, r, "RECONFIGURE")
        r = _kv(ws, r, "Target config", f"{tc.get('beds')}bd / {tc.get('baths')}ba", human=True)
        r = _kv(ws, r, "Verified on the walk", "YES" if w.get("reconfig_verified") else "NO",
                human=True,
                note="Plumbing runs, window egress, framing, ceiling heights"
                     if w.get("reconfig_verified")
                     else "Until verified the upside is the buyer's, not ours")
    return ws


# ── 6. Repair Numbers ─────────────────────────────────────────────────

def sheet_repair_numbers(wb, p: WalkPack):
    ws = wb.create_sheet("Repair Numbers")
    _widths(ws, [30, 16, 16, 16, 14, 30])
    r = _title(ws, "Repair Numbers", "Every dollar traces back to a line on Repair Logic.")
    r = _hdr(ws, r, ("Category", "Materials", "Labor", "Total", "Weeks", ""))
    for room in p.rehab_full.rooms:
        for col, v in enumerate([room.category, room.materials, room.labor,
                                 room.total, room.weeks], start=1):
            c = ws.cell(row=r, column=col, value=v)
            if col in (2, 3, 4):
                c.number_format = MONEY
        r += 1
    r += 1
    for label, val in (("Subtotal materials", p.rehab_full.total_materials),
                       ("Subtotal labor", p.rehab_full.total_labor),
                       ("Permits", p.rehab_full.permits_cost),
                       ("Contingency", p.rehab_full.contingency_cost),
                       ("FULL REHAB", p.rehab_full.grand_total),
                       ("Wholetail alternative", p.rehab_wholetail.grand_total)):
        ws.cell(row=r, column=1, value=label).font = BOLD
        c = ws.cell(row=r, column=4, value=val)
        c.number_format, c.font = MONEY, BOLD
        r += 1
    r += 1
    _para(ws, r, f"Timeline {p.rehab_full.total_weeks:.0f} weeks. Materials and labor are "
                 f"separate lines on purpose: the regional multiplier applies to LABOR, "
                 f"and where a locked material list exists its prices are already local "
                 f"and are never multiplied.")
    return ws


# ── 7. Buyer Targets ──────────────────────────────────────────────────

def sheet_buyers(wb, p: WalkPack):
    ws = wb.create_sheet("Buyer Targets")
    _widths(ws, [34, 22, 16, 14, 16, 34])
    r = _title(ws, "Buyer Targets", "Deed-verified buyers first. Best deal-fit at the top.")
    if not p.buyers:
        _para(ws, r, "No buyer list supplied. Run the buyer sweep and attach it — a "
                     "dispo sheet with no named buyers is a wish, not a plan.", font=WARN)
        return ws
    r = _hdr(ws, r, ("Buyer", "Entity", "Last buy", "Deals", "Typical price", "Fit"))
    for b in p.buyers[:40]:
        for col, k in enumerate(("name", "entity", "last_purchase", "deal_count",
                                 "typical_price", "fit"), start=1):
            c = ws.cell(row=r, column=col, value=b.get(k, ""))
            if k == "typical_price" and b.get(k):
                c.number_format = MONEY
        r += 1
    return ws


# ── 8. Outreach ───────────────────────────────────────────────────────

def sheet_outreach(wb, p: WalkPack):
    ws = wb.create_sheet("Outreach")
    _widths(ws, [100, 14, 14, 14, 14, 14])
    w, a = p.walk, p.arv
    r = _title(ws, "Outreach", "What we actually say. Name the BUYER's exit, never our hold.")
    best = None
    if p.exits and p.exits.suggested:
        best = p.exits.suggested[0]
    r = _band(ws, r, "THE PITCH")
    if w.get("pitch"):
        r = _para(ws, r, w["pitch"])
    else:
        cfg = f"{p.subject.bedrooms}/{p.subject.bathrooms:.0f}"
        line = (f"{p.subject.address}, {p.subject.city}. {cfg}, "
                f"{p.subject.sqft:,} sqft, built {p.subject.year_built or '?'}. ")
        if best:
            line += (f"Best lane is {best.name.lower()} at {_money(best.sale_price)} "
                     f"resale on {_money(p.rehab_full.grand_total)} of work. ")
        line += (f"ARV {_money(a.arv_base)} on {a.same_bed_count} same-bed comps.")
        if a.arv_upside and not a.upside_credited:
            line += (f" Reconfigure to {w.get('target_config', {}).get('beds', '?')}bd "
                     f"puts it in the {_money(a.arv_upside)} band — that upside is "
                     f"the buyer's to take, we have not priced it in.")
        r = _para(ws, r, line)
    r += 1
    r = _band(ws, r, "NUMBERS FOR THE BUYER")
    r = _kv(ws, r, "Assignment price", float(w.get("assignment_price") or 0), MONEY, human=True)
    r = _kv(ws, r, "Their ARV", a.arv_base, MONEY)
    r = _kv(ws, r, "Their rehab", p.rehab_full.grand_total, MONEY)
    if w.get("monthly_rent"):
        r = _kv(ws, r, "Market rent", float(w["monthly_rent"]), MONEY, human=True)
    r += 1
    for g in (w.get("gates") or []):
        r = _para(ws, r, f"GATE — {g}", font=WARN)
    return ws


# ── 9. Lender Analysis ────────────────────────────────────────────────

def sheet_lender(wb, p: WalkPack):
    ws = wb.create_sheet("Lender Analysis")
    _widths(ws, [34, 18, 52, 14, 14, 14])
    r = _title(ws, "Lender Analysis", "Renders only when the walk carries a financing block.")
    if p.lender is None:
        _para(ws, r, "No financing block on the walk, so the deal is modelled all-cash. "
                     "Add a financing block to price the debt.", font=Font(size=10, italic=True))
        return ws
    from lender_analysis import render
    for line in render(p.lender, p.lender_terms, p.subject.address).splitlines():
        ws.cell(row=r, column=1, value=line).font = (
            WARN if line.strip().startswith("!!") else Font(name="Menlo", size=9))
        r += 1
    return ws


# ── build ─────────────────────────────────────────────────────────────

BUILDERS = (sheet_overview, sheet_exits, sheet_comps, sheet_active,
            sheet_repair_logic, sheet_repair_numbers, sheet_buyers,
            sheet_outreach, sheet_lender)


def build_workbook(pack: WalkPack, out_path: str) -> str:
    wb = Workbook()
    wb.remove(wb.active)
    for fn in BUILDERS:
        fn(wb, pack)
    wb._sheets.sort(key=lambda s: SHEET_ORDER.index(s.title)
                    if s.title in SHEET_ORDER else 99)
    for ws in wb.worksheets:
        ws.sheet_view.showGridLines = False
        ws.freeze_panes = "A4"
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out))
    missing = [t for t in SHEET_ORDER if t not in wb.sheetnames]
    if missing:
        raise RuntimeError(f"Package is incomplete, missing sheets: {missing}")
    logger.info("Post-walkthrough package: %s", out)
    return str(out)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Build the 9-sheet post-walkthrough package")
    ap.add_argument("--walk", required=True, help="Walkthrough JSON (the human layer)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--comps", help="Optional pre-pulled comps JSON")
    ap.add_argument("--actives", help="Optional pre-pulled active listings JSON")
    ap.add_argument("--buyers", help="Optional buyer list JSON")
    ap.add_argument("--live-comps", action="store_true",
                    help="Pull comps live (METERED). Omitted, comps must be supplied.")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    walk = json.loads(Path(a.walk).read_text(encoding="utf-8"))
    comps = actives = buyers = None

    if a.comps:
        from comp_analyzer import CompProperty
        comps = [CompProperty(**row) for row in
                 json.loads(Path(a.comps).read_text(encoding="utf-8"))]
    elif a.live_comps:
        from comp_analyzer import SubjectProperty, fetch_comparable_sales
        prop = walk.get("property", {})
        subj = SubjectProperty(address=prop.get("address", ""), city=prop.get("city", ""),
                               state=prop.get("state", "NJ"), zip_code=prop.get("zip", ""),
                               sqft=int(prop.get("sqft") or 0),
                               bedrooms=int(prop.get("beds") or 0),
                               bathrooms=float(prop.get("baths") or 0),
                               year_built=int(prop.get("year_built") or 0))
        comps = fetch_comparable_sales(subj)
    if a.actives:
        actives = json.loads(Path(a.actives).read_text(encoding="utf-8"))
    if a.buyers:
        buyers = json.loads(Path(a.buyers).read_text(encoding="utf-8"))

    try:
        pack = build_pack(walk, comps=comps, actives=actives, buyers=buyers)
        build_workbook(pack, a.out)
    except (ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)
    for n in pack.notes:
        print(f"  !! {n}")


if __name__ == "__main__":
    main()
