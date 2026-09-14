"""Thin LLM abstraction layer — routes calls to Anthropic or Ollama.

Supports two backends:
  - anthropic: Claude Haiku via Anthropic API (production, paid)
  - ollama: Local model via Ollama OpenAI-compatible API (development, free)

Backend selection: LLM_BACKEND env var or config.LLM_BACKEND.
"""

import json
import logging
import re

import config as cfg

logger = logging.getLogger(__name__)

# ── Credential preflight ─────────────────────────────────────────────
# A rotated key is present, well-formed, and rejected. Shape checks pass and prove nothing —
# only a live call detects it. Without this, an auth failure arrives as a generic warning and
# a None return, which is indistinguishable from "the model found nothing". That is how a dead
# key went unnoticed across two projects (see 2026-08-18).

_AUTH_DEAD = {"dead": False}   # set once an auth error is seen; later calls fail fast
                               # (a dict cell, so no `global` declaration is needed —
                               #  `global` after a read is a SyntaxError ast.parse misses)


def preflight(verbose: bool = True) -> tuple[bool, str]:
    """Prove the configured backend's credential works. -> (ok, actionable detail).

    Call this at the start of any unattended run. Only checks the backend actually in use.
    """
    backend = getattr(cfg, "LLM_BACKEND", "anthropic")
    if backend != "anthropic":
        msg = f"backend is {backend!r}, not anthropic — no Anthropic credential needed"
        if verbose:
            logger.info("preflight: %s", msg)
        return True, msg

    key = cfg.ANTHROPIC_API_KEY
    model = getattr(cfg, "LLM_MODEL", "claude-haiku-4-5-20251001")

    if not key:
        return False, ("ANTHROPIC_API_KEY is NOT SET. Add it to this project's gitignored "
                       ".env, or run `ant auth login`.")
    if not key.startswith("sk-ant-"):
        return False, ("ANTHROPIC_API_KEY is set but malformed (no 'sk-ant-' prefix). "
                       "Check for a truncated paste or stray quotes.")

    import anthropic
    try:
        anthropic.Anthropic(api_key=key).messages.create(
            model=model, max_tokens=4,
            messages=[{"role": "user", "content": "ok"}])
    except anthropic.AuthenticationError:
        return False, (f"ANTHROPIC_API_KEY is present and well-formed but was REJECTED (401) "
                       f"by the API — it has expired or been rotated. Put a current key in "
                       f"this project's gitignored .env. NOTE: this key is shared with the "
                       f"other SiftStack project and sms-loop; if it was rotated, update all "
                       f"three.")
    except anthropic.NotFoundError:
        return False, (f"credential works, but model {model!r} was not found. Check LLM_MODEL "
                       f"in .env against the current model list.")
    except Exception as e:
        return False, f"preflight could not complete: {type(e).__name__}: {e}"

    msg = f"credential accepted (backend=anthropic, model={model})"
    if verbose:
        logger.info("preflight: %s", msg)
    return True, msg


def require_preflight() -> None:
    """Preflight or exit. For scripts that should not start work on a dead credential."""
    ok, detail = preflight()
    if not ok:
        raise SystemExit(f"PREFLIGHT FAILED — {detail}")


# ── Backend dispatch ──────────────────────────────────────────────────


def chat_json(
    prompt: str,
    system: str = "",
    max_tokens: int = 1024,
    api_key: str | None = None,
) -> dict | None:
    """Send prompt, get parsed JSON response. Routes to configured backend.

    Returns parsed dict on success, None on failure.
    """
    backend = getattr(cfg, "LLM_BACKEND", "anthropic")
    if backend == "ollama":
        return _chat_ollama(prompt, system, max_tokens)
    elif backend == "openrouter":
        return _chat_openrouter(prompt, system, max_tokens)
    else:
        return _chat_anthropic(prompt, system, max_tokens, api_key)


def chat_json_async(
    prompt: str,
    system: str = "",
    max_tokens: int = 1024,
    api_key: str | None = None,
):
    """Async version — returns a coroutine. For llm_parser.py compatibility."""
    import asyncio
    backend = getattr(cfg, "LLM_BACKEND", "anthropic")
    if backend == "ollama":
        return _chat_ollama_async(prompt, system, max_tokens)
    elif backend == "openrouter":
        return _chat_openrouter_async(prompt, system, max_tokens)
    else:
        return _chat_anthropic_async(prompt, system, max_tokens, api_key)


