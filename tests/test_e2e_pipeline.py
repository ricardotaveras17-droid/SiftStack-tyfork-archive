"""End to end: comp pull -> ARV -> rehab -> MAO -> exit lanes, fully mocked."""
import sys, types, logging
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
logging.basicConfig(level=logging.ERROR)

import zillow_market_api as zma
sys.path.insert(0, str(ROOT / "tests"))

def make_rows(n, lo, hi):
    rows = []
    for i in range(n):
        price = lo + (hi - lo) * (i + 1) // (n + 1)
        rows.append({"zpid": f"{lo}-{i}", "streetAddress": f"{100+i*3} Elm St",
                     "addressCity": "Newark", "addressState": "NJ",
                     "addressZipcode": "07103", "unformattedPrice": price,
                     "dateSold": 1_780_000_000_000, "latitude": 40.735 + i*0.0008,
                     "longitude": -74.172 - i*0.0008, "livingArea": 1380 + i*40,
                     "bedrooms": 3, "bathrooms": 2, "yearBuilt": 1952 + i,
                     "homeType": "SINGLE_FAMILY", "daysOnZillow": 28 + i,
                     "lotAreaValue": 0.11, "lotAreaUnit": "acres"})
    return rows

def fake_get(url, params=None, headers=None, timeout=None):
    lo = int(params.get("min_price", 0)); hi = int(params.get("max_price", 0))
    # only the band around a real Newark resale returns rows
    n = 6 if lo <= 400_000 <= hi else 0
    body = {"data": make_rows(n, max(lo, 1000), max(hi, 2000)),
            "parameters": {"location": params.get("location"),
                           "home_status": params.get("home_status"),
                           "min_price": lo, "max_price": hi}}
    return types.SimpleNamespace(json=lambda: body, raise_for_status=lambda: None)

zma.requests = types.SimpleNamespace(get=fake_get, RequestException=Exception)
zma.time = types.SimpleNamespace(sleep=lambda *_: None)

import comp_analyzer as ca, deal_analyzer as da
from comp_analyzer import SubjectProperty
ca.config.OPENWEBNINJA_API_KEY = "test-key"

subject = SubjectProperty(address="123 Main St", city="Newark", state="NJ",
                          zip_code="07103", latitude=40.735, longitude=-74.172,
                          sqft=1400, bedrooms=3, bathrooms=2.0, year_built=1955)
da.fetch_subject_property = lambda *a, **k: subject
da.generate_deal_report = lambda *a, **k: "(report suppressed)"

print("1. comp pull via /search")
comps = ca.fetch_comparable_sales(subject, radius_miles=1.0, months_back=12)
print(f"   {len(comps)} comps, ppsf range "
      f"${min(c.ppsf for c in comps):,.0f}-${max(c.ppsf for c in comps):,.0f}")
assert comps, "no comps — pipeline still blocked"

print("2. ARV from Two-Bucket")
arv = ca.calculate_arv(subject, comps)
print(f"   ARV ${arv.arv_mid:,.0f} ({arv.confidence}) — "
      f"bucket A {arv.bucket_a_count} / B {arv.bucket_b_count}")
assert arv.confidence != "none"

print("3. full deal analysis (comp -> rehab -> MAO)")
res = da.run_deal_analysis(address="123 Main St, Newark, NJ 07103", county="Essex")
assert "error" not in res, res.get("error")
pkg = res["package"]
print(f"   region {pkg.rehab_full.region} | rehab ${pkg.rehab_full.grand_total:,} "
      f"| transfer ${pkg.selling_costs.transfer_tax:,}")
print(f"   flip MAO ${pkg.mao.flip_mao:,.0f}")

print("4. exit lanes on the same numbers")
from exit_strategy import analyse
ex = analyse(base_arv=arv.arv_mid, quoted_rehab=pkg.rehab_full.grand_total,
             offer=pkg.mao.flip_mao, scope="med", state="NJ")
print(f"   {ex.headline[:88]}")
print(f"   clears: {[l.key for l in ex.suggested] or '-none-'}")

print("\nPIPELINE UNBLOCKED — comp pull through exit lanes, no live calls.")
