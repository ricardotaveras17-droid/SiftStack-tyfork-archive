"""Local Middlesex + Somerset + Ocean probate backfill.

Use when Modal's egress IPs are CF-blocked from the Bluestone portals.
Scrapes all three counties from the laptop (clean IP), combines, enriches,
writes a single CSV, optionally uploads to DataSift + Slack.

Run with:
  PYTHONPATH=src python scripts/nj_probate_local_backfill.py \
      --mx-days-back 180 --som-days-back 30 --ocean-days-back 180 \
      --upload-datasift --notify-slack
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


# Why each source came back empty, from the last _scrape_all. A source that
# was BLOCKED must never render as a bare "0 notices" — that is the exact
# silent zero that hid Somerset's Cloudflare block for months.
SOURCE_ERRORS: dict[str, str] = {}


def _status_line(label: str, count: int) -> str:
    """One summary line that can never hide a failure behind a zero.

    Three distinct states that all used to print as "0 notices":
      BLOCKED/FAILED  - the scraper never got to look. Re-run recovers it.
      clean zero      - the scraper ran fine and the county filed nothing.
      normal          - records found.
    """
    err = SOURCE_ERRORS.get(label)
    if err:
        return f"  :red_circle: {label}: {count} notices — {err}"
    if count == 0:
        return f"  :warning: {label}: 0 notices (ran clean — verify the county really filed nothing)"
    return f"  {label}: {count} notices"


async def _scrape_all(
    mx_days_back: int, som_days_back: int, ocean_days_back: int, headless: bool,
):
    """Scrape Middlesex (DoD) + Somerset (File-Date) + Ocean (DoD) sequentially.

    Sequential rather than parallel to avoid Playwright contention in a
    single Python process — the DoD scans dominate wall-clock anyway, so
    parallelism saves <10%. Ocean is the same Bluestone deployment as
    Middlesex (Death-Date filter), so it uses a DoD scan too.
    """
    from nj_middlesex_probate import (
        scrape_middlesex_probates,
        scrape_somerset_probates,
        scrape_ocean_probates,
        CloudflareBlockError,
    )

    SOURCE_ERRORS.clear()

    logger = logging.getLogger("nj_probate_backfill")

    async def _safe(label, coro):
        """Run one scraper; never let its failure discard the others' work.

        Mirrors modal_app's _safe wrapper. This script is what you reach for
        when something has ALREADY gone wrong, so a crash in a later source
        must not throw away an earlier one: on 2026-09-10 a Playwright error
        in Somerset discarded 1,042 successfully scraped Middlesex records
        and 27 minutes of wall-clock.
        """
        try:
            out = await coro
            logger.info("%s: %d notices", label, len(out))
            return out
        except CloudflareBlockError as e:
            SOURCE_ERRORS[label] = f"BLOCKED (Cloudflare) — {e}"
            logger.error("%s BLOCKED by Cloudflare — continuing with other sources: %s",
                         label, e)
            return []
        except Exception as e:
            SOURCE_ERRORS[label] = f"FAILED ({type(e).__name__}) — {e}"
            logger.error("%s FAILED (%s: %s) — continuing with other sources",
                         label, type(e).__name__, e)
            return []

    logger.info("Middlesex: %d days DoD scan", mx_days_back)
    mx = await _safe(
        "Middlesex",
        scrape_middlesex_probates(days_back=mx_days_back, headless=headless),
    )

    logger.info("Somerset: %d days file-date scan", som_days_back)
    som = await _safe(
        "Somerset",
        scrape_somerset_probates(days_back=som_days_back, headless=headless),
    )

    # Ocean is OFF by default, matching modal_app's weekly cron, which
    # deliberately does not import or run it: those leads are parked until
    # the team has capacity to work them. Running it anyway cost ~27 minutes
    # of scraping per backfill for data nobody reads. Pass --include-ocean
    # to turn it back on.
    if ocean_days_back > 0:
        logger.info("Ocean: %d days DoD scan", ocean_days_back)
        ocean = await _safe(
            "Ocean",
            scrape_ocean_probates(days_back=ocean_days_back, headless=headless),
        )
    else:
        logger.info("Ocean: skipped (parked — pass --include-ocean to run)")
        ocean = []

    return mx, som, ocean


def main() -> int:
    p = argparse.ArgumentParser(description="Local Middlesex + Somerset probate backfill")
    p.add_argument("--mx-days-back", type=int, default=180,
                   help="Middlesex DoD window (default 180)")
    p.add_argument("--som-days-back", type=int, default=30,
                   help="Somerset file-date window (default 30)")
    p.add_argument("--include-ocean", action="store_true",
                   help="Also scrape Ocean (parked by default, matches the weekly cron)")
    p.add_argument("--ocean-days-back", type=int, default=180,
                   help="Ocean DoD window (default 180)")
    p.add_argument("--headed", action="store_true", help="Show browser windows")
    p.add_argument("--upload-datasift", action="store_true",
                   help="Upload combined CSV to DataSift after enrichment")
    p.add_argument("--notify-slack", action="store_true",
                   help="Post run summary to Slack webhook")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("nj_probate_backfill")

    mx, som, ocean = asyncio.run(_scrape_all(
        mx_days_back=args.mx_days_back,
        som_days_back=args.som_days_back,
        ocean_days_back=args.ocean_days_back if args.include_ocean else 0,
        headless=not args.headed,
    ))
    combined = mx + som + ocean
    if not combined:
        logger.error("All scrapers returned 0 records — nothing to enrich")
        return 1

    from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline
    opts = PipelineOptions(
        skip_filter_sold=False,
        skip_tax=True,
        skip_obituary=False,
        skip_ancestry=False,
        skip_dm_address=False,
        skip_heir_verification=False,
        skip_parcel_lookup=True,
        source_label=f"NJ Probate Backfill (Middlesex {args.mx_days_back}d + Somerset {args.som_days_back}d)",
    )
    enriched = run_enrichment_pipeline(combined, opts)

    from data_formatter import write_csv, write_csv_by_list
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    csv_path = write_csv(enriched, f"nj_probate_backfill_{ts}.csv")
    logger.info("Combined CSV: %s (%d records)", csv_path, len(enriched))

    import config
    paused = config.SIFTSTACK_UPLOAD_PAUSED_TYPES
    upload_ready = [n for n in enriched if (n.notice_type or "").lower() not in paused]
    held_back = [n for n in enriched if (n.notice_type or "").lower() in paused]
    if held_back:
        held_csv = write_csv(held_back, f"nj_probate_backfill_{ts}_HELD_FOR_CLEANING.csv")
        logger.info("Held for cleaning: %d records (probate paused) -> %s",
                    len(held_back), held_csv)
    by_list = write_csv_by_list(enriched, prefix="probate_backfill") if enriched else []
    for list_name, path, count in by_list:
        logger.info("Per-list CSV: %s (%d) -> %s", list_name, count, path)

    if args.upload_datasift and upload_ready:
        from datasift_uploader import upload_to_datasift
        from datasift_formatter import write_datasift_split_csvs
        csv_infos = write_datasift_split_csvs(upload_ready, list_name="")
        for info in csv_infos:
            logger.info("DataSift uploading %s ...", info["path"].name)
            asyncio.run(upload_to_datasift(
                # Explicit target list — upload_csv no longer derives one.
                info["path"], enrich=True, skip_trace=True,
                list_name="SiftStack",
            ))

    if args.notify_slack and config.SLACK_WEBHOOK_URL:
        from slack_notifier import _send_webhook
        header = (":rotating_light: NJ PROBATE BACKFILL — SOURCE FAILURE\n"
                  if SOURCE_ERRORS else "")
        lines = [
            header + f"*NJ Probate Local Backfill — Middlesex {args.mx_days_back}d + Somerset {args.som_days_back}d"
            + (f" + Ocean {args.ocean_days_back}d*" if args.include_ocean else "*"),
            _status_line("Middlesex", len(mx)),
            _status_line("Somerset", len(som)),
            (_status_line("Ocean", len(ocean)) if args.include_ocean
             else "  Ocean: skipped"),
            f"Enriched total: {len(enriched)}",
            f"CSV: {csv_path.name}",
        ]
        if held_back:
            lines.append(f":pause_button: Held for cleaning: {len(held_back)} (probate paused)")
        _send_webhook("\n".join(lines))

    # Non-zero when ANY source failed, even though others succeeded and a CSV
    # was written. The scheduled job keys its alert off this: a partial run
    # that quietly returns 0 is how a blocked scraper stays invisible.
    if SOURCE_ERRORS:
        logger.error("Sources failed: %s", ", ".join(sorted(SOURCE_ERRORS)))
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