# ── Anthropic backend ────────────────────────────────────────────────


def _chat_anthropic(
    prompt: str, system: str, max_tokens: int, api_key: str | None,
) -> dict | None:
    """Call Claude Haiku via Anthropic API (sync)."""
    import anthropic

    key = api_key or cfg.ANTHROPIC_API_KEY
    if _AUTH_DEAD["dead"]:
        return None          # key already proven dead this run; do not retry
    if not key:
        logger.warning("No Anthropic API key — skipping LLM call")
        return None

    model = getattr(cfg, "LLM_MODEL", "claude-haiku-4-5-20251001")
    try:
        client = anthropic.Anthropic(api_key=key)
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        result_text = response.content[0].text.strip()
        return _parse_json(result_text)
    except anthropic.AuthenticationError as e:
        # Not a transient failure — every later call will fail the same way.
        if not _AUTH_DEAD['dead']:
            _AUTH_DEAD['dead'] = True
            logger.error(
                "ANTHROPIC KEY REJECTED (401) — every LLM call in this run will "
                "return nothing. The key is set but expired or rotated. Fix it in "
                "this project's .env (shared with the other SiftStack project and "
                "sms-loop). Underlying error: %s", e)
        return None
    except Exception as e:
        logger.warning("Anthropic LLM call failed: %s", e)
        return None


async def _chat_anthropic_async(
    prompt: str, system: str, max_tokens: int, api_key: str | None,
) -> dict | None:
    """Call Claude Haiku via Anthropic API (async)."""
    import anthropic

    key = api_key or cfg.ANTHROPIC_API_KEY
    if _AUTH_DEAD["dead"]:
        return None          # key already proven dead this run; do not retry
    if not key:
        logger.warning("No Anthropic API key — skipping LLM call")
        return None

    model = getattr(cfg, "LLM_MODEL", "claude-haiku-4-5-20251001")
    try:
        client = anthropic.AsyncAnthropic(api_key=key)
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        result_text = response.content[0].text.strip()
        return _parse_json(result_text)
    except anthropic.AuthenticationError as e:
        # Not a transient failure — every later call will fail the same way.
        if not _AUTH_DEAD['dead']:
            _AUTH_DEAD['dead'] = True
            logger.error(
                "ANTHROPIC KEY REJECTED (401) — every LLM call in this run will "
                "return nothing. The key is set but expired or rotated. Fix it in "
                "this project's .env (shared with the other SiftStack project and "
                "sms-loop). Underlying error: %s", e)
        return None
    except Exception as e:
        logger.warning("Anthropic async LLM call failed: %s", e)
        return None


# ── Ollama backend ───────────────────────────────────────────────────


def _chat_ollama(
    prompt: str, system: str, max_tokens: int,
) -> dict | None:
    """Call local Ollama model via OpenAI-compatible API (sync)."""
    from openai import OpenAI

    base_url = getattr(cfg, "OLLAMA_BASE_URL", "http://localhost:11434/v1/")
    model = getattr(cfg, "OLLAMA_MODEL", "qwen2.5:7b")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        client = OpenAI(base_url=base_url, api_key="ollama")
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.1,
        )
        result_text = response.choices[0].message.content.strip()
        parsed = _parse_json(result_text)
        if parsed is None:
            # Retry once with explicit JSON instruction appended
            logger.debug("Ollama JSON parse failed, retrying with hint")
            retry_prompt = prompt + "\n\nIMPORTANT: Return ONLY valid JSON. No markdown, no explanation."
            response = client.chat.completions.create(
                model=model,
                messages=[
                    *(([{"role": "system", "content": system}] if system else [])),
                    {"role": "user", "content": retry_prompt},
                ],
                max_tokens=max_tokens,
                temperature=0.0,
            )
            result_text = response.choices[0].message.content.strip()
            parsed = _parse_json(result_text)
        return parsed
    except Exception as e:
        logger.warning("Ollama LLM call failed: %s", e)
        return None


