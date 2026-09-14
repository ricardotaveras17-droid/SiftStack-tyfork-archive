"""Comp pull via /search — band partitioning, boundary clips, no live calls.

Every request is mocked. This suite must never touch the network: comp pulls
are metered, and a test that costs money will not get run.

    .venv/bin/python tests/test_comp_pull.py
"""
import re, sys, types
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import zillow_market_api as zma                                    # noqa: E402
import comp_analyzer as ca                                         # noqa: E402
from comp_analyzer import SubjectProperty                          # noqa: E402

fails, calls = 0, []


def check(label, ok, detail=""):
    global fails
    print(f"  {'OK  ' if ok else 'FAIL'} — {label}{(': ' + detail) if detail else ''}")
    if not ok:
        fails += 1


def make_rows(n, lo, hi):
    """Rows spread across streets and coordinates so the boundary clips have
    something to actually exclude — identical rows in every band would make a
    filter look like it works when it has simply nothing to do."""
    rows = []
    spread = (lo // 25_000) % 9          # vary the pocket per price band
    streets = ["Test St", "Elm St", "Grove Ter", "Park Pl", "Ridge Rd"]
    for i in range(n):
        price = lo + (hi - lo) * (i + 1) // (n + 1)
        k = spread + i
        rows.append({
            "zpid": f"{lo}-{i}",
            "streetAddress": f"{100 + k * 7} {streets[k % len(streets)]}",
            "addressCity": "Newark", "addressState": "NJ",
            "addressZipcode": "07103",
            "unformattedPrice": price, "soldPrice": f"${price:,}",
            "dateSold": 1_780_000_000_000, "latitude": 40.735 + k * 0.0015,
            "longitude": -74.172 - k * 0.0015, "livingArea": 1400,
            "bedrooms": 3, "bathrooms": 2, "yearBuilt": 1955,
            "homeType": "SINGLE_FAMILY", "daysOnZillow": 30, "lotAreaValue": 0.1,
            "lotAreaUnit": "acres",
        })
    return rows


def fake_get(url, params=None, headers=None, timeout=None):
    """Stand-in for requests.get. Saturates one band to force a split."""
    calls.append(dict(params or {}))
    lo = int(params.get("min_price", 0)); hi = int(params.get("max_price", 0))
    saturated = (lo, hi) == (285_001, 340_000)          # force a recursive split
    n = zma.RESULT_CAP if saturated else 3
    body = {"data": make_rows(n, max(lo, 1000), max(hi, 2000)),
            "parameters": {"location": params.get("location"),
                           "home_status": params.get("home_status"),
                           "min_price": lo, "max_price": hi}}
    return types.SimpleNamespace(json=lambda: body, raise_for_status=lambda: None)


zma.requests = types.SimpleNamespace(get=fake_get, RequestException=Exception)
zma.time = types.SimpleNamespace(sleep=lambda *_: None)

print("== price bands are chosen per market ==")
check("NJ gets the NJ spread", zma.bands_for("NJ") is zma.NJ_BANDS)
check("TN gets the TN spread", zma.bands_for("TN") is zma.TN_BANDS)
check("explicit bands win", zma.bands_for("NJ", [(1, 2)]) == [(1, 2)])
check("NJ top band starts above TN's",
      zma.NJ_BANDS[-1][0] > zma.TN_BANDS[-1][0],
      f"NJ ${zma.NJ_BANDS[-1][0]:,} vs TN ${zma.TN_BANDS[-1][0]:,}")

print("\n== saturated bands split recursively ==")
calls.clear()
api = zma.ZillowMarketAPI("test-key")
sold = api.pull_sold("Newark, NJ 07103", months_back=24, state="NJ")
band_calls = [(int(c["min_price"]), int(c["max_price"])) for c in calls if "min_price" in c]
check("one call per NJ band, plus splits",
      len(band_calls) > len(zma.NJ_BANDS),
      f"{len(band_calls)} calls for {len(zma.NJ_BANDS)} bands")
check("the saturated band was split",
      any(lo == 285_001 and hi < 340_000 for lo, hi in band_calls),
      str([b for b in band_calls if b[0] == 285_001]))
check("listings normalized", len(sold) > 0 and sold[0].price > 0,
      f"{len(sold)} listings")

print("\n== silently-dropped params are reported ==")
warned = []
zma.logger.warning = lambda msg, *a: warned.append(msg % a if a else msg)
def drop_get(url, params=None, headers=None, timeout=None):
    body = {"data": [], "parameters": {"location": params.get("location")}}   # drops price
    return types.SimpleNamespace(json=lambda: body, raise_for_status=lambda: None)
zma.requests = types.SimpleNamespace(get=drop_get, RequestException=Exception)
api.search("Newark, NJ", "RECENTLY_SOLD", min_price=1, max_price=2)
check("warns when the API ignores a filter",
      any("silently ignored" in w for w in warned), warned[0][:70] if warned else "")
zma.requests = types.SimpleNamespace(get=fake_get, RequestException=Exception)

print("\n== boundary clips both apply ==")
subject = SubjectProperty(address="1 Test St", city="Newark", state="NJ",
                          zip_code="07103", latitude=40.735, longitude=-74.172,
                          sqft=1400, bedrooms=3, bathrooms=2.0, year_built=1955)
ca.config.OPENWEBNINJA_API_KEY = "test-key"
sys.modules["zillow_market_api"].requests = zma.requests
base = ca.fetch_comparable_sales(subject, radius_miles=50, months_back=24)
check("comps come back at all", len(base) > 0, f"{len(base)} comps")
tight = ca.fetch_comparable_sales(subject, radius_miles=50, months_back=24,
                                  bbox=(40.730, 40.737, -74.180, -74.170))
check("bbox narrows the set", len(tight) < len(base), f"{len(base)} -> {len(tight)}")
streets = ca.fetch_comparable_sales(subject, radius_miles=50, months_back=24,
                                    street_pattern=re.compile(r"Test St|Elm St"))
check("street regex narrows the set", 0 < len(streets) < len(base),
      f"{len(base)} -> {len(streets)}")

print("\n== the retired endpoint is gone ==")
src = (ROOT / "src" / "comp_analyzer.py").read_text()
check("no live reference to similar-sale-homes",
      "COMPS_ENDPOINT" not in src)
check("no network call escaped the mock", all("location" in c for c in calls))

print(f"\n{'FAILURES: ' + str(fails) if fails else 'all checks passed'}")
sys.exit(1 if fails else 0)
