"""
Daily news briefing generator.

Calls the Anthropic API with the web_search tool to build a personalised,
six-topic news brief, then writes it to a plain-text file and to an Alexa
Flash Briefing JSON feed.

Run directly to generate today's brief:

    python main.py
"""

import html
import json
import logging
import os
import re
import sys
import time
import urllib.request
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

# Optional daily email of the FULL brief via Resend. Set RESEND_API_KEY to
# enable (no-op if unset). Recipient/sender can be overridden via env.
EMAIL_TO = os.getenv("BRIEF_EMAIL_TO") or "imjohnny252@gmail.com"
EMAIL_FROM = os.getenv("BRIEF_EMAIL_FROM") or "Daily Brief <onboarding@resend.dev>"

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
        "Output exactly in this structure:\n"
        "Line 1: the header exactly as: {header}\n"
        "Line 2: HEADLINE: then ONE punchy sentence (under 16 words) capturing the "
        "single biggest story for this topic, with a number or named entity.\n"
        "Then exactly 3 dash-prefixed bullet points giving the fuller detail, each "
        "one short sentence with a specific number, named company, person or product.\n\n"
        "Plain text only. Start with the header line. No preamble and no sign-off."
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
                "headline": "no fresh news today", "search_ok": False,
                "usage": dict(_ZERO_USAGE)}

    text, searches, errors = _parse_response(response)

    # Some models (notably Haiku) narrate before the content, e.g.
    # "I'll search for...Here is the section:". The section must begin at the
    # emoji header, so drop anything before the first occurrence of the emoji.
    idx = text.find(topic["emoji"])
    if idx > 0:
        text = text[idx:].strip()
    elif idx == -1 and text:
        text = header + "\n" + text  # model omitted the header; restore it

    # Pull out the HEADLINE line (used for the short spoken brief) and keep the
    # header + bullets as the full section body (used for the web page).
    headline = ""
    body_lines = []
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            continue
        if s.upper().startswith("HEADLINE:"):
            headline = s.split(":", 1)[1].strip()
        else:
            body_lines.append(s)
    text = "\n".join(body_lines)
    if not headline:  # fallback: use the first bullet
        for s in body_lines:
            if s.startswith("- "):
                headline = s[2:].strip()
                break

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
    if not headline:
        headline = "no fresh news today"

    return {"title": topic["title"], "header": header, "text": text,
            "headline": headline, "search_ok": search_ok, "usage": _usage_of(response)}


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