async def _chat_ollama_async(
    prompt: str, system: str, max_tokens: int,
) -> dict | None:
    """Call local Ollama model via OpenAI-compatible API (async)."""
    from openai import AsyncOpenAI

    base_url = getattr(cfg, "OLLAMA_BASE_URL", "http://localhost:11434/v1/")
    model = getattr(cfg, "OLLAMA_MODEL", "qwen2.5:7b")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        client = AsyncOpenAI(base_url=base_url, api_key="ollama")
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.1,
        )
        result_text = response.choices[0].message.content.strip()
        parsed = _parse_json(result_text)
        if parsed is None:
            logger.debug("Ollama async JSON parse failed, retrying with hint")
            retry_prompt = prompt + "\n\nIMPORTANT: Return ONLY valid JSON. No markdown, no explanation."
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    *(([{"role": "system", "content": system}] if system else [])),
                    {"role": "user", "content": retry_prompt},
                ],
                max_tokens=max_tokens,
                temperature=0.0,
            )
            result_text = response.choices[0].message.content.strip()
            parsed = _parse_json(result_text)
        return parsed
    except Exception as e:
        logger.warning("Ollama async LLM call failed: %s", e)
        return None


# ── OpenRouter backend ──────────────────────────────────────────────


def _chat_openrouter(
    prompt: str, system: str, max_tokens: int,
) -> dict | None:
    """Call OpenRouter model via OpenAI-compatible API (sync)."""
    from openai import OpenAI

    api_key = getattr(cfg, "OPENROUTER_API_KEY", "")
    if not api_key:
        logger.warning("No OpenRouter API key — skipping LLM call")
        return None

    base_url = getattr(cfg, "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    model = getattr(cfg, "OPENROUTER_MODEL", "qwen/qwen-2.5-72b-instruct")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        client = OpenAI(base_url=base_url, api_key=api_key)
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.1,
        )
        result_text = response.choices[0].message.content.strip()
        parsed = _parse_json(result_text)
        if parsed is None:
            logger.debug("OpenRouter JSON parse failed, retrying with hint")
            retry_prompt = prompt + "\n\nIMPORTANT: Return ONLY valid JSON. No markdown, no explanation."
            response = client.chat.completions.create(
                model=model,
                messages=[
                    *(([{"role": "system", "content": system}] if system else [])),
                    {"role": "user", "content": retry_prompt},
                ],
                max_tokens=max_tokens,
                temperature=0.0,
            )
            result_text = response.choices[0].message.content.strip()
            parsed = _parse_json(result_text)
        return parsed
    except Exception as e:
        logger.warning("OpenRouter LLM call failed: %s", e)
        return None


async def _chat_openrouter_async(
    prompt: str, system: str, max_tokens: int,
) -> dict | None:
    """Call OpenRouter model via OpenAI-compatible API (async)."""
    from openai import AsyncOpenAI

    api_key = getattr(cfg, "OPENROUTER_API_KEY", "")
    if not api_key:
        logger.warning("No OpenRouter API key — skipping LLM call")
        return None

    base_url = getattr(cfg, "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    model = getattr(cfg, "OPENROUTER_MODEL", "qwen/qwen-2.5-72b-instruct")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.1,
        )
        result_text = response.choices[0].message.content.strip()
        parsed = _parse_json(result_text)
        if parsed is None:
            logger.debug("OpenRouter async JSON parse failed, retrying with hint")
            retry_prompt = prompt + "\n\nIMPORTANT: Return ONLY valid JSON. No markdown, no explanation."
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    *(([{"role": "system", "content": system}] if system else [])),
                    {"role": "user", "content": retry_prompt},
                ],
                max_tokens=max_tokens,
                temperature=0.0,
            )
            result_text = response.choices[0].message.content.strip()
            parsed = _parse_json(result_text)
        return parsed
    except Exception as e:
        logger.warning("OpenRouter async LLM call failed: %s", e)
        return None


# ── JSON parsing ─────────────────────────────────────────────────────


def _parse_json(text: str) -> dict | None:
    """Parse JSON from LLM response, stripping markdown fences."""
    # Strip markdown code fences
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()

    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
        if isinstance(result, list):
            return {"items": result}  # Wrap list in dict for consistency
        return None
    except json.JSONDecodeError:
        # Try to find JSON object in the text
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        logger.debug("Failed to parse JSON from LLM response: %.200s", text)
        return None
