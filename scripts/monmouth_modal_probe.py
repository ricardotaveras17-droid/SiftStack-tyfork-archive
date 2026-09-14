"""Do Monmouth's record portals answer from Modal's datacenter IPs?

The Bluestone probate counties (Middlesex/Somerset/Ocean) 403 or get
Cloudflare-challenged from Modal egress, which is why we keep local recovery
scripts on a residential connection. Monmouth's two portals are ordinary
IIS/ASP.NET with no Cloudflare in front of them as of 2026-08-26, so they
may well answer from the cloud — but "may well" is not a fact, and the whole
NJ IP-block file exists because that assumption was wrong before.

This settles it for the two hosts a Monmouth build would depend on:

  mcapps.co.monmouth.nj.us   the Surrogate index (decedent + town + DOD)
  oprs.co.monmouth.nj.us     the Clerk deed index (name -> town/block/lot)

Deliberately a standalone app — it does not touch modal_app.py or the
"siftstack" app, so a probe run cannot disturb the Wednesday cron.

    modal run scripts/monmouth_modal_probe.py

Each host is checked for reachability, for a Cloudflare interstitial, and
for the specific control the scrapers depend on. Run the same checks locally
for the residential control:

    python scripts/monmouth_modal_probe.py --local
"""
from __future__ import annotations

import sys

import modal

app = modal.App("monmouth-egress-probe")
image = modal.Image.debian_slim(python_version="3.12").pip_install("requests")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# (label, url, a marker that proves we got the real page and not a gate)
TARGETS = [
    ("surrogate-index",
     "https://mcapps.co.monmouth.nj.us/Surrogate/SurrogateList.aspx",
     "ContentPlaceHolder1$IAgree"),
    ("oprs-clerk-deeds",
     "https://oprs.co.monmouth.nj.us/oprs/clerk/clerkhome.aspx?op=basic",
     "txtLastNameTab1"),
]

_CF_SIGNALS = ("just a moment", "cf-browser-verification", "cf_chl_opt",
               "attention required", "checking your browser")


def _check(label: str, url: str, marker: str) -> dict:
    import requests

    out = {"target": label, "url": url}
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=45)
    except requests.RequestException as e:
        out.update(ok=False, status=None, reason=f"{type(e).__name__}: {e}")
        return out
    body = r.text or ""
    low = body.lower()
    out["status"] = r.status_code
    out["bytes"] = len(body)
    out["cf_ray"] = r.headers.get("cf-ray", "")
    if any(sig in low for sig in _CF_SIGNALS) or out["cf_ray"]:
        out.update(ok=False, reason="cloudflare challenge/interstitial")
    elif r.status_code != 200:
        out.update(ok=False, reason=f"HTTP {r.status_code}")
    elif marker not in body:
        out.update(ok=False, reason=f"page served but marker {marker!r} missing")
    else:
        out.update(ok=True, reason="reachable, expected control present")
    return out


def _report(where: str, results: list[dict], *, cloud: bool) -> bool:
    print(f"\nMonmouth egress probe — {where}")
    print("=" * 62)
    for r in results:
        flag = "OK  " if r["ok"] else "FAIL"
        print(f"  [{flag}] {r['target']:18} {r['reason']}")
        if r.get("status") is not None:
            print(f"           HTTP {r['status']}, {r.get('bytes', 0)} bytes"
                  + (f", cf-ray={r['cf_ray']}" if r.get("cf_ray") else ""))
    every = all(r["ok"] for r in results)
    print("-" * 62)
    if every and cloud:
        print("  VERDICT: both portals answer from Modal — a Monmouth scraper could")
        print("           run in the cloud alongside the weekly cron.")
    elif cloud:
        print("  VERDICT: at least one portal is blocked from Modal — a Monmouth")
        print("           scraper would need the local-recovery treatment, same as")
        print("           Bluestone. Re-run --local to confirm it is the egress IP")
        print("           and not the portal being down for everyone.")
    elif every:
        print("  VERDICT: control good — both portals are up and unchanged, so a")
        print("           Modal failure would be about egress, not availability.")
    else:
        print("  VERDICT: the control itself failed — the portal is down or changed.")
        print("           Fix that before reading anything into a Modal result.")
    return every


@app.function(image=image, timeout=300)
def probe_from_modal() -> list[dict]:
    return [_check(*t) for t in TARGETS]


@app.local_entrypoint()
def main():
    _report("MODAL datacenter egress", probe_from_modal.remote(), cloud=True)
    print("\nCompare against the residential control:")
    print("    python scripts/monmouth_modal_probe.py --local")


if __name__ == "__main__":
    if "--local" in sys.argv:
        _report("LOCAL (residential control)", [_check(*t) for t in TARGETS], cloud=False)
    else:
        print(__doc__)
        print("Run `modal run scripts/monmouth_modal_probe.py` for the cloud check,")
        print("or `python scripts/monmouth_modal_probe.py --local` for the control.")
