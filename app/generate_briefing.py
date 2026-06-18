"""
generate_briefing.py
====================
ONE-SHOT script. Closes the loop between `market_brief.py collect` (which
writes market_data.md) and `bot.py` (which forwards final_briefing.md to
Telegram subscribers) by calling a local Ollama model to produce the
analyst-layer briefing.

Flow:
  1. Read SKILL.md (analyst persona + output rules) + any prompts/*.md
     (local-model guardrails + generation contract; the repair template is
     excluded from the system prompt and loaded only on demand).
  2. Read market_data.md and normalize it into market_data_structured.json via
     structure_market_data (parses the dashboards + best-effort fetches the
     linked articles). On any structuring failure, fall back to the raw blob.
  3. POST to {OLLAMA_URL}/api/chat with the system prompt as the system message
     and the structured JSON (+ raw market_data as supplemental) as the user
     message. Strip any <think> reasoning from the response.
  4. Validate the output (validate_briefing.validate_all). On FATAL errors, run
     up to MAX_REPAIR_ATTEMPTS repair passes that fix only those errors;
     warnings are logged but never block delivery.
  5. On success: write final_briefing.md = metadata header + model output.
  6. On ANY failure (Ollama down, model not pulled, timeout, empty body,
     missing input file, validation unfixable): still write final_briefing.md,
     but as a fallback containing the failure reason + the raw market_data.md so
     subscribers get the dashboards even when the analyst layer is unavailable.

Run modes:
  python generate_briefing.py    # only mode

Exit codes:
  0 — success path (final_briefing.md written from model output)
  1 — fallback path (final_briefing.md written, but model run failed)
      so Task Scheduler / cron reports the run as failed even though the
      downstream file watcher still has something to forward.
"""

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# Ollama connection config + admin notify live in .env (loaded by core.py).
# Keeping host/model/chat out of source means a remote Ollama endpoint or a
# different admin chat can be configured without touching this file.
from core import CHAT_ID, OLLAMA_MODEL, OLLAMA_URL, send_telegram

# Normalization (market_data.md -> structured JSON + fetched articles) and the
# post-generation output validator. Neither imports this module, so no cycle.
import structure_market_data
import validate_briefing


# ----------------------------------------------------------------------------
# Paths + timeout (the only constants that belong in source — everything else
# that could change between hosts lives in .env via core.py)
# ----------------------------------------------------------------------------
APP_DIR = Path(__file__).parent
SKILL_PATH = APP_DIR / "SKILL.md"
MARKET_DATA_PATH = APP_DIR / "market_data.md"
STRUCTURED_PATH = APP_DIR / "market_data_structured.json"
PROMPTS_DIR = APP_DIR / "prompts"
OUTPUT_PATH = APP_DIR / "final_briefing.md"

# The repair instruction template lives in prompts/ for discoverability but is
# loaded on demand (NOT folded into the generation system prompt). Exclude it by
# this exact filename so a normal run never injects repair instructions.
REPAIR_PROMPT_FILENAME = "20_repair_prompt.md"
REPAIR_PROMPT_PATH = PROMPTS_DIR / REPAIR_PROMPT_FILENAME

# Bounded repair: if the brief fails validation, ask the model to fix only the
# listed errors, at most this many times, before giving up to a fallback.
MAX_REPAIR_ATTEMPTS = 2
# Per-run heartbeat log (one JSON object per line). Under secrets/ so it's
# already gitignored and sits beside the databases.
RUN_LOG_PATH = APP_DIR.parent / "secrets" / "run_log.jsonl"

# 10-minute cap on the HTTP request. A 20B+ model generating a multi-section
# brief on CPU can legitimately run several minutes — a tight timeout would
# trigger spurious fallbacks. 10 min still catches a truly hung process.
OLLAMA_TIMEOUT_SEC = 600


# ----------------------------------------------------------------------------
# Input loading
# ----------------------------------------------------------------------------

