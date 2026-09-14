"""NJ cost calibration from real construction spend.

Reads the property-construction JSON exports (transactions + draws) and derives
what a New Jersey rehab actually costs us, so the rehab engine stops running on
an index estimate and starts running on our own closed jobs.

Two things come out of it:

  1. THE LABOR / MATERIALS SPLIT. The engine's tables assume roughly 60%
     materials / 40% labor. The first real property read the other way round.
     That matters more than the multiplier, because the labor multiplier is
     what does the NJ correction, and applying it to a table that thinks labor
     is a minority of the job under-corrects.

  2. COST PER SQUARE FOOT, where the property's size is known, checked against
     what the engine would have estimated for the same house.

It PROPOSES; it does not apply. Output is a review file plus a printed diff
against the multipliers currently in the engine. Nothing is written into
rehab_estimator by this script, for the same reason the material list only
locks on an explicit act: a calibration that installs itself is a calibration
nobody checked.

n=1 IS NOT A CALIBRATION. One property can be idiosyncratic — a GC who bundles
materials into a labor invoice moves the split on its own. Below MIN_PROPERTIES
the output is marked INDICATIVE and the proposed multipliers are withheld.

Usage:
  python src/nj_cost_calibration.py --spend "DNT-Operations/Property-Construction-Data/*.json" \\
      --specs deals/property_specs.json --out output/nj_calibration.json
"""

import argparse
import glob
import json
import logging
import statistics
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

MIN_PROPERTIES = 3          # below this, propose nothing
SPLIT_AGREEMENT_TOL = 0.08  # properties must agree within 8 points to trust a split

# ── Vendor classification ─────────────────────────────────────────────
# Vendor decides materials-vs-labor. Trade decides WHICH trade. A plumbing
# charge at Home Depot is plumbing MATERIALS; the same trade from a sub is
# plumbing LABOR, and conflating them is how the split goes wrong.
#
# Extend these lists rather than letting an unknown vendor default into a
# bucket: unknowns are reported, never absorbed.
MATERIAL_VENDORS = (
    "home depot", "lowes", "lowe's", "menards", "home surplus", "amazon",
    "best buy", "walmart", "target", "harbor freight", "floor & decor",
    "floor and decor", "wayfair", "ikea", "ferguson", "sherwin", "benjamin moore",
    "ppg", "84 lumber", "builders firstsource", "abc supply", "beacon",
    "supply", "wholesale", "lumber", "tile shop", "appliance",
)
SOFT_TRADES = ("Survey/Inspection", "Staging", "Permits", "Legal", "Insurance",
               "Utilities", "Marketing")
SOFT_VENDOR_HINTS = ("township", "borough", "city of", "county of", "inspection",
                     "permit", "surveyor", "insurance")


@dataclass
class Txn:
    date: str = ""
    vendor: str = ""
    description: str = ""
    amount: float = 0.0
    trade: str = ""
    bucket: str = ""        # materials | labor | soft | unknown


@dataclass
class PropertySpend:
    key: str = ""
    address: str = ""
    txns: list = field(default_factory=list)
    draws: list = field(default_factory=list)
    sqft: int = 0
    beds: int = 0
    baths: float = 0.0
    year_built: int = 0
    # Scope matters more than anything else in the comparison: asking the engine
    # for a full gut and comparing it to a wholetail is not a calibration
    # finding, it is the wrong question. Set "scope" in the specs file.
    scope: str = "full"       # full | wholetail
    tier: int = 2

    @property
    def total(self) -> float:
        return sum(t.amount for t in self.txns)

    def bucket_total(self, bucket: str) -> float:
        return sum(t.amount for t in self.txns if t.bucket == bucket)

    @property
    def labor_all(self) -> float:
        """Known subs plus presumed. The presumed share is reported separately."""
        return self.bucket_total("labor") + self.bucket_total("labor_presumed")

    @property
    def hard_cost(self) -> float:
        """Materials + labor. Soft costs are excluded from the split."""
        return self.bucket_total("materials") + self.labor_all

    @property
    def labor_share(self) -> float:
        return self.labor_all / self.hard_cost if self.hard_cost else 0.0

    @property
    def presumed_share(self) -> float:
        """How much of the labor figure rests on an unrecognised vendor."""
        return (self.bucket_total("labor_presumed") / self.labor_all
                if self.labor_all else 0.0)


