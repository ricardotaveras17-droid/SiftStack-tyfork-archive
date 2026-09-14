"""A missing ARV must abort, not render a branded $0 deal package.

Before the fix this path logged a warning and continued, producing a complete
report in which ARV, MAO and every projection were $0 — output that reads as
deliberate rather than broken.

    .venv/bin/python tests/test_fail_loud_arv.py
"""
import logging
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
logging.basicConfig(level=logging.CRITICAL)

import deal_analyzer as da                                        # noqa: E402
from comp_analyzer import SubjectProperty                         # noqa: E402

fails = 0
def check(label, ok, detail=""):
    global fails
    print(f"  {'OK  ' if ok else 'FAIL'} — {label}{(': ' + detail) if detail else ''}")
    if not ok: fails += 1

subject = SubjectProperty(address="123 Test St", city="Newark", state="NJ",
                          zip_code="07103", sqft=1400, bedrooms=3,
                          bathrooms=2.0, year_built=1955)
da.fetch_subject_property = lambda *a, **k: subject
da.fetch_comparable_sales = lambda *a, **k: []          # the dead-endpoint case

print("== zero comps ==")
result = da.run_deal_analysis(address="123 Test St, Newark, NJ 07103", county="Essex")
check("returns an error", "error" in result, str(result.get("error", ""))[:70])
check("no deal package was built", "package" not in result)
check("no report was written", "report_path" not in result)
check("subject and comps returned for debugging",
      "subject" in result and "comps" in result)

print(f"\n{'FAILURES: ' + str(fails) if fails else 'all checks passed'}")
sys.exit(1 if fails else 0)
