"""
generate_briefing.py
====================
ONE-SHOT script. Closes the loop between `market_brief.py collect` (which
writes market_data.md) and `bot.py` (which forwards final_briefing.md to
Telegram subscribers) by calling a local Ollama model to produce the
analyst-layer briefing.

Flow:
  1. Read SKILL.md (analyst persona + output rules) + any prompts/*.md.
  2. Read market_data.md (the dashboards + headlines produced upstream).
  3. POST to {OLLAMA_URL}/api/chat with the SKILL.md content as the system
     message and the market_data.md content as the user message.
  4. On success: write final_briefing.md = metadata header + model output.
  5. On ANY failure (Ollama down, model not pulled, timeout, empty body,
     missing input file): still write final_briefing.md, but as a fallback
     containing the failure reason + the raw market_data.md so subscribers
     get the dashboards even when the analyst layer is unavailable.

Run modes:
  python generate_briefing.py    # only mode

Exit codes:
  0 — success path (final_briefing.md written from model output)
  1 — fallback path (final_briefing.md written, but model run failed)
      so Task Scheduler / cron reports the run as failed even though the
      downstream file watcher still has something to forward.
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# Ollama connection config + admin notify live in .env (loaded by core.py).
# Keeping host/model/chat out of source means a remote Ollama endpoint or a
# different admin chat can be configured without touching this file.
from core import CHAT_ID, OLLAMA_MODEL, OLLAMA_URL, send_telegram


# ----------------------------------------------------------------------------
# Paths + timeout (the only constants that belong in source — everything else
# that could change between hosts lives in .env via core.py)
# ----------------------------------------------------------------------------
APP_DIR = Path(__file__).parent
SKILL_PATH = APP_DIR / "SKILL.md"
MARKET_DATA_PATH = APP_DIR / "market_data.md"
PROMPTS_DIR = APP_DIR / "prompts"
OUTPUT_PATH = APP_DIR / "final_briefing.md"
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
    """Concatenate SKILL.md + any prompts/*.md into the system message.

    SKILL.md is the analyst persona + output format rules — it's mandatory
    and a missing file raises FileNotFoundError (which main() turns into a
    fallback briefing). prompts/ is optional: drop additional .md files in
    there to extend the system prompt without code changes. Files are sorted
    by name so the concatenation order is deterministic across runs."""
    if not SKILL_PATH.exists():
        raise FileNotFoundError(
            f"SKILL.md not found at {SKILL_PATH}; cannot build system prompt"
        )

    parts = [SKILL_PATH.read_text(encoding="utf-8")]

    if PROMPTS_DIR.is_dir():
        for md in sorted(PROMPTS_DIR.glob("*.md")):
            parts.append(md.read_text(encoding="utf-8"))

    return "\n\n".join(parts)


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


def build_briefing_text(market_data, model=None, url=None):
    """Generate a briefing from `market_data` IN MEMORY and return
    (text, ok) — no file writes, no heartbeat, no scorecard ingest, no admin
    alerts. Used by bot.py's /refresh so a per-user, intraday regeneration
    never touches the shared final_briefing.md (which would trip the file
    watcher and broadcast to everyone) nor pollutes trades.db / run_log.jsonl
    with off-schedule ideas — those side effects stay in main(), the daily run.

    `ok` is True when the model produced the briefing, False when we fell back
    to embedding the raw dashboards (Ollama down, model missing, timeout, …) so
    the requester always gets at least the data."""
    model = model or OLLAMA_MODEL
    url = url or OLLAMA_URL
    try:
        response, actual_model = _call_ollama(
            system=_load_system_prompt(), user=market_data, model=model, url=url,
        )
        return _success_text(actual_model, response), True
    except OllamaError as e:
        return _fallback_text(str(e), model, market_data), False
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
    """Run the pipeline: load market_data.md + SKILL.md, call Ollama, and
    write final_briefing.md. Returns an exit code (0 success, 1 fallback)."""
    # `market_data` is captured as soon as step 1 succeeds so the fallback
    # writer can embed the raw dashboards even if a LATER step fails. It
    # stays None if step 1 itself fails (nothing to embed yet).
    started = time.monotonic()
    market_data = None
    try:
        # Step 1: load market_data.md (the upstream dashboards + headlines).
        market_data = _load_market_data()
        # Step 2: load SKILL.md (+ optional prompts/) — the analyst persona.
        system_prompt = _load_system_prompt()
        # Step 3: call Ollama to produce the analyst-layer briefing.
        print(
            f"[generate_briefing] calling {OLLAMA_URL} with model "
            f"{OLLAMA_MODEL} (timeout {OLLAMA_TIMEOUT_SEC}s)"
        )
        response, actual_model = _call_ollama(
            system=system_prompt,
            user=market_data,
            model=OLLAMA_MODEL,
            url=OLLAMA_URL,
        )
    except (FileNotFoundError, OllamaError) as e:
        # Expected, well-described failures — str(e) is already operator-readable
        # (missing input file, or a specific Ollama failure reason).
        reason = str(e)
        print(f"[generate_briefing] FAILED: {reason}")
        _write_fallback(reason, OLLAMA_MODEL, market_data)
        _post_fallback(reason, OLLAMA_MODEL, time.monotonic() - started)
        return 1
    except Exception as e:  # pylint: disable=broad-exception-caught
        # The whole point of this script is that bot.py ALWAYS has a file to
        # forward. An UNEXPECTED error (permission denied, decode error, an
        # unforeseen bug) must still yield a fallback briefing, never a bare
        # crash that leaves final_briefing.md untouched. Tag the type so the
        # reason line is debuggable.
        reason = f"unexpected {type(e).__name__}: {e}"
        print(f"[generate_briefing] FAILED (unexpected): {reason}")
        _write_fallback(reason, OLLAMA_MODEL, market_data)
        _post_fallback(reason, OLLAMA_MODEL, time.monotonic() - started)
        return 1

    _write_success(actual_model, response)
    print(f"[generate_briefing] wrote {OUTPUT_PATH} ({len(response)} chars)")
    _post_success(actual_model, time.monotonic() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