# Vendors positively known to be subs/trades. Grows as jobs are read; anything
# not on either list is PRESUMED labor and reported, never absorbed silently.
LABOR_VENDOR_HINTS = (
    "contract", "construction", "carpentry", "plumbing", "electric", "hvac",
    "roofing", "roof", "paving", "masonry", "painting", "landscap", "drywall",
    "flooring", "floors", "hardwood", "remodel", "renovation", "builders",
    "& sons", "and sons", "services", "llc", "inc",
    # trade-named shops seen in real NJ jobs
    "marble", "granite", "siding", "screens", "glaze", "glass", "cabinet",
    "concrete", "demo", "haul", "dumpster", "cleaning", "junk", "gutter",
    "insulation", "stucco", "chimney", "septic", "well", "pest", "termite",
)


# Filled from --vendors. A person's name cannot be pattern-matched, so the
# only honest way to classify "Diego Ruiz" is for a human to say once which
# bucket he is. Overrides win over every rule below.
VENDOR_OVERRIDES: dict = {}


def classify(vendor: str, trade: str) -> str:
    """materials | labor | labor_presumed | soft.

    ``labor_presumed`` is counted as labor in the arithmetic but listed
    separately in the report with its dollar weight, so the share of the split
    resting on a guess is visible rather than buried. A vendor list that
    silently absorbs what it does not recognise produces a confident number
    built on unexamined rows.
    """
    v = (vendor or "").strip().lower()
    if not v:
        return "labor_presumed"
    if v in VENDOR_OVERRIDES:
        return VENDOR_OVERRIDES[v]
    if (trade or "") in SOFT_TRADES or any(h in v for h in SOFT_VENDOR_HINTS):
        return "soft"
    if any(m in v for m in MATERIAL_VENDORS):
        return "materials"
    if any(l in v for l in LABOR_VENDOR_HINTS):
        return "labor"
    return "labor_presumed"


# ── loading ───────────────────────────────────────────────────────────

def _norm_scope(raw) -> str:
    """Map the pipeline's deal-type vocabulary onto the engine's two scopes."""
    s = (raw or "full").strip().lower()
    if s in ("wholetail", "cosmetic flip", "cosmetic", "light", "paint and carpet"):
        return "wholetail"
    return "full"


def load_spend(paths: list, specs: dict) -> list:
    """Read the construction JSON exports into PropertySpend records.

    Export shape: {property_key: {transactions: [...], draws: [...]}}. Specs
    (address, sqft, beds, baths, year) are NOT in the export, so they come from
    a sidecar keyed the same way. Without sqft a property still calibrates the
    split; it just cannot contribute a $/SF.
    """
    seen, out = set(), []
    for path in paths:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Could not read %s (%s) — skipped", path, exc)
            continue
        for key, body in (data or {}).items():
            if not isinstance(body, dict) or "transactions" not in body:
                continue
            if key in seen:
                logger.info("Property %s seen already; keeping the first copy "
                            "(duplicate exports would double-weight it)", key)
                continue
            seen.add(key)
            spec = specs.get(key, {})
            txns = []
            for t in body.get("transactions", []):
                try:
                    amt = float(t.get("amount") or 0)
                except (TypeError, ValueError):
                    continue
                if amt <= 0:
                    continue
                txns.append(Txn(date=t.get("date", ""), vendor=t.get("vendor", ""),
                                description=t.get("description", ""), amount=amt,
                                trade=t.get("trade", ""),
                                bucket=classify(t.get("vendor", ""), t.get("trade", ""))))
            draws = []
            for d in body.get("draws", []):
                try:
                    draws.append(float(d.get("amount") or 0))
                except (TypeError, ValueError):
                    pass
            out.append(PropertySpend(
                key=key, address=spec.get("address", key), txns=txns, draws=draws,
                sqft=int(spec.get("sqft") or 0), beds=int(spec.get("beds") or 0),
                baths=float(spec.get("baths") or 0),
                year_built=int(spec.get("year_built") or 0),
                scope=_norm_scope(spec.get("scope")), tier=int(spec.get("tier") or 2)))
    return out


