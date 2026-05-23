"""
Daily news briefing generator.

Calls the Anthropic API with the web_search tool to build a personalised,
six-topic news brief, then writes it to a plain-text file and to an Alexa
Flash Briefing JSON feed.

Run directly to generate today's brief:

    python main.py
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

import anthropic

# --------------------------------------------------------------------------- #
# Paths and configuration
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

load_dotenv(BASE_DIR / ".env")

# A capable model that supports the web_search tool. Override with BRIEF_MODEL.
# Use "claude-opus-4-7" for maximum quality, or "claude-sonnet-4-6" (default)
# for a strong, cost-effective daily run.
# `or` (not a default arg) so an empty BRIEF_MODEL="" still falls back correctly.
# Default is Haiku 4.5 — fastest and cheapest, fine for a news brief. Switch to
# "claude-sonnet-4-6" (sharper writing) or "claude-opus-4-7" via BRIEF_MODEL.
MODEL = os.getenv("BRIEF_MODEL") or "claude-haiku-4-5"

# Alexa Flash Briefing requires a valid, absolute https URL here — an empty
# string is rejected. Points at the GitHub Pages site that hosts the feed.
REDIRECT_URL = os.getenv("BRIEF_REDIRECT_URL") or "https://osher252.github.io/daily-brief/"

# Alexa caps a text item's mainText at 4500 characters. We aim well under and
# hard-trim as a safety net so the feed can never be rejected for length.
MAX_MAINTEXT_CHARS = 4400

# Approximate pricing for the per-run cost estimate logged at the end (USD).
# ($ per million tokens) input, output — keyed by model. Update if prices change.
PRICING = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4-7": (5.0, 25.0),
}
PRICE_INPUT_PER_M, PRICE_OUTPUT_PER_M = PRICING.get(MODEL, (1.0, 5.0))
PRICE_PER_SEARCH = 0.01    # web search, $10 per 1,000 searches (model-independent)
USD_TO_GBP = 0.79          # rough, for a friendly pence figure in the log

LONDON = ZoneInfo("Europe/London")

# Server-side web search tool. The type string is the released version id.
# max_uses caps searches per topic — 3 is plenty for 3 bullets and keeps the
# per-run cost down (web search is billed per search plus the result tokens).
WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 3,
}

# --------------------------------------------------------------------------- #
# Logging — file + stdout, with timestamps
# --------------------------------------------------------------------------- #

logger = logging.getLogger("daily-brief")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    _fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s")

    _file = logging.FileHandler(OUTPUT_DIR / "run.log")
    _file.setFormatter(_fmt)
    logger.addHandler(_file)

    _stream = logging.StreamHandler(sys.stdout)
    _stream.setFormatter(_fmt)
    logger.addHandler(_stream)

# --------------------------------------------------------------------------- #
# Who we are writing for (hardcoded reader profile)
# --------------------------------------------------------------------------- #

READER_PROFILE = """About the reader (write everything for this specific person):
- Half-Israeli, half-British consultant based in Walthamstow, London.
- Background in B2B SaaS sales leadership (Monday.com, and Israeli tech companies expanding into the UK).
- Currently doing fractional go-to-market (GTM) advisory work.
- Runs a personal sleep optimisation experiment with eight years of Fitbit data.
- Has two young kids.
- Interested in UK personal finance: mortgages, ISAs, savings rates, and named providers such as Chase, Trading 212, and Atom.
- Follows Israeli politics and UK politics closely."""

# The base system prompt supplied by the spec. {date} is filled in at runtime.
BASE_SYSTEM_PROMPT = (
    "You are a news research assistant generating a personalised daily briefing. "
    "Search the web for the latest news across the specified topics. "
    "Today's date is {date}. Prioritise stories from the last 7 days. "
    "Be specific — include numbers, named companies, named people, and named "
    "financial products where relevant. Write in plain text suitable for "
    "text-to-speech. No markdown formatting. No bullet symbols — use dashes. "
    "No bold text."
)

# --------------------------------------------------------------------------- #
# The six topics
# --------------------------------------------------------------------------- #

TOPICS = [
    {
        "emoji": "\U0001F916",  # robot
        "title": "AI models and products",
        "focus": (
            "New model releases, benchmarks, agentic tools and developer "
            "products relevant to someone building AI-powered products."
        ),
    },
    {
        "emoji": "\U0001F4B7",  # pound banknote
        "title": "UK personal finance",
        "focus": (
            "Savings rates, ISA changes, mortgage rates and Bank of England "
            "moves. Name specific providers and products (for example Chase, "
            "Trading 212, Atom, Nationwide) with their current rates."
        ),
    },
    {
        "emoji": "\U0001F1EE\U0001F1F1",  # Israel flag
        "title": "Israeli politics",
        "focus": (
            "The coalition, elections, Gaza, and diplomatic developments. "
            "Name the politicians and parties involved."
        ),
    },
    {
        "emoji": "\U0001F1EC\U0001F1E7",  # UK flag
        "title": "UK politics",
        "focus": (
            "Labour, Reform, Starmer or a successor, and anything touching "
            "schools or London. Name the politicians and policies."
        ),
    },
    {
        "emoji": "\U0001F634",  # sleeping face
        "title": "Sleep and wearables",
        "focus": (
            "Sleep research, new devices, REM and HRV science, and news from "
            "Fitbit, Oura and Whoop."
        ),
    },
    {
        "emoji": "\U0001F4C8",  # chart increasing
        "title": "B2B SaaS and go-to-market",
        "focus": (
            "Funding rounds, go-to-market strategy shifts, AI in sales, and "
            "SaaS metrics. Name the companies and the numbers."
        ),
    },
]


# --------------------------------------------------------------------------- #
# Anthropic response parsing
# --------------------------------------------------------------------------- #


def _parse_response(response):
    """Pull text, count web searches, and collect any search errors.

    Returns (text, search_count, errors) where errors is a list of strings.
    """
    text_parts = []
    search_count = 0
    errors = []

    for block in response.content:
        btype = getattr(block, "type", None)

        if btype == "text":
            text_parts.append(block.text)

        elif btype == "server_tool_use":
            # Claude issued a search request.
            search_count += 1

        elif btype == "web_search_tool_result":
            content = getattr(block, "content", None)
            # On error, content is a single error object rather than a list.
            ctype = getattr(content, "type", None)
            if ctype == "web_search_tool_result_error":
                errors.append(getattr(content, "error_code", "unknown_error"))

    # Join with "" — when Claude cites web sources it splits a single sentence
    # across several text blocks, so gluing them directly (not with newlines)
    # reconstructs the original prose. The model's own line breaks are preserved.
    text = "".join(text_parts)
    # Collapse any run of 3+ blank lines down to a single blank line.
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, search_count, errors


def _usage_of(response):
    """Pull token + web-search counts from a response for cost tracking."""
    u = getattr(response, "usage", None)
    searches = 0
    stu = getattr(u, "server_tool_use", None)
    if stu is not None:
        searches = getattr(stu, "web_search_requests", 0) or 0
    return {
        "in": getattr(u, "input_tokens", 0) or 0,
        "out": getattr(u, "output_tokens", 0) or 0,
        "searches": searches,
    }


_ZERO_USAGE = {"in": 0, "out": 0, "searches": 0}


def _create_with_retry(client, max_attempts=4, **kwargs):
    """Call the Messages API, retrying on rate limits and transient server
    errors with a wait. This is a once-a-day batch job, so waiting ~30-60s to
    let an entry-tier per-minute limit reset is perfectly acceptable.
    """
    delay = 30.0
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            return client.messages.create(**kwargs)
        except anthropic.RateLimitError as exc:
            last_exc = exc
            wait = delay
            try:
                hdr = exc.response.headers.get("retry-after")
                if hdr:
                    wait = float(hdr)
            except Exception:  # noqa: BLE001
                pass
            logger.warning(
                "Rate limited (attempt %d/%d). Waiting %.0fs before retry.",
                attempt, max_attempts, wait,
            )
            time.sleep(wait)
            delay = min(delay * 2, 120)
        except (anthropic.APIConnectionError, anthropic.InternalServerError) as exc:
            last_exc = exc
            logger.warning(
                "Transient error (attempt %d/%d): %s. Retrying in %.0fs.",
                attempt, max_attempts, exc, delay,
            )
            time.sleep(delay)
            delay = min(delay * 2, 120)
    # Out of attempts — re-raise so the caller can degrade this section.
    raise last_exc


# --------------------------------------------------------------------------- #
# Section generation (one web-search call per topic)
# --------------------------------------------------------------------------- #


def generate_section(client, topic, date_str):
    """Generate a single topic section. Never raises — failures are reported
    in the returned text so one bad topic cannot crash the whole brief.

    Returns a dict: {"title", "header", "text", "search_ok"}.
    """
    header = "{emoji} {title}".format(emoji=topic["emoji"], title=topic["title"])
    system = BASE_SYSTEM_PROMPT.format(date=date_str) + "\n\n" + READER_PROFILE

    user_message = (
        "Write the \"{title}\" section of today's brief.\n"
        "Focus: {focus}\n\n"
        "Format the section exactly like this:\n"
        "- First line: the header exactly as: {header}\n"
        "- Then exactly 3 dash-prefixed bullet points. Each bullet is ONE short "
        "sentence carrying one fact plus its implication, with a specific number, "
        "named company, named person or named product.\n\n"
        "Keep the whole section under 60 words (this is a hard limit — Alexa has "
        "a strict length cap). Plain text only. Start with the header line. "
        "No preamble and no sign-off."
    ).format(title=topic["title"], focus=topic["focus"], header=header)

    try:
        response = _create_with_retry(
            client,
            model=MODEL,
            max_tokens=1500,
            system=system,
            tools=[WEB_SEARCH_TOOL],
            messages=[{"role": "user", "content": user_message}],
        )
    except Exception as exc:  # noqa: BLE001 — we want to degrade gracefully
        logger.error("Topic '%s' API call failed: %s", topic["title"], exc)
        text = header + "\n- News for this topic is unavailable today (the request failed)."
        return {"title": topic["title"], "header": header, "text": text,
                "search_ok": False, "usage": dict(_ZERO_USAGE)}

    text, searches, errors = _parse_response(response)

    # Some models (notably Haiku) narrate before the content, e.g.
    # "I'll search for...Here is the section:". The section must begin at the
    # emoji header, so drop anything before the first occurrence of the emoji.
    idx = text.find(topic["emoji"])
    if idx > 0:
        text = text[idx:].strip()
    elif idx == -1 and text:
        text = header + "\n" + text  # model omitted the header; restore it

    # 'max_uses_exceeded' is not a real failure — the searches that ran returned
    # results; the model merely asked for one more than the cap allows.
    real_errors = [e for e in errors if e != "max_uses_exceeded"]
    search_ok = searches > 0 and not real_errors

    if real_errors:
        logger.warning("Topic '%s' web search errors: %s", topic["title"], real_errors)
    logger.info(
        "Topic '%s': %d web search(es), errors=%s",
        topic["title"], searches, errors or "none",
    )

    if not text:
        text = header + "\n- No content was returned for this topic today."
    elif not search_ok:
        # Keep whatever Claude wrote but flag that it is not freshly sourced.
        text = text + "\n- (Note: live web results were unavailable for this topic, so the above may not be current.)"

    return {"title": topic["title"], "header": header, "text": text,
            "search_ok": search_ok, "usage": _usage_of(response)}


# --------------------------------------------------------------------------- #
# Closing question (a single synthesis call, no web search)
# --------------------------------------------------------------------------- #


def generate_closing(client, brief_text, date_str):
    system = BASE_SYSTEM_PROMPT.format(date=date_str) + "\n\n" + READER_PROFILE
    user_message = (
        "Here is today's brief:\n\n"
        + brief_text
        + "\n\nWrite ONE sharp closing question (a single sentence) that is "
        "directly relevant to the reader's current situation — fractional "
        "go-to-market advisor, builder of AI products, UK personal finance, an "
        "eight-year sleep experiment, parent of two young kids. Make it land. "
        "Output only the question, prefixed exactly with 'Today's question: '. "
        "Plain text, no markdown."
    )

    try:
        response = _create_with_retry(
            client,
            model=MODEL,
            max_tokens=200,
            system=system,
            messages=[{"role": "user", "content": user_message}],
        )
        text, _, _ = _parse_response(response)
        if text:
            return text.strip(), _usage_of(response)
    except Exception as exc:  # noqa: BLE001
        logger.error("Closing-question call failed: %s", exc)

    fallback = "Today's question: What is the one small change you can make today that compounds across your work, money, and sleep?"
    return fallback, dict(_ZERO_USAGE)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def _trim_to_sentence(text, budget):
    """Trim text to at most `budget` characters, cutting at the last sentence
    or line boundary so it never ends mid-word."""
    if len(text) <= budget:
        return text
    cut = text[:budget]
    pos = max(cut.rfind(s) for s in ("\n", ". ", "? ", "! ", ".", "?", "!"))
    if pos > 0:
        cut = cut[: pos + 1]
    return cut.rstrip()


def build_main_text(sections, closing, now_london):
    greeting = "Good morning. Here is your daily brief for {d}.".format(
        d=now_london.strftime("%A %-d %B %Y")
    )
    body = "\n\n".join(s["text"] for s in sections)
    full = greeting + "\n\n" + body + "\n\n" + closing
    if len(full) <= MAX_MAINTEXT_CHARS:
        return full

    # Over Alexa's limit — keep the greeting and closing, trim the body to fit.
    reserve = len(greeting) + len(closing) + 4  # 4 for the two "\n\n" joins
    body = _trim_to_sentence(body, MAX_MAINTEXT_CHARS - reserve)
    logger.warning(
        "Brief exceeded %d chars; trimmed the body to fit Alexa's limit.",
        MAX_MAINTEXT_CHARS,
    )
    return greeting + "\n\n" + body + "\n\n" + closing


def write_outputs(main_text, now_london, now_utc):
    date_compact = now_london.strftime("%Y%m%d")

    txt_path = OUTPUT_DIR / "{d}_brief.txt".format(d=date_compact)
    txt_path.write_text(main_text, encoding="utf-8")

    feed = {
        "uid": "daily-brief-{d}".format(d=date_compact),
        "updateDate": now_utc.strftime("%Y-%m-%dT%H:%M:%S.0Z"),
        "titleText": "Your Daily Brief",
        "mainText": main_text,
        "redirectionUrl": REDIRECT_URL,
    }
    feed_path = OUTPUT_DIR / "alexa_feed.json"
    feed_path.write_text(json.dumps(feed, ensure_ascii=False, indent=2), encoding="utf-8")

    return txt_path, feed_path


def run():
    """Generate the brief, write outputs, print to stdout. Returns main_text."""
    now_utc = datetime.now(timezone.utc)
    now_london = now_utc.astimezone(LONDON)
    date_str = now_london.strftime("%A %-d %B %Y")

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY is not set. Create a .env file (see .env.example).")
        raise SystemExit(1)

    logger.info("=== Run start: %s (Europe/London) | model=%s ===", date_str, MODEL)
    client = anthropic.Anthropic(api_key=api_key)

    sections = []
    for topic in TOPICS:
        logger.info("Generating section: %s", topic["title"])
        sections.append(generate_section(client, topic, date_str))

    interim = "\n\n".join(s["text"] for s in sections)
    closing, closing_usage = generate_closing(client, interim, date_str)

    main_text = build_main_text(sections, closing, now_london)

    txt_path, feed_path = write_outputs(main_text, now_london, now_utc)

    # Tally usage and estimate the run's cost.
    usages = [s["usage"] for s in sections] + [closing_usage]
    tot_in = sum(u["in"] for u in usages)
    tot_out = sum(u["out"] for u in usages)
    tot_searches = sum(u["searches"] for u in usages)
    cost_usd = (
        tot_in / 1_000_000 * PRICE_INPUT_PER_M
        + tot_out / 1_000_000 * PRICE_OUTPUT_PER_M
        + tot_searches * PRICE_PER_SEARCH
    )
    logger.info(
        "Usage: %s input + %s output tokens, %d web searches  ~  est. cost $%.3f (~%.0fp)",
        f"{tot_in:,}", f"{tot_out:,}", tot_searches, cost_usd, cost_usd * USD_TO_GBP * 100,
    )

    ok = sum(1 for s in sections if s["search_ok"])
    word_count = len(main_text.split())
    logger.info(
        "=== Run complete: %d/%d topics had live web results | %d words | %s | %s ===",
        ok, len(sections), word_count, txt_path.name, feed_path.name,
    )

    print("\n" + "=" * 70)
    print(main_text)
    print("=" * 70 + "\n")

    return main_text


if __name__ == "__main__":
    run()
