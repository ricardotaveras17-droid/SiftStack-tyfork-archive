"""Cross-cutting utilities for the deep_prospecting module.

Lives outside `phases/` and `sources/` so any layer can import without
creating cycles. Keep this module DEPENDENCY-LIGHT — stdlib + this
module's own models only. Anything heavier (Anthropic, Playwright) goes
in `llm.py` or specific source modules.

Contents:
  - Cost ceiling constants (the ceiling/target for the orchestrator).
  - `_safe()` — coroutine wrapper with retry/backoff/logging.
  - `slug()` — filesystem-safe identifier from an address/owner string.
  - Path helpers for outputs/{date}/{slug}/...
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

# ── Cost discipline ───────────────────────────────────────────────────
# Hard ceiling: orchestrator stamps abort + halts before a phase whose
# projected spend would push total over this.
#
# Slice 1 placeholder was $0.18 (CBC-only, no paid APIs). Slice 2 raised
# to $1.00 because Tracerfy at $0.10/heir × 3 heirs + Trestle at $0.015
# × ~10 phones lands an L3 case near $0.50 with headroom for outliers.
COST_CEILING_USD: float = 1.00
# Target: aspirational — runs above this aren't aborted, but the orchestrator
# can use it to choose cheaper variants (e.g., Haiku vs Sonnet for the
# DM-reasoning paragraph) when below target, switch to faster/cheaper
# strategies when above. Slice 2: $0.50 target per spec.
COST_TARGET_USD: float = 0.50


# ── _safe wrapper ─────────────────────────────────────────────────────

T = TypeVar("T")


async def _safe(
    coro: Awaitable[T],
    *,
    name: str,
    retries: int = 3,
    backoff: float = 2.0,
) -> T | None:
    """Catch + log + retry an awaitable. Returns None on final failure.

    Mirrors SiftStack's `_safe` ethos: a single hostile source must NEVER
    crash the run. Phases wrap external calls in `_safe()`; if a source
    is permanently down, the phase records `[MISSING]` in its output and
    moves on.

    Retry semantics (per spec):
      attempt 1 → fail → sleep `backoff` seconds
      attempt 2 → fail → sleep `backoff * 2`
      attempt 3 → fail → return None

    Args:
        coro: awaitable to invoke. NOTE: an awaitable can only be awaited
            once — for retry semantics with side-effecting calls, prefer
            `_safe_call(factory)` below where the factory can produce a
            fresh coroutine per attempt.
        name: short label for logs ("findagrave.search", "obit_search.firecrawl").
        retries: total attempts. 1 = no retry. Default 3.
        backoff: base seconds; doubles each failure (2, 4, 8, ...).
    """
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return await coro
        except Exception as e:
            last_err = e
            logger.warning("%s: attempt %d/%d failed: %s", name, attempt, retries, e)
            if attempt < retries:
                sleep_for = backoff * (2 ** (attempt - 1))
                await asyncio.sleep(sleep_for)
    logger.error("%s: all %d attempts failed (last: %s)", name, retries, last_err)
    return None


async def _safe_call(
    factory: Callable[[], Awaitable[T]],
    *,
    name: str,
    retries: int = 3,
    backoff: float = 2.0,
) -> T | None:
    """Variant of _safe for retry-with-side-effects.

    Many awaitables are single-shot (an HTTP call, a Playwright navigation).
    Retrying the SAME coroutine raises RuntimeError after the first await.
    `_safe_call` takes a factory so each attempt builds a fresh coroutine.

    Use this anywhere the operation has side effects you want to retry —
    network calls, Playwright actions, LLM completions.
    """
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return await factory()
        except Exception as e:
            last_err = e
            logger.warning("%s: attempt %d/%d failed: %s", name, attempt, retries, e)
            if attempt < retries:
                sleep_for = backoff * (2 ** (attempt - 1))
                await asyncio.sleep(sleep_for)
    logger.error("%s: all %d attempts failed (last: %s)", name, retries, last_err)
    return None


# ── Name parsing ──────────────────────────────────────────────────────

# Surname particles. A surname beginning at one of these tokens is multi-word:
# "Antoinette Del Duca" is Del Duca, not Duca. Taking the last whitespace token
# instead sent every CBC lookup on a two-word surname to a DIFFERENT family, and
# made the MOD-IV comma form ("DEL DUCA, ANTOINETTE M" -> "del duca") compare
# unequal to the caller form ("Antoinette Del Duca" -> "duca") for the same
# person, which silently suppressed the same-surname death signal.
_SURNAME_PARTICLES = frozenset({
    "de", "del", "della", "dell", "di", "da", "das", "dos", "du", "degli",
    "la", "le", "van", "von", "der", "den", "ter", "ten", "vander",
    "st", "saint", "san", "santa", "al", "bin", "ibn", "abu", "mac",
})

# Stripped before the surname is read, never treated as the surname itself.
_NAME_SUFFIXES = frozenset({
    "jr", "sr", "ii", "iii", "iv", "v", "md", "do", "phd", "esq", "cpa",
})

_NAME_WS = re.compile(r"\s+")


def split_name(name: str) -> tuple[str, str]:
    """Split a free-text personal name into (first, surname).

    Handles both orderings and multi-word surnames:

        "Antoinette Del Duca"       -> ("Antoinette", "Del Duca")
        "DEL DUCA, ANTOINETTE M"    -> ("ANTOINETTE", "DEL DUCA")
        "John Smith"                -> ("John", "Smith")
        "Maria De La Cruz"          -> ("Maria", "De La Cruz")
        "Antoinette Del Duca Jr"    -> ("Antoinette", "Del Duca")

    Case is preserved; callers lowercase when comparing. Returns ("", "") for
    input with no usable tokens, and ("", token) for a single token, so a
    caller can still tell "no name" from "surname only".

    The comma form is authoritative when present: everything before the comma
    is the surname, however many words it runs to. Without a comma, the surname
    starts at the first particle after the first token, or at the last token if
    there is no particle.
    """
    if not name:
        return ("", "")
    n = _NAME_WS.sub(" ", name.strip())
    if not n:
        return ("", "")

    if "," in n:
        surname, _, rest = n.partition(",")
        surname = surname.strip()
        rest_tokens = [t for t in rest.strip().split(" ") if t]
        while rest_tokens and rest_tokens[-1].lower().strip(".") in _NAME_SUFFIXES:
            rest_tokens.pop()
        return (rest_tokens[0] if rest_tokens else "", surname)

    tokens = [t for t in n.split(" ") if t]
    while len(tokens) > 2 and tokens[-1].lower().strip(".") in _NAME_SUFFIXES:
        tokens.pop()
    if not tokens:
        return ("", "")
    if len(tokens) == 1:
        return ("", tokens[0])

    # Never treat the first token as a particle - a given name is not a particle,
    # and "Del" alone as a first name would otherwise swallow the whole string.
    for i in range(1, len(tokens) - 1):
        if tokens[i].lower().strip(".") in _SURNAME_PARTICLES:
            return (tokens[0], " ".join(tokens[i:]))
    return (tokens[0], tokens[-1])


def last_name(name: str) -> str:
    """Lowercased surname, for comparison. See split_name."""
    return split_name(name)[1].lower()


# ── Filesystem helpers ────────────────────────────────────────────────

_SLUG_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slug(text: str, max_len: int = 60) -> str:
    """Make a filesystem-safe identifier from a free-text input.

    Used for `outputs/{date}/{slug}/...` paths. Lowercases, replaces
    non-alphanum with hyphens, collapses runs, trims. Caps length at
    max_len so a long owner+address combo doesn't blow past path limits.
    """
    if not text:
        return "unknown"
    s = _SLUG_NON_ALNUM.sub("-", text.lower()).strip("-")
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s or "unknown"


def output_dir_for_run(slug_text: str, *, base: Path | None = None) -> Path:
    """Return (and mkdir) the output dir for a single run.

    Layout: outputs/YYYY-MM-DD/{slug}/

    The date in the folder name is the operator's LOCAL date — UTC was
    confusing for west-coast operators ("I ran this at 5pm PDT but the
    folder is in tomorrow's UTC dir"). The audit timestamp on the
    ResearchPack itself (`now_utc()`) stays UTC for record-keeping
    consistency across machines.

    The base can be overridden via `PROSPECT_OUTPUT_DIR` env var; default
    is `outputs/` next to the deep_prospecting module.
    """
    if base is None:
        env_base = os.environ.get("PROSPECT_OUTPUT_DIR")
        base = Path(env_base) if env_base else Path(__file__).resolve().parent / "outputs"
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    out = base / today / slug(slug_text)
    out.mkdir(parents=True, exist_ok=True)
    return out


def now_utc() -> datetime:
    """Tz-aware UTC timestamp. Stamped on every ResearchPack."""
    return datetime.now(timezone.utc)