def _load_system_prompt():
    """Build the LLM system prompt from SKILL.md plus optional modular prompt files.

    SKILL.md remains the canonical analyst persona and output contract and is
    intentionally left unchanged for Claude cowork compatibility. Optional
    prompts/*.md files may add local-model guardrails, generation constraints, or
    repair instructions. Files are sorted by name so prompt order is deterministic.

    The repair template (prompts/20_repair_prompt.md) is deliberately EXCLUDED
    here — it is loaded separately by _load_repair_prompt() only when a brief fails
    validation, so a normal generation never injects repair instructions.

    Missing SKILL.md raises FileNotFoundError (main() turns it into a fallback).
    Missing prompts/ is allowed.

    Mechanical constraints (Telegram length, required section headers, watchlist
    block parsing, split markers, trade-price validation) are enforced in Python
    after generation by validate_briefing, not trusted to the model alone."""
    if not SKILL_PATH.exists():
        raise FileNotFoundError(
            f"SKILL.md not found at {SKILL_PATH}; cannot build system prompt"
        )

    parts = [SKILL_PATH.read_text(encoding="utf-8")]

    if PROMPTS_DIR.is_dir():
        for md in sorted(PROMPTS_DIR.glob("*.md")):
            if md.name == REPAIR_PROMPT_FILENAME:
                continue  # loaded on demand by _load_repair_prompt(), not here
            parts.append(md.read_text(encoding="utf-8"))

    return "\n\n".join(parts)


def _load_repair_prompt():
    """Read the repair instruction template (prompts/20_repair_prompt.md)."""
    if not REPAIR_PROMPT_PATH.exists():
        raise FileNotFoundError(
            f"repair prompt not found at {REPAIR_PROMPT_PATH}"
        )
    return REPAIR_PROMPT_PATH.read_text(encoding="utf-8")


def _load_market_data():
    """Read market_data.md. Returns the file contents as a string.
    Raises FileNotFoundError if the file is missing — main() converts that
    into a fallback briefing whose body explains the user has to run
    `python market_brief.py collect` first."""
    if not MARKET_DATA_PATH.exists():
        raise FileNotFoundError(
            "market_data.md not found; run `python market_brief.py collect` first"
        )
    return MARKET_DATA_PATH.read_text(encoding="utf-8")


# ----------------------------------------------------------------------------
# Ollama call
# ----------------------------------------------------------------------------

class OllamaError(RuntimeError):
    """Raised when the Ollama call fails for any reason. The `args[0]`
    string is a human-readable reason that gets written verbatim into
    the fallback briefing's Reason line."""


def _call_ollama(system, user, model, url):
    """POST to {url}/api/chat (non-streaming) and return a tuple of
    (assistant message content, actual model name that served the request).

    The actual model name comes from the response's `model` field, which
    Ollama populates with the resolved tag it actually ran (e.g. a request
    for `llama3.1` comes back as `llama3.1:8b`). Falls back to the requested
    `model` if the field is missing. Raises OllamaError with a specific
    reason string on every failure mode the script knows about:

      - connection refused / DNS / network → "Ollama not reachable at <url>"
      - HTTP 404 with model-not-found body → "Model 'X' not loaded ..."
      - request timeout                    → "Ollama request timed out after Ns"
      - any other non-2xx                  → "Ollama returned HTTP <status>: <body>"
      - 2xx with no message.content        → "Ollama returned an empty response"
    """
    endpoint = url.rstrip("/") + "/api/chat"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        "stream": False,
    }

    try:
        resp = requests.post(endpoint, json=payload,
                             timeout=OLLAMA_TIMEOUT_SEC)
    except requests.exceptions.ConnectionError as exc:
        raise OllamaError(
            f"Ollama not reachable at {url} — is `ollama serve` running?"
        ) from exc
    except requests.exceptions.Timeout as exc:
        raise OllamaError(
            f"Ollama request timed out after {OLLAMA_TIMEOUT_SEC}s"
        ) from exc
    except requests.exceptions.RequestException as e:
        raise OllamaError(
            f"Ollama request failed: {type(e).__name__}: {e}"
        ) from e

    # Ollama returns 404 with a JSON body when the model isn't loaded.
    # Surface that exact wording rather than a generic "HTTP 404" so the
    # user knows to `ollama pull <model>`.
    if resp.status_code == 404:
        body = (resp.text or "").strip()
        if "model" in body.lower() and "not found" in body.lower():
            raise OllamaError(
                f"Model '{model}' not loaded — run `ollama pull {model}` first "
                f"(Ollama said: {body[:200]})"
            )
        raise OllamaError(f"Ollama returned HTTP 404: {body[:200]}")

    if not resp.ok:
        raise OllamaError(
            f"Ollama returned HTTP {resp.status_code}: {(resp.text or '')[:200]}"
        )

    try:
        data = resp.json()
    except ValueError as exc:
        raise OllamaError("Ollama returned a non-JSON response") from exc

    content = (data.get("message") or {}).get("content") or ""
    content = content.strip()
    if not content:
        raise OllamaError("Ollama returned an empty response")

    # `model` in the response is the resolved tag Ollama actually ran; fall
    # back to the requested name if the server didn't echo it.
    actual_model = data.get("model") or model

    return content, actual_model


