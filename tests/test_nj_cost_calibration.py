"""NJ cost calibration — the gates that stop a guess becoming a number.

    .venv/bin/python tests/test_nj_cost_calibration.py
"""
import json, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import nj_cost_calibration as cal                                   # noqa: E402

fails = 0
def check(label, ok, detail=""):
    global fails
    print(f"  {'OK  ' if ok else 'FAIL'} — {label}{(': ' + detail) if detail else ''}")
    if not ok: fails += 1


def prop(key, labor_share=0.64, total=100_000, sqft=1500):
    """Synthetic property at a target labor share."""
    lab = total * labor_share
    mat = total - lab
    return {key: {"transactions": [
        {"date": "2025-06-01", "vendor": "Home Depot", "description": "materials",
         "amount": str(mat), "category": "rehab", "trade": "Materials", "type": "Expense"},
        {"date": "2025-06-02", "vendor": "Acme Construction LLC", "description": "labor",
         "amount": str(lab), "category": "rehab", "trade": "GC", "type": "Expense"},
    ], "draws": [{"date": "2025-07-01", "amount": str(total * 0.5), "description": "draw"}]}}


def write(tmp, payload, name):
    p = Path(tmp) / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return str(p)


print("== vendor classification ==")
check("retailer -> materials", cal.classify("Home Depot", "Materials") == "materials")
check("trade-named shop -> labor", cal.classify("Segundo Duchi Roof & Siding", "Roofing") == "labor")
check("civic -> soft", cal.classify("Township of Edison", "Permits") == "soft")
check("inspection trade -> soft", cal.classify("Anyone", "Survey/Inspection") == "soft")
check("a person's name -> PRESUMED, not silently labor",
      cal.classify("Diego Ruiz", "GC") == "labor_presumed")

print("\n== overrides win, and are validated ==")
cal.VENDOR_OVERRIDES.clear()
cal.VENDOR_OVERRIDES["diego ruiz"] = "labor"
check("override reclassifies", cal.classify("Diego Ruiz", "GC") == "labor")
cal.VENDOR_OVERRIDES.clear()

print("\n== n=1 refuses to propose ==")
tmp = tempfile.mkdtemp()
one = cal.load_spend([write(tmp, prop("a"), "a.json")], {})
r1 = cal.calibrate(one)
check("marked indicative", r1["confidence"].startswith("INDICATIVE"), r1["confidence"])
check("no proposal emitted", r1["proposal"] is None)
check("says why", any("at least 3" in w for w in r1["warnings"]))

print("\n== three agreeing properties DO propose ==")
files = [write(tmp, prop(k, 0.64), f"{k}.json") for k in ("a", "b", "c")]
r3 = cal.calibrate(cal.load_spend(files, {}))
check("proposal emitted", r3["proposal"] is not None)
check("confidence CALIBRATED", r3["confidence"] == "CALIBRATED", r3["confidence"])
check("observed share ~64%", abs(r3["median_labor_share"] - 0.64) < 0.01,
      f"{r3['median_labor_share']:.1%}")
reg = r3["proposal"]["regions"]["essex"]
check("re-weighting raises the effective multiplier",
      reg["effective_at_observed_mix"] > reg["effective_at_engine_mix"],
      f"{reg['effective_at_engine_mix']:.3f} -> {reg['effective_at_observed_mix']:.3f}")

print("\n== disagreeing properties are flagged, not averaged away ==")
mixed = [write(tmp, prop("d", 0.40), "d.json"),
         write(tmp, prop("e", 0.64), "e.json"),
         write(tmp, prop("f", 0.80), "f.json")]
rm = cal.calibrate(cal.load_spend(mixed, {}))
check("flagged provisional", rm["confidence"] == "PROVISIONAL", rm["confidence"])
check("spread named in the warning", any("spread" in w for w in rm["warnings"]))

print("\n== duplicate exports are not double-counted ==")
dup = cal.load_spend([files[0], files[0]], {})
check("same property key loaded once", len(dup) == 1, f"{len(dup)} loaded")

print("\n== presumed labor is surfaced with its weight ==")
pres = {"g": {"transactions": [
    {"date": "2025-06-01", "vendor": "Home Depot", "amount": "40000",
     "trade": "Materials", "description": "m", "category": "rehab", "type": "Expense"},
    {"date": "2025-06-02", "vendor": "Diego Ruiz", "amount": "60000",
     "trade": "GC", "description": "l", "category": "rehab", "type": "Expense"}], "draws": []}}
rp = cal.calibrate(cal.load_spend([write(tmp, pres, "g.json")], {}))
check("presumed share reported", rp["presumed_share_of_labor"] == 1.0,
      f"{rp['presumed_share_of_labor']:.0%}")
check("vendor listed for review",
      any(v == "Diego Ruiz" for v, _ in rp["unrecognised_vendors"]))

print("\n== sqft absent: split still works, $/sf withheld ==")
check("no per_sqft claimed", r1["median_per_sqft"] == 0.0)
withsq = cal.calibrate(cal.load_spend([files[0]], {"a": {"sqft": 1500, "beds": 3, "baths": 2}}))
check("per_sqft computed when sqft supplied", withsq["median_per_sqft"] > 0,
      f"${withsq['median_per_sqft']:.2f}/sf")

import shutil; shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{'FAILURES: ' + str(fails) if fails else 'all checks passed'}")
sys.exit(1 if fails else 0)
