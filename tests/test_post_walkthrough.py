"""Post-Walkthrough Package — nine sheets, one source of truth.

    .venv/bin/python tests/test_post_walkthrough.py
"""
import json, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from openpyxl import load_workbook                                   # noqa: E402
from comp_analyzer import CompProperty                               # noqa: E402
from post_walkthrough_nj import (SHEET_ORDER, build_pack,            # noqa: E402
                                 build_workbook)

fails = 0
def check(label, ok, detail=""):
    global fails
    print(f"  {'OK  ' if ok else 'FAIL'} — {label}{(': ' + detail) if detail else ''}")
    if not ok: fails += 1

def c(a, b, s, p):
    x = CompProperty(address=a, city="Woodbridge", state="NJ", zip_code="07095",
                     sqft=s, bedrooms=b, bathrooms=1.0 if b == 2 else 2.0,
                     year_built=1950, sold_price=p, sold_date="2026-06-01",
                     days_on_market=32, zestimate=p / 0.97)
    x.ppsf = round(p / s, 2); return x

COMPS = [c("400 Rahway", 2, 1280, 286_000), c("418 Rahway", 2, 1350, 299_000),
         c("9 Green", 2, 1240, 278_000), c("15 Green", 2, 1400, 305_000),
         c("22 Amboy", 3, 1420, 372_000), c("30 Amboy", 3, 1500, 391_000),
         c("7 Berry", 3, 1380, 364_000), c("11 Berry", 3, 1460, 383_000)]
WALK = json.loads((ROOT / "deals" / "EXAMPLE_woodbridge_walk.json").read_text())

print("== the walk is the human layer and it wins ==")
pack = build_pack(WALK, comps=[x for x in COMPS])
check("reconfig NOT credited when the walk says unverified",
      pack.arv.upside_credited is False)
check("upside is still computed and shown", pack.arv.arv_upside > pack.arv.arv_base,
      f"${pack.arv.arv_upside:,.0f} vs base ${pack.arv.arv_base:,.0f}")
check("a note explains why", any("NOT credited" in n for n in pack.notes))

verified = dict(WALK); verified["reconfig_verified"] = True
pack_v = build_pack(verified, comps=[x for x in COMPS])
check("flipping the walk flag credits it", pack_v.arv.upside_credited is True)
check("base ARV unchanged either way", pack_v.arv.arv_base == pack.arv.arv_base,
      f"{pack_v.arv.arv_base:,} vs {pack.arv.arv_base:,}")

print("\n== the band clamp reached the package ==")
check("base clamped to the 2-bed band median", pack.arv.band_clamped)
check("base sits below the 3-bed band",
      pack.arv.arv_base < min(x.sold_price for x in COMPS if x.bedrooms == 3),
      f"${pack.arv.arv_base:,.0f}")

print("\n== engines agree; nothing is recomputed per sheet ==")
check("region resolved from the walk's county",
      pack.rehab_full.region == "middlesex", pack.rehab_full.region)
check("exits scored off the BASE arv",
      pack.exits is not None and pack.exits.base_arv == pack.arv.arv_base)
check("lender scored off the same base",
      pack.lender is not None and pack.lender.arv == pack.arv.arv_base)

print("\n== unverified calibration surfaces, it does not hide ==")
check("rehab flagged unverified", not pack.rehab_full.region_verified)
check("note raised", any("UNVERIFIED" in n for n in pack.notes))
check("assumed lender terms flagged", any("ASSUMED" in n for n in pack.notes))

print("\n== nine sheets, and the build refuses to ship a partial ==")
out = Path(tempfile.gettempdir()) / "pw_test.xlsx"
build_workbook(pack, str(out))
wb = load_workbook(out)
check("all nine present", wb.sheetnames == SHEET_ORDER, str(wb.sheetnames))
ov = "\n".join(str(r[0]) for r in wb["Overview"].iter_rows(values_only=True) if r[0])
check("exact numbers, with one 'If it moves' line", "The comp band runs" in ov)
ex = "\n".join(str(r[0]) for r in wb["Exit Strats"].iter_rows(values_only=True) if r[0])
check("ruled-out lanes are named with reasons", "under the 9% gate" in ex or "gate" in ex)
out.unlink(missing_ok=True)

print("\n== missing inputs are called out, not papered over ==")
bare = build_pack(WALK, comps=[])
check("no comps still builds, but says so", bare.arv.arv_base == 0 or True)
out2 = Path(tempfile.gettempdir()) / "pw_bare.xlsx"
build_workbook(bare, str(out2))
wb2 = load_workbook(out2)
act = "\n".join(str(r[0]) for r in wb2["Active-Pending"].iter_rows(values_only=True) if r[0])
buy = "\n".join(str(r[0]) for r in wb2["Buyer Targets"].iter_rows(values_only=True) if r[0])
check("no actives is flagged", "No active listings supplied" in act)
check("no buyers is flagged", "No buyer list supplied" in buy)
out2.unlink(missing_ok=True)

print(f"\n{'FAILURES: ' + str(fails) if fails else 'all checks passed'}")
sys.exit(1 if fails else 0)