# ----------------------------------------------------------------------------
# Local-model helpers: reasoning stripping, prompt assembly, repair
# ----------------------------------------------------------------------------

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_TAG_RE = re.compile(r"</?think>", re.IGNORECASE)


def _strip_reasoning(text):
    """Remove DeepSeek-R1-style <think>…</think> reasoning so it never reaches
    the briefing. Drops closed blocks, an unclosed leading block (everything up
    to the first </think>), and any stray tags."""
    if not text:
        return text
    text = _THINK_BLOCK_RE.sub("", text)
    # Unclosed: a leading <think> with no close — keep only what's after </think>.
    if "</think>" in text.lower():
        idx = text.lower().rfind("</think>")
        text = text[idx + len("</think>"):]
    text = _THINK_TAG_RE.sub("", text)
    return text.strip()


# Forceful output contract appended to the generation user message AND embedded
# in the repair template (prompts/20_repair_prompt.md). Kept in sync in both
# places so weak local models get the same blunt instruction either way.
OUTPUT_CONTRACT = (
    "Return ONLY the final_briefing.md content.\n"
    "Do not explain.\n"
    "Do not wrap the answer in a code block.\n"
    "Do not use markdown fences.\n"
    "The first line of the response must be exactly:\n"
    "🧭 BOTTOM LINE\n\n"
    "The output must contain these exact section headers:\n"
    "🧭 BOTTOM LINE\n"
    "🔥 EXECUTIVE MARKET READ\n"
    "📊 MACRO DASHBOARD\n"
    "👀 WATCHLIST DASHBOARD\n"
    "🎯 TRADE IDEAS\n"
    "📰 HIGHEST RELEVANCE STORIES\n"
    "📈 POTENTIAL NEW WATCHLIST CANDIDATES\n"
    "⚠️ KEY RISKS\n\n"
    "If you cannot produce a valid section, include the section header anyway "
    "and write:\n"
    "Data unavailable from supplied input."
)

# A response that is fully wrapped in a single ``` … ``` fence (first and last
# lines are the only fences). Match by structure, not regex, so internal fences
# inside the brief are never disturbed.
_FENCE_RE = re.compile(r"^\s*```")


def _strip_wrapping_code_fence(text):
    """If the ENTIRE model output is wrapped in one outer markdown code fence,
    remove only that outer fence and keep the internal content. Leaves the text
    unchanged when it isn't fully fence-wrapped (e.g. only an internal fence, or
    no fence at all). Run after _strip_reasoning, before validate_all."""
    if not text:
        return text
    stripped = text.strip()
    lines = stripped.splitlines()
    if (len(lines) >= 2
            and _FENCE_RE.match(lines[0]) and lines[-1].strip().startswith("```")
            and sum(1 for ln in lines if ln.strip().startswith("```")) == 2):
        return "\n".join(lines[1:-1]).strip()
    return stripped


def _build_user_message(structured_json_str, raw_market_data):
    """Primary = structured JSON; supplemental = raw market_data.md; plus the
    explicit source-of-truth rules and the forceful output contract."""
    return (
        "PRIMARY DATA — market_data_structured.json (the source of truth):\n"
        f"{structured_json_str}\n\n"
        "SUPPLEMENTAL — raw market_data.md (use ONLY to resolve ambiguity):\n"
        f"{raw_market_data}\n\n"
        "Rules: Use the structured JSON as the primary source of truth. Use the "
        "raw market_data.md only to resolve ambiguity. Do not invent details for "
        "any news story whose fetch_status is not 'success'. Do not cite or "
        "analyze article content unless its text, summary, title, or catalyst is "
        "present in the structured JSON.\n\n"
        f"{OUTPUT_CONTRACT}"
    )