# ── analysis ──────────────────────────────────────────────────────────

def trade_mix(props: list) -> dict:
    """Share of hard cost by trade, pooled across properties."""
    totals, grand = {}, 0.0
    for p in props:
        for t in p.txns:
            if t.bucket == "soft":
                continue
            totals[t.trade or "(untagged)"] = totals.get(t.trade or "(untagged)", 0.0) + t.amount
            grand += t.amount
    return {k: v / grand for k, v in sorted(totals.items(), key=lambda kv: -kv[1])} if grand else {}


def unrecognised_vendors(props: list) -> list:
    """Presumed-labor vendors, biggest first. This is the review queue."""
    tally = {}
    for p in props:
        for t in p.txns:
            if t.bucket == "labor_presumed":
                tally[t.vendor or "(blank)"] = tally.get(t.vendor or "(blank)", 0.0) + t.amount
    return sorted(tally.items(), key=lambda kv: -kv[1])


def engine_estimate(p: PropertySpend, region: str) -> dict | None:
    """What the engine would have said for this house, for comparison."""
    if not (p.sqft and p.beds):
        return None
    try:
        from rehab_estimator import estimate_rehab
        e = estimate_rehab(sqft=p.sqft, bedrooms=p.beds,
                           bathrooms=p.baths or 2.0, year_built=p.year_built,
                           tier=p.tier, scope=p.scope, region=region)
    except Exception as exc:
        logger.warning("Engine estimate failed for %s: %s", p.key, exc)
        return None
    tot = e.total_materials + e.total_labor
    return {"grand_total": e.grand_total, "materials": e.total_materials,
            "labor": e.total_labor,
            "labor_share": e.total_labor / tot if tot else 0.0,
            "per_sqft": e.grand_total / p.sqft if p.sqft else 0.0}


# ── calibration ───────────────────────────────────────────────────────

def calibrate(props: list, region: str = "middlesex") -> dict:
    """Aggregate the properties into a proposal, gated on having enough of them."""
    usable = [p for p in props if p.hard_cost > 0]
    shares = [p.labor_share for p in usable]
    with_sqft = [p for p in usable if p.sqft]
    per_sqft = [p.total / p.sqft for p in with_sqft]

    result = {
        "properties": len(usable),
        "properties_with_sqft": len(with_sqft),
        "total_spend": round(sum(p.total for p in usable)),
        "labor_shares": [round(s, 4) for s in shares],
        "median_labor_share": round(statistics.median(shares), 4) if shares else 0.0,
        "median_per_sqft": round(statistics.median(per_sqft), 2) if per_sqft else 0.0,
        "trade_mix": {k: round(v, 4) for k, v in trade_mix(usable).items()},
        "unrecognised_vendors": [(v, round(a)) for v, a in unrecognised_vendors(usable)[:20]],
        "presumed_share_of_labor": round(
            statistics.median([p.presumed_share for p in usable]), 4) if usable else 0.0,
        "per_property": [],
        "proposal": None,
        "confidence": "",
        "warnings": [],
    }

    for p in usable:
        row = {"key": p.key, "address": p.address, "scope": p.scope,
               "total": round(p.total),
               "sqft": p.sqft, "labor_share": round(p.labor_share, 4),
               "presumed_share_of_labor": round(p.presumed_share, 4),
               "materials": round(p.bucket_total("materials")),
               "labor": round(p.labor_all), "soft": round(p.bucket_total("soft")),
               "draws": round(sum(p.draws))}
        if p.sqft:
            row["per_sqft"] = round(p.total / p.sqft, 2)
        est = engine_estimate(p, region)
        if est:
            row["engine"] = {k: (round(v, 4) if isinstance(v, float) else v)
                             for k, v in est.items()}
            row["engine_vs_actual"] = round(est["grand_total"] / p.total, 3) if p.total else 0
        result["per_property"].append(row)

    # ── gates ─────────────────────────────────────────────────────────
    if len(usable) < MIN_PROPERTIES:
        result["confidence"] = "INDICATIVE — NOT A CALIBRATION"
        result["warnings"].append(
            f"{len(usable)} property(ies). A calibration needs at least "
            f"{MIN_PROPERTIES}: one job can be idiosyncratic, and a general "
            "contractor who bundles materials into a labor invoice moves the "
            "split on its own. No multipliers are proposed.")
        return result

    spread = max(shares) - min(shares)
    if spread > SPLIT_AGREEMENT_TOL:
        result["warnings"].append(
            f"Labor share ranges {min(shares):.0%}-{max(shares):.0%} across "
            f"{len(usable)} properties — a {spread:.0%} spread, wider than the "
            f"{SPLIT_AGREEMENT_TOL:.0%} tolerance. The median is reported but "
            "these jobs are not the same kind of job; segment before trusting it.")
    if result["presumed_share_of_labor"] > 0.25:
        result["warnings"].append(
            f"{result['presumed_share_of_labor']:.0%} of the labor figure comes from "
            "vendors the classifier did not recognise. Review "
            "unrecognised_vendors and extend the vendor lists before relying "
            "on the split.")

    result["confidence"] = ("CALIBRATED" if not result["warnings"] else "PROVISIONAL")
    result["proposal"] = _proposal(result, usable)
    return result