def build_short_text(sections, closing, now_london):
    """The short spoken brief: greeting + one headline per topic + closing."""
    greeting = "Good morning. Here are your headlines for {d}.".format(
        d=now_london.strftime("%A %-d %B %Y")
    )
    parts = [greeting]
    for s in sections:
        headline = (s.get("headline") or "").strip().rstrip(".")
        if headline:
            parts.append("{}. {}.".format(s["title"], headline))
    parts.append(closing)
    return "\n\n".join(parts)


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Your Daily Brief — {date}</title>
<style>
 body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 42rem;
        margin: 0 auto; padding: 1.5rem 1.1rem 4rem; line-height: 1.55;
        color: #1c1c1e; background: #fff; }}
 h1 {{ font-size: 1.5rem; margin: 0 0 .15rem; }}
 .date {{ color: #666; margin: 0 0 1.6rem; font-size: .95rem; }}
 section {{ margin: 0 0 1.7rem; }}
 h2 {{ font-size: 1.15rem; margin: 0 0 .5rem; }}
 ul {{ margin: 0; padding-left: 1.15rem; }}
 li {{ margin: 0 0 .5rem; }}
 .closing {{ margin-top: 2rem; padding: 1rem 1.1rem; background: #f4f6f8;
            border-radius: 12px; font-weight: 600; }}
 .foot {{ margin-top: 2.5rem; color: #999; font-size: .8rem; }}
 @media (prefers-color-scheme: dark) {{
   body {{ background: #000; color: #eee; }}
   .closing {{ background: #1c1c1e; }}
   .date, .foot {{ color: #888; }}
 }}
</style>
</head>
<body>
<h1>Your Daily Brief</h1>
<p class="date">{date}</p>
{body}
<p class="closing">{closing}</p>
<p class="foot">Updated {updated}. Generated automatically each weekday.</p>
</body>
</html>
"""


def build_html(sections, closing, now_london):
    blocks = []
    for s in sections:
        lines = [ln.strip() for ln in s["text"].split("\n") if ln.strip()]
        header = lines[0] if lines else s["title"]
        bullets = []
        for ln in lines[1:]:
            text = ln[2:].strip() if ln.startswith("- ") else ln
            bullets.append("<li>{}</li>".format(html.escape(text)))
        blocks.append("<section><h2>{}</h2><ul>{}</ul></section>".format(
            html.escape(header), "".join(bullets)))
    return HTML_TEMPLATE.format(
        date=now_london.strftime("%A %-d %B %Y"),
        body="\n".join(blocks),
        closing=html.escape(closing),
        updated=now_london.strftime("%H:%M %Z"),
    )


def build_email_html(sections, closing, now_london):
    """Full brief as an email body with inline styles (robust across clients)."""
    date_str = now_london.strftime("%A %-d %B %Y")
    p = ['<div style="font-family:-apple-system,Segoe UI,Arial,sans-serif;'
         'max-width:640px;margin:0 auto;color:#1c1c1e;line-height:1.5;">']
    p.append('<h1 style="font-size:20px;margin:0 0 2px;">Your Daily Brief</h1>')
    p.append('<p style="color:#666;margin:0 0 20px;font-size:14px;">{}</p>'.format(
        html.escape(date_str)))
    for s in sections:
        lines = [ln.strip() for ln in s["text"].split("\n") if ln.strip()]
        header = lines[0] if lines else s["title"]
        p.append('<h2 style="font-size:16px;margin:18px 0 6px;">{}</h2>'.format(
            html.escape(header)))
        p.append('<ul style="margin:0;padding-left:18px;">')
        for ln in lines[1:]:
            b = ln[2:].strip() if ln.startswith("- ") else ln
            p.append('<li style="margin:0 0 6px;">{}</li>'.format(html.escape(b)))
        p.append('</ul>')
    p.append('<p style="margin-top:24px;padding:12px 14px;background:#f4f6f8;'
             'border-radius:10px;font-weight:600;">{}</p>'.format(html.escape(closing)))
    p.append('<p style="color:#999;font-size:12px;margin-top:24px;">'
             'Full brief online: https://osher252.github.io/daily-brief/</p>')
    p.append('</div>')
    return "\n".join(p)


def send_email(subject, html_body):
    """Email the brief via Resend. No-op if RESEND_API_KEY isn't set."""
    key = os.getenv("RESEND_API_KEY")
    if not key:
        logger.info("RESEND_API_KEY not set; skipping email.")
        return
    payload = json.dumps({
        "from": EMAIL_FROM,
        "to": [EMAIL_TO],
        "subject": subject,
        "html": html_body,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            logger.info("Email sent to %s (HTTP %s).", EMAIL_TO, resp.status)
    except Exception as exc:  # noqa: BLE001 — email failure must not fail the run
        logger.error("Email send failed: %s", exc)


def write_outputs(short_text, full_text, sections, closing, now_london, now_utc):
    date_compact = now_london.strftime("%Y%m%d")

    txt_path = OUTPUT_DIR / "{d}_brief.txt".format(d=date_compact)
    txt_path.write_text(full_text, encoding="utf-8")

    feed = {
        "uid": "daily-brief-{d}".format(d=date_compact),
        "updateDate": now_utc.strftime("%Y-%m-%dT%H:%M:%S.0Z"),
        "titleText": "Your Daily Brief",
        "mainText": short_text,   # what Alexa speaks — short headlines
        "fullText": full_text,    # full detail (also rendered on the web page)
        "redirectionUrl": REDIRECT_URL,
    }
    feed_path = OUTPUT_DIR / "alexa_feed.json"
    feed_path.write_text(json.dumps(feed, ensure_ascii=False, indent=2), encoding="utf-8")

    html_path = OUTPUT_DIR / "index.html"
    html_path.write_text(build_html(sections, closing, now_london), encoding="utf-8")

    return txt_path, feed_path, html_path


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

    full_text = build_main_text(sections, closing, now_london)
    short_text = build_short_text(sections, closing, now_london)

    txt_path, feed_path, html_path = write_outputs(
        short_text, full_text, sections, closing, now_london, now_utc
    )

    # Email the full brief (no-op unless RESEND_API_KEY is set).
    send_email(
        "Your Daily Brief — {}".format(date_str),
        build_email_html(sections, closing, now_london),
    )

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
    logger.info(
        "=== Run complete: %d/%d topics live | spoken %d words / full %d words | %s | %s | %s ===",
        ok, len(sections), len(short_text.split()), len(full_text.split()),
        txt_path.name, feed_path.name, html_path.name,
    )

    print("\n===== SPOKEN (Alexa) =====")
    print(short_text)
    print("\n===== FULL (web page) =====")
    print(full_text)
    print("=" * 70 + "\n")

    return short_text


if __name__ == "__main__":
    run()