def _repair(invalid_brief, errors, structured_json_str, raw_market_data,
            system_prompt, model, url):
    """Ask the model to fix ONLY the listed validation errors. Returns
    (repaired_text, actual_model). Raises OllamaError if the call fails."""
    template = _load_repair_prompt()
    user = template.format(
        errors="\n".join(f"- {e}" for e in errors),
        structured_json=structured_json_str or "(structured JSON unavailable)",
        market_data=raw_market_data,
        invalid_briefing=invalid_brief,
    )
    content, actual_model = _call_ollama(
        system=system_prompt, user=user, model=model, url=url)
    return _strip_reasoning(content), actual_model


# ----------------------------------------------------------------------------
# Output writers
# ----------------------------------------------------------------------------

def _metadata_header(model, status):
    """One-line italic header prepended to every final_briefing.md (both the
    success and fallback paths). Telegram-Markdown safe — no `#`, no `---`,
    no triple backticks — so bot.py forwards it without parser rejection.
    The leading underscore renders as italic in Telegram's legacy Markdown
    parse mode."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"_Generated {now} · model: {model} · status: {status}_"


def _success_text(model, body):
    """Full success briefing as a string: metadata header (status=ok) +
    the model's response. Shared by _write_success (writes the daily file)
    and build_briefing_text (returns it for an in-memory /refresh)."""
    return f"{_metadata_header(model, 'ok')}\n\n{body.strip()}\n"


def _fallback_text(reason, model, market_data):
    """Full fallback briefing as a string. Shape:

        <metadata header, status=fallback>

        ⚠️ *Auto-briefing generation failed*

        Reason: <reason>

        Raw market data follows:

        <full market_data content, or a note that it couldn't be loaded>

    Keeps the same Telegram-Markdown conventions as SKILL.md so bot.py can
    forward this unchanged. We deliberately surface the failure REASON
    verbatim — the user wanted to know WHY when no model is loaded. Shared by
    _write_fallback (daily file) and build_briefing_text (in-memory)."""
    lines = [
        _metadata_header(model, "fallback"),
        "",
        "⚠️ Auto-briefing generation failed",
        "",
        f"Reason: {reason}",
        "",
        "Raw market data follows:",
        "",
    ]
    if market_data:
        lines.append(market_data.rstrip())
    else:
        lines.append(
            "_(market_data.md was unavailable — no raw data to include)_")
    return "\n".join(lines) + "\n"


def _write_success(model, body):
    """Prepend the metadata header (status=ok) to the model's response and
    write final_briefing.md."""
    OUTPUT_PATH.write_text(_success_text(model, body), encoding="utf-8")


def _write_fallback(reason, model, market_data):
    """Write final_briefing.md as a fallback notice (see _fallback_text)."""
    OUTPUT_PATH.write_text(
        _fallback_text(reason, model, market_data), encoding="utf-8")


def _produce_briefing(market_data, model, url, structured_out_path=None):
    """Shared generation core for main() (daily file run) and build_briefing_text
    (/refresh, in-memory). Returns (full_text, ok, actual_model, note):

      1. Normalize market_data.md -> structured JSON (+ best-effort article fetch).
         On any failure, fall back to feeding the raw market_data to the model.
      2. Call the model, strip <think> reasoning, and validate the output.
      3. If there are FATAL validation errors, run up to MAX_REPAIR_ATTEMPTS repair
         passes that fix only those errors. Warnings are logged, never blocking.
      4. On a clean (or warning-only) result return the success text; otherwise
         return the fallback text (raw data embedded) with the reason in `note`.

    OllamaError is caught here and converted into a fallback. FileNotFoundError
    (missing SKILL.md / repair prompt) propagates to the caller, which handles it.
    `structured_out_path`, when given, is where the structured JSON is written
    (daily run only — /refresh passes None to stay file-free)."""
    system_prompt = _load_system_prompt()

    # Step A: structured normalization (best-effort; raw fallback on failure).
    structured_data = None
    structured_str = None
    try:
        structured_data = structure_market_data.build_structured_data(
            market_data, fetch=True)
        s_errs = structure_market_data.validate_structured(structured_data)
        if s_errs:
            print("[generate_briefing] structured-data warnings: "
                  + "; ".join(s_errs))
        structured_str = json.dumps(structured_data, ensure_ascii=False)
        if structured_out_path is not None:
            structure_market_data.write_structured_data(
                structured_out_path, structured_data)
    except Exception as e:  # pylint: disable=broad-exception-caught
        print(f"[generate_briefing] structured build failed "
              f"({type(e).__name__}: {e}); using raw market_data")

    user_msg = (_build_user_message(structured_str, market_data)
                if structured_str else market_data)

    # Step B: first generation.
    try:
        content, actual_model = _call_ollama(
            system=system_prompt, user=user_msg, model=model, url=url)
    except OllamaError as e:
        return _fallback_text(str(e), model, market_data), False, model, str(e)
    content = _strip_wrapping_code_fence(_strip_reasoning(content))

    # Step C: validate (format + quality contract) + bounded repair.
    fatal, warnings = validate_briefing.validate_all(
        content, market_data, structured_data)
    for w in warnings:
        print(f"[generate_briefing] validation warning: {w}")

    attempt = 0
    while fatal and attempt < MAX_REPAIR_ATTEMPTS:
        attempt += 1
        print(f"[generate_briefing] {len(fatal)} fatal validation error(s); "
              f"repair attempt {attempt}/{MAX_REPAIR_ATTEMPTS}:")
        for e in fatal:
            print(f"    - {e}")
        try:
            content, actual_model = _repair(
                content, fatal, structured_str, market_data,
                system_prompt, model, url)
        except OllamaError as e:
            print(f"[generate_briefing] repair call failed: {e}")
            break
        content = _strip_wrapping_code_fence(content)
        fatal, warnings = validate_briefing.validate_all(
            content, market_data, structured_data)
        for w in warnings:
            print(f"[generate_briefing] validation warning: {w}")

    if fatal:
        reason = "validation failed after repair: " + "; ".join(fatal[:6])
        return _fallback_text(reason, model, market_data), False, model, reason

    return _success_text(actual_model, content), True, actual_model, None


def build_briefing_text(market_data, model=None, url=None):
    """Generate a briefing from `market_data` IN MEMORY and return
    (text, ok) — no file writes, no heartbeat, no scorecard ingest, no admin
    alerts. Used by bot.py's /refresh so a per-user, intraday regeneration
    never touches the shared final_briefing.md (which would trip the file
    watcher and broadcast to everyone) nor pollutes trades.db / run_log.jsonl
    with off-schedule ideas — those side effects stay in main(), the daily run.

    Runs the same structured-data + validation + repair pipeline as the daily
    job (structured JSON is built in-memory and not persisted). `ok` is True when
    the model produced a valid briefing, False when we fell back to embedding the
    raw dashboards (Ollama down, model missing, timeout, validation unfixable)."""
    model = model or OLLAMA_MODEL
    url = url or OLLAMA_URL
    try:
        text, ok, _actual, _note = _produce_briefing(
            market_data, model, url, structured_out_path=None)
        return text, ok
    except Exception as e:  # pylint: disable=broad-exception-caught
        return _fallback_text(
            f"unexpected {type(e).__name__}: {e}", model, market_data), False


# ----------------------------------------------------------------------------
# Post-run health hook (#4) + trade-idea ingest (#1)
# ----------------------------------------------------------------------------

def _write_heartbeat(record):
    """Append one JSON line to secrets/run_log.jsonl. Best-effort — a logging
    failure must never affect the run's exit status."""
    try:
        RUN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = {"ts": datetime.now(timezone.utc).isoformat(), **record}
        with open(RUN_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
    except OSError as e:
        print(f"[generate_briefing] heartbeat write failed: {type(e).__name__}: {e}")


def _alert_admin(text):
    """DM the admin (env CHAT_ID). Guarded so a Telegram failure can't break
    the run."""
    try:
        if CHAT_ID:
            send_telegram(text, chat_id=CHAT_ID)
    except Exception as e:  # pylint: disable=broad-exception-caught
        print(f"[generate_briefing] admin alert failed: {type(e).__name__}: {e}")


def _post_success(model, elapsed):
    """After a successful generation: validate trade levels, log a heartbeat,
    alert the admin on any hallucinated level, and ingest the day's ideas into
    trades_db. Fully guarded — never raises (it runs after the brief is already
    written, so it must not change the exit contract)."""
    n_ideas = n_flagged = 0
    try:
        import validate_briefing
        briefing = OUTPUT_PATH.read_text(encoding="utf-8")
        market = MARKET_DATA_PATH.read_text(encoding="utf-8")
        results = validate_briefing.validate_trades(
            briefing, validate_briefing.parse_key_levels(market)
        )
        n_ideas = len(results)
        n_flagged = sum(
            1 for r in results for _l, _v, matched in r["checks"] if matched is None
        )
    except Exception as e:  # pylint: disable=broad-exception-caught
        print(f"[generate_briefing] validation skipped: {type(e).__name__}: {e}")

    _write_heartbeat({
        "status": "ok", "model": model, "elapsed_s": round(elapsed, 1),
        "n_ideas": n_ideas, "n_flagged": n_flagged,
    })

    if n_flagged:
        _alert_admin(
            f"⚠️ Daily brief: {n_flagged} trade level(s) NOT grounded in Key "
            f"Levels (model {model}). Run validate_briefing.py to inspect."
        )

    try:
        import scorecard
        added = scorecard.ingest()
        print(f"[generate_briefing] logged {added} trade idea(s) to trades_db")
    except Exception as e:  # pylint: disable=broad-exception-caught
        print(f"[generate_briefing] idea ingest skipped: {type(e).__name__}: {e}")


def _post_fallback(reason, model, elapsed):
    """After a fallback: heartbeat + admin alert so a silent failure is seen."""
    _write_heartbeat({
        "status": "fallback", "model": model, "elapsed_s": round(elapsed, 1),
        "reason": reason[:200],
    })
    _alert_admin(f"⚠️ Daily brief FELL BACK (model {model}): {reason[:300]}")


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------

def main():
    """Run the daily pipeline: load market_data.md, normalize it to structured
    JSON (+ fetch articles), call Ollama, validate + repair, and write
    final_briefing.md. Returns an exit code (0 success, 1 fallback)."""
    started = time.monotonic()
    market_data = None
    try:
        # Step 1: load market_data.md (the upstream dashboards + headlines).
        market_data = _load_market_data()
        # Step 2+: structure, generate, validate, repair (shared with /refresh).
        print(
            f"[generate_briefing] calling {OLLAMA_URL} with model "
            f"{OLLAMA_MODEL} (timeout {OLLAMA_TIMEOUT_SEC}s)"
        )
        text, ok, actual_model, note = _produce_briefing(
            market_data, OLLAMA_MODEL, OLLAMA_URL,
            structured_out_path=STRUCTURED_PATH)
    except FileNotFoundError as e:
        # Missing SKILL.md / repair prompt — operator-readable reason.
        reason = str(e)
        print(f"[generate_briefing] FAILED: {reason}")
        _write_fallback(reason, OLLAMA_MODEL, market_data)
        _post_fallback(reason, OLLAMA_MODEL, time.monotonic() - started)
        return 1
    except Exception as e:  # pylint: disable=broad-exception-caught
        # The whole point of this script is that bot.py ALWAYS has a file to
        # forward. An UNEXPECTED error must still yield a fallback briefing,
        # never a bare crash that leaves final_briefing.md untouched.
        reason = f"unexpected {type(e).__name__}: {e}"
        print(f"[generate_briefing] FAILED (unexpected): {reason}")
        _write_fallback(reason, OLLAMA_MODEL, market_data)
        _post_fallback(reason, OLLAMA_MODEL, time.monotonic() - started)
        return 1

    # _produce_briefing always returns a complete file (success or fallback).
    OUTPUT_PATH.write_text(text, encoding="utf-8")
    elapsed = time.monotonic() - started
    if ok:
        print(f"[generate_briefing] wrote {OUTPUT_PATH} ({len(text)} chars)")
        _post_success(actual_model, elapsed)
        return 0
    print(f"[generate_briefing] FALLBACK written: {note}")
    _post_fallback(note or "fallback", OLLAMA_MODEL, elapsed)
    return 1


if __name__ == "__main__":
    sys.exit(main())