def _proposal(result: dict, props: list) -> dict:
    """Restate the engine's split multiplier at the OBSERVED labor mix.

    The multipliers themselves are not re-derived here — that needs bids
    against a known scope, not spend totals. What this fixes is the WEIGHTING:
    an effective multiplier computed at a 40% labor mix understates a market
    whose jobs actually run at 64%.
    """
    from rehab_estimator import REGIONAL_RATES
    observed = result["median_labor_share"]
    out = {"observed_labor_share": observed, "regions": {}}
    for name in ("essex", "union", "middlesex", "somerset"):
        r = REGIONAL_RATES.get(name)
        if not r:
            continue
        engine_mix = 0.40      # what the engine's own tables produce
        out["regions"][name] = {
            "labor": r.labor, "materials": r.materials,
            "effective_at_engine_mix": round(engine_mix * r.labor + (1 - engine_mix) * r.materials, 4),
            "effective_at_observed_mix": round(observed * r.labor + (1 - observed) * r.materials, 4),
        }
    out["note"] = ("These are the SAME labor and materials multipliers, re-weighted "
                   "at the observed mix. Re-deriving the multipliers themselves "
                   "requires GC bids against a known scope, which spend totals "
                   "cannot give you.")
    return out


# ── reporting ─────────────────────────────────────────────────────────

def render(result: dict) -> str:
    L = ["NJ COST CALIBRATION", ""]
    L.append(f"  Confidence: {result['confidence']}")
    L.append(f"  {result['properties']} property(ies), "
             f"{result['properties_with_sqft']} with square footage, "
             f"${result['total_spend']:,} of spend")
    L.append("")
    for w in result["warnings"]:
        L.append(f"  !! {w}")
    if result["warnings"]:
        L.append("")

    L.append("PER PROPERTY")
    L.append(f"  {'address':<34}{'total':>10}{'sqft':>7}{'$/sf':>8}"
             f"{'labor%':>8}{'presumed':>10}")
    for p in result["per_property"]:
        L.append(f"  {p['address'][:33]:<34}${p['total']:>9,}"
                 f"{p['sqft'] or '-':>7}"
                 f"{('$%.0f' % p['per_sqft']) if p.get('per_sqft') else '-':>8}"
                 f"{p['labor_share']:>8.1%}{p['presumed_share_of_labor']:>10.0%}")
        if "engine" in p:
            e = p["engine"]
            L.append(f"      engine at {p.get('scope','full')} scope: ${e['grand_total']:,} "
                     f"(${e['per_sqft']:.0f}/sf, labor {e['labor_share']:.0%}) — "
                     f"engine/actual {p['engine_vs_actual']:.2f}x")
    L.append("")

    L.append("COST STRUCTURE")
    L.append(f"  observed labor share (median)   {result['median_labor_share']:.1%}")
    L.append(f"  engine's own tables assume      40.0%")
    if result["median_per_sqft"]:
        L.append(f"  observed $/sqft (median)        ${result['median_per_sqft']:,.2f}")
    L.append("")

    if result["trade_mix"]:
        L.append("TRADE MIX (share of hard cost)")
        for k, v in list(result["trade_mix"].items())[:14]:
            L.append(f"  {k:<26}{v:>7.1%}")
        L.append("")

    if result["unrecognised_vendors"]:
        L.append("VENDORS THE CLASSIFIER DID NOT RECOGNISE — counted as labor, review these")
        for v, a in result["unrecognised_vendors"][:12]:
            L.append(f"  {v[:40]:<42}${a:>9,}")
        L.append("  Extend MATERIAL_VENDORS / LABOR_VENDOR_HINTS and re-run.")
        L.append("")

    prop = result.get("proposal")
    if prop:
        L.append("PROPOSED RE-WEIGHTING (same multipliers, observed mix)")
        L.append(f"  {'region':<12}{'labor':>8}{'matls':>8}"
                 f"{'eff @40%':>11}{'eff @obs':>11}{'change':>9}")
        for name, r in prop["regions"].items():
            delta = r["effective_at_observed_mix"] - r["effective_at_engine_mix"]
            L.append(f"  {name:<12}{r['labor']:>8.2f}{r['materials']:>8.2f}"
                     f"{r['effective_at_engine_mix']:>11.3f}"
                     f"{r['effective_at_observed_mix']:>11.3f}{delta:>+9.3f}")
        L.append("")
        L.append(f"  {prop['note']}")
    else:
        L.append("NO PROPOSAL — see the warning above.")
    L.append("")
    L.append("  Nothing was written to the engine. This is a review artifact.")
    return "\n".join(L)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Calibrate NJ rehab costs from real spend")
    ap.add_argument("--spend", nargs="+", required=True,
                    help="Construction JSON export(s); globs allowed")
    ap.add_argument("--specs", help="Sidecar JSON of property specs keyed by property id")
    ap.add_argument("--region", default="middlesex", help="Region to compare the engine against")
    ap.add_argument("--vendors", help='JSON of vendor -> bucket, e.g. '
                    '{"Diego Ruiz": "labor", "ABC Supply": "materials"}. '
                    "Overrides every rule; this is how named tradespeople get classified.")
    ap.add_argument("--write-vendor-stub", metavar="PATH",
                    help="Write the unrecognised vendors to a stub file for you to fill in")
    ap.add_argument("--out", help="Write the full result JSON here")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    paths = []
    for pattern in a.spend:
        hits = glob.glob(pattern)
        if not hits:
            logger.warning("No files matched %s", pattern)
        paths.extend(hits)
    if not paths:
        print("ERROR: no spend files matched.")
        raise SystemExit(1)

    specs = {}
    if a.specs:
        try:
            specs = json.loads(Path(a.specs).read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"ERROR: could not read specs ({exc})")
            raise SystemExit(1)

    if a.vendors:
        try:
            raw = json.loads(Path(a.vendors).read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"ERROR: could not read vendor overrides ({exc})")
            raise SystemExit(1)
        valid = {"materials", "labor", "soft"}
        bad = {k: v for k, v in raw.items() if v not in valid}
        if bad:
            print(f"ERROR: vendor overrides must be one of {sorted(valid)}; bad "
                  f"entries: {bad}")
            raise SystemExit(1)
        VENDOR_OVERRIDES.update({k.strip().lower(): v for k, v in raw.items()})
        logger.info("Loaded %d vendor override(s)", len(VENDOR_OVERRIDES))

    props = load_spend(paths, specs)
    if not props:
        print("ERROR: no properties with transactions were found in those files.")
        raise SystemExit(1)

    result = calibrate(props, a.region)
    print(render(result))
    if a.write_vendor_stub:
        stub = {v: "labor" for v, _ in result["unrecognised_vendors"]}
        sp = Path(a.write_vendor_stub)
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(stub, indent=2), encoding="utf-8")
        print(f"  Vendor stub ({len(stub)} entries, all defaulted to 'labor' — "
              f"correct any that are materials): {sp}")
    if a.out:
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"  Full result: {out}")


if __name__ == "__main__":
    main()
