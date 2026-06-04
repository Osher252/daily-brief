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
import urllib.error
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
# Optional comma-separated CC recipients. Note: Resend's free tier (using the
# onboarding@resend.dev sender) only delivers to the account owner — to CC
# others, verify their email in Resend or verify your own domain.
EMAIL_CC = [a.strip() for a in (os.getenv("BRIEF_EMAIL_CC") or "").split(",") if a.strip()]

# A real browser UA — Cloudflare/WAFs block the default urllib agent (err 1010).
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Optional smooth spoken audio via OpenAI TTS (far less robotic than Alexa's
# built-in voices). Set OPENAI_API_KEY to enable. main.py writes the raw MP3;
# the workflow transcodes it to Alexa's audio spec and publishes it.
OPENAI_TTS_MODEL = os.getenv("OPENAI_TTS_MODEL") or "gpt-4o-mini-tts"
OPENAI_TTS_VOICE = os.getenv("OPENAI_TTS_VOICE") or "shimmer"
# Voices to alternate per segment (greeting, each headline, closing). One name
# = single voice; two+ = they trade off line by line (two-presenter feel).
OPENAI_TTS_VOICES = os.getenv("OPENAI_TTS_VOICES") or "shimmer,ash"
OPENAI_TTS_INSTRUCTIONS = (os.getenv("OPENAI_TTS_INSTRUCTIONS") or
    "Speak like a warm, upbeat morning news presenter: energetic and friendly, "
    "smooth and natural, with lively but clear pacing.")
AUDIO_FILE = "brief.mp3"  # published filename on the web page

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
# Topic catalogue + per-weekday schedule
# --------------------------------------------------------------------------- #

TOPIC_CATALOG = {
    "ai": {
        "emoji": "\U0001F916",  # robot
        "title": "AI models and products",
        "focus": ("New model releases, benchmarks, agentic tools and developer "
                  "products relevant to someone building AI-powered products."),
        "feeds": [
            {"name": "TechCrunch AI",   "url": "https://techcrunch.com/category/artificial-intelligence/feed/"},
            {"name": "The Verge",       "url": "https://www.theverge.com/rss/index.xml"},
            {"name": "Hugging Face Blog","url": "https://huggingface.co/blog/feed.xml"},
        ],
    },
    "finance": {
        "emoji": "\U0001F4B7",  # pound banknote
        "title": "UK personal finance",
        "focus": ("Savings rates, ISA changes, mortgage rates and Bank of England "
                  "moves. Name specific providers and products (Chase, Trading 212, "
                  "Atom, Nationwide) with their current rates where available."),
        "feeds": [
            {"name": "Guardian Money", "url": "https://www.theguardian.com/money/rss"},
            {"name": "BBC Business",   "url": "http://feeds.bbci.co.uk/news/business/rss.xml"},
            {"name": "This Is Money",  "url": "https://www.thisismoney.co.uk/money/index.rss"},
        ],
    },
    "israel": {
        "emoji": "\U0001F1EE\U0001F1F1",  # Israel flag
        "title": "Israeli politics",
        "focus": ("The coalition, elections, Gaza, and diplomatic developments. "
                  "Name the politicians and parties involved."),
        "feeds": [
            {"name": "+972 Magazine (left)",     "url": "https://www.972mag.com/feed/"},
            {"name": "Times of Israel (centre)", "url": "https://www.timesofisrael.com/feed/"},
            {"name": "Jerusalem Post (right)",   "url": "https://www.jpost.com/rss/rssfeedsfrontpage.aspx"},
        ],
    },
    "uk_politics": {
        "emoji": "\U0001F1EC\U0001F1E7",  # UK flag
        "title": "UK politics",
        "focus": ("Labour, Reform, Starmer or a successor, and anything touching "
                  "schools or London. Name the politicians and policies."),
        "feeds": [
            {"name": "Guardian Politics (left)", "url": "https://www.theguardian.com/politics/rss"},
            {"name": "BBC Politics (centre)",    "url": "http://feeds.bbci.co.uk/news/politics/rss.xml"},
            {"name": "ConservativeHome (right)", "url": "https://conservativehome.com/feed/"},
        ],
    },
    "tech": {
        "emoji": "\U0001F4F1",  # mobile phone
        "title": "Tech news",
        "focus": ("Interesting, fun or notable tech and gadget stories from across "
                  "the industry — device launches, startups, internet culture, dev "
                  "tools, security drama, anything an AI-product builder would find "
                  "worth knowing. Skip pure AI-model releases (covered separately)."),
        "feeds": [
            {"name": "Techmeme", "url": "https://www.techmeme.com/feed.xml"},
        ],
    },
    "b2b": {
        "emoji": "\U0001F4C8",  # chart increasing
        "title": "B2B SaaS and go-to-market",
        "focus": ("Funding rounds, go-to-market strategy shifts, AI in sales, and "
                  "SaaS metrics. Name the companies and the numbers."),
        "feeds": [
            {"name": "SaaStr",              "url": "https://www.saastr.com/feed/"},
            {"name": "TechCrunch Venture",  "url": "https://techcrunch.com/category/venture/feed/"},
            {"name": "TechCrunch Startups", "url": "https://techcrunch.com/category/startups/feed/"},
        ],
    },
    "israeli_tech": {
        "emoji": "\U0001F680",  # rocket
        "title": "Israeli tech",
        "focus": ("Israeli startups, scale-ups, exits, funding rounds and "
                  "UK / global expansion stories. Name founders, companies, "
                  "deal sizes."),
        "feeds": [
            {"name": "NoCamels",        "url": "https://nocamels.com/feed/"},
            {"name": "TechCrunch Israel","url": "https://techcrunch.com/tag/israel/feed/"},
        ],
    },
    "ai_safety": {
        "emoji": "\U0001F6E1️",  # shield
        "title": "AI safety & policy",
        "focus": ("AI safety, model evaluations, alignment, the EU AI Act, UK "
                  "AI policy, US AI executive orders, frontier-lab governance "
                  "news. Prefer specifics: papers, regulators, named labs."),
        "feeds": [
            {"name": "MIT Tech Review", "url": "https://www.technologyreview.com/feed/"},
            {"name": "AI Snake Oil",    "url": "https://www.aisnakeoil.com/feed"},
            {"name": "Hugging Face Blog","url": "https://huggingface.co/blog/feed.xml"},
        ],
    },
    "schools": {
        "emoji": "\U0001F3EB",  # school
        "title": "Schools & education",
        "focus": ("UK schools and education policy: Ofsted, SEND, exam reforms, "
                  "school funding and London-specific schooling news. Parent "
                  "angle — say what changes for families."),
        "feeds": [
            {"name": "BBC Education",      "url": "http://feeds.bbci.co.uk/news/education/rss.xml"},
            {"name": "Guardian Education", "url": "https://www.theguardian.com/education/rss"},
            {"name": "Schools Week",       "url": "https://schoolsweek.co.uk/feed/"},
        ],
    },
    "health": {
        "emoji": "\U0001F9EC",  # DNA
        "title": "Health, sleep & longevity",
        "focus": ("Sleep research, biomarkers, longevity science, intervention "
                  "studies and wearable-data findings. Prefer peer-reviewed "
                  "results; name researchers, institutions and specific numbers."),
        "feeds": [
            {"name": "STAT News",              "url": "https://www.statnews.com/feed/"},
            {"name": "Eric Topol — Ground Truths", "url": "https://erictopol.substack.com/feed"},
            {"name": "Guardian Science",       "url": "https://www.theguardian.com/science/rss"},
        ],
    },
    "research": {
        "emoji": "\U0001F52C",  # microscope
        "title": "Verified research",
        "focus": ("Significant new peer-reviewed findings across science, "
                  "medicine, AI and tech — ONLY from highly-credible outlets. "
                  "Name the paper, institution, and the specific result with "
                  "a number where possible."),
        "feeds": [
            {"name": "Nature news", "url": "https://www.nature.com/nature.rss"},
            {"name": "Quanta",      "url": "https://www.quantamagazine.org/feed/"},
            {"name": "phys.org",    "url": "https://phys.org/rss-feed/"},
        ],
    },
}


def select_topics(now_london):
    """Pick today's topics by weekday (Mon=0 .. Fri=4) — balanced so every
    day has the same load: 3 sections + HN top-3 from run().
      Mon: finance + schools + politics
      Tue: israeli_tech + politics + tech (Techmeme as filler)
      Wed: b2b + research + politics
      Thu: health + politics + tech (Techmeme as filler)
      Fri: ai + ai_safety + politics
    Politics alternates Israel / UK every other calendar day.
    Tech (Techmeme) only runs on the days that have one specialty topic,
    so the daily section count stays flat.
    """
    weekday = now_london.weekday()
    politics = "israel" if now_london.toordinal() % 2 == 0 else "uk_politics"

    keys = []
    if weekday == 0:          # Monday
        keys.append("finance")
        keys.append("schools")
    if weekday == 1:          # Tuesday
        keys.append("israeli_tech")
    if weekday == 2:          # Wednesday
        keys.append("b2b")
        keys.append("research")
    if weekday == 3:          # Thursday
        keys.append("health")
    if weekday == 4:          # Friday
        keys.append("ai")
        keys.append("ai_safety")
    keys.append(politics)
    if weekday in (1, 3):     # Tech (Techmeme) only on light days
        keys.append("tech")
    return [TOPIC_CATALOG[k] for k in keys]


# --------------------------------------------------------------------------- #
# RSS feed fetching — provides news context without paying for web search
# --------------------------------------------------------------------------- #


def _strip_html(s):
    """Crude tag stripper for RSS <description>/<summary> HTML."""
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


class _Redirect308(urllib.request.HTTPRedirectHandler):
    """urllib doesn't always handle HTTP 308 redirects — patch it in."""
    def http_error_308(self, req, fp, code, msg, headers):
        return self.http_error_301(req, fp, code, msg, headers)


_FEED_OPENER = urllib.request.build_opener(_Redirect308())


def fetch_feed(url, max_items=15):
    """Fetch one RSS/Atom feed -> list of {title, link, summary}. Returns []
    on any failure so a single bad feed never breaks a topic."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": _UA,
            "Accept": "application/rss+xml, application/atom+xml, "
                      "application/xml, text/xml, */*",
        })
        with _FEED_OPENER.open(req, timeout=10) as resp:
            xml_bytes = resp.read()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Feed fetch failed [%s]: %s", url, exc)
        return []

    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        logger.warning("Feed parse failed [%s]: %s", url, exc)
        return []

    items = []
    for entry in root.iter():
        tag = entry.tag.rsplit("}", 1)[-1]  # strip XML namespace
        if tag not in ("item", "entry"):
            continue
        title = link = summary = ""
        for child in entry:
            ctag = child.tag.rsplit("}", 1)[-1]
            text = (child.text or "").strip()
            if ctag == "title":
                title = text
            elif ctag == "link" and not link:
                link = text or child.get("href", "")
            elif ctag in ("description", "summary", "content") and not summary:
                summary = _strip_html(text)[:240]
        if title and link:
            items.append({"title": title, "link": link, "summary": summary})
        if len(items) >= max_items:
            break
    return items


def build_feed_context(feeds):
    """Fetch every feed for a topic and format it as readable context for the
    model: grouped by outlet, each item with title + (short) summary + URL.
    Also returns a {item_url: outlet_name} map so we can label sources even
    when the model forgets to include outlet names.
    Returns (context_text, total_item_count, url_to_outlet)."""
    blocks = []
    total = 0
    url_to_outlet = {}
    for feed in feeds:
        items = fetch_feed(feed["url"])
        if not items:
            continue
        lines = ["[" + feed["name"] + "]"]
        for it in items:
            url_to_outlet[it["link"]] = feed["name"]
            lines.append("- {title}{sep}{summary} <{link}>".format(
                title=it["title"],
                sep=" — " if it["summary"] else " ",
                summary=it["summary"],
                link=it["link"],
            ))
        blocks.append("\n".join(lines))
        total += len(items)
    return "\n\n".join(blocks), total, url_to_outlet


# --------------------------------------------------------------------------- #
# Morning extras: weather, currency, Hacker News top — all free APIs/feeds
# --------------------------------------------------------------------------- #

_WEATHER_CODES = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "foggy",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "rain showers", 81: "rain showers", 82: "heavy showers",
    85: "snow showers", 86: "snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with hail",
}


def _fmt_hour(h):
    """0 -> 'midnight', 12 -> 'noon', 7 -> '7am', 14 -> '2pm'."""
    h = h % 24
    if h == 0:
        return "midnight"
    if h == 12:
        return "noon"
    if h < 12:
        return "{}am".format(h)
    return "{}pm".format(h - 12)


def _rain_windows(hours, probs, threshold=50):
    """Group hours where prob >= threshold into (start, end_exclusive) windows."""
    out = []
    start = last = None
    for h, p in zip(hours, probs):
        if p is None:
            continue
        if p >= threshold:
            if start is None:
                start = h
            last = h
        elif start is not None:
            out.append((start, last + 1))
            start = last = None
    if start is not None:
        out.append((start, last + 1))
    return out


def fetch_weather(lat=51.583, lon=-0.020, place="Walthamstow"):
    """One-line weather summary from Open-Meteo (no API key). Includes the
    hour windows when rain is likely so the user can plan the day."""
    url = (
        "https://api.open-meteo.com/v1/forecast"
        "?latitude={lat}&longitude={lon}"
        "&current_weather=true"
        "&hourly=precipitation_probability"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        "&timezone=Europe%2FLondon&forecast_days=1"
    ).format(lat=lat, lon=lon)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with _FEED_OPENER.open(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
        cur = data.get("current_weather") or {}
        daily = data.get("daily") or {}
        temp = cur.get("temperature")
        desc = _WEATHER_CODES.get(int(cur.get("weathercode", 0)), "mixed weather")
        if temp is None:
            return ""
        bits = ["{place}: {t:.0f}°C, {d}".format(place=place, t=temp, d=desc)]
        hi = (daily.get("temperature_2m_max") or [None])[0]
        lo = (daily.get("temperature_2m_min") or [None])[0]
        if hi is not None and lo is not None:
            bits.append("high {h:.0f}° / low {l:.0f}°".format(h=hi, l=lo))
        pop = (daily.get("precipitation_probability_max") or [None])[0]
        if pop is not None and pop >= 20:
            rain_bit = "{p}% chance of rain".format(p=int(pop))
            # Pull hourly probabilities for today only and find rain windows.
            hourly = data.get("hourly") or {}
            times = hourly.get("time") or []
            hprobs = hourly.get("precipitation_probability") or []
            today_date = times[0][:10] if times and isinstance(times[0], str) else ""
            hours, p_today = [], []
            for t, pv in zip(times, hprobs):
                if isinstance(t, str) and t.startswith(today_date):
                    hours.append(int(t[11:13]))
                    p_today.append(pv)
            wins = _rain_windows(hours, p_today, threshold=50)
            if wins and wins[0][0] is not None:
                # Collapse to "most of the day" if a single ~10h+ window.
                if len(wins) == 1 and (wins[0][1] - wins[0][0]) >= 10:
                    rain_bit += " (expected most of the day)"
                else:
                    parts = ["{a}–{b}".format(a=_fmt_hour(s), b=_fmt_hour(e))
                             for s, e in wins[:2]]
                    rain_bit += " (expected " + " and ".join(parts) + ")"
            bits.append(rain_bit)
        return ", ".join(bits) + "."
    except Exception as exc:  # noqa: BLE001
        logger.warning("Weather fetch failed: %s", exc)
        return ""


def fetch_currency():
    """One-line FX rates (no API key)."""
    try:
        req = urllib.request.Request(
            "https://api.frankfurter.app/latest?base=GBP&symbols=USD,ILS",
            headers={"User-Agent": _UA},
        )
        with _FEED_OPENER.open(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
        rates = data.get("rates") or {}
        bits = []
        for sym in ("USD", "ILS"):
            v = rates.get(sym)
            if v is not None:
                bits.append("GBP/{s} {v:.3f}".format(s=sym, v=v))
        return " · ".join(bits) + "." if bits else ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("Currency fetch failed: %s", exc)
        return ""


def _hn_via_firebase(count):
    """Fallback HN fetcher using the official Firebase API."""
    items = []
    try:
        req = urllib.request.Request(
            "https://hacker-news.firebaseio.com/v0/topstories.json",
            headers={"User-Agent": _UA},
        )
        with _FEED_OPENER.open(req, timeout=8) as r:
            ids = json.loads(r.read().decode("utf-8"))[:count]
        for sid in ids:
            r2 = urllib.request.Request(
                "https://hacker-news.firebaseio.com/v0/item/{}.json".format(sid),
                headers={"User-Agent": _UA},
            )
            with _FEED_OPENER.open(r2, timeout=8) as resp:
                d = json.loads(resp.read().decode("utf-8")) or {}
            title = (d.get("title") or "").strip()
            link = d.get("url") or "https://news.ycombinator.com/item?id={}".format(sid)
            if title:
                items.append({"title": title, "link": link, "summary": ""})
    except Exception as exc:  # noqa: BLE001
        logger.warning("HN Firebase fallback failed: %s", exc)
    return items


def fetch_hn_section(count=3):
    """Top stories from Hacker News as a ready-to-render section dict.
    No Claude call — deterministic and free."""
    items = fetch_feed("https://hnrss.org/frontpage?count={}".format(count), max_items=count)
    if not items:
        items = _hn_via_firebase(count)
    if not items:
        return None
    header = "\U0001F4F0 Hacker News top {}".format(len(items))
    body_lines = [header]
    for it in items:
        # Embed the article URL in the bullet so the renderers can link it.
        body_lines.append("- {title} <{url}>".format(title=it["title"], url=it["link"]))
    return {
        "title": "Hacker News",
        "header": header,
        "text": "\n".join(body_lines),
        "headline": items[0]["title"] if items else "",
        "sources": [],            # links are inline in the bullets
        "search_ok": True,
        "usage": dict(_ZERO_USAGE),
        "skip_spoken": True,      # tech links don't read well aloud
    }


def _outlet_for_url(url, url_to_outlet):
    """Find the friendly outlet name for a URL. Falls back to its domain."""
    if url in url_to_outlet:
        return url_to_outlet[url]
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        # See if any known URL has the same domain.
        for u, name in url_to_outlet.items():
            uh = urlparse(u).netloc.lower()
            if uh.startswith("www."):
                uh = uh[4:]
            if uh == host:
                return name
        return host or "Source"
    except Exception:  # noqa: BLE001
        return "Source"


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
# Section generation (writes from pre-fetched RSS context — no web search)
# --------------------------------------------------------------------------- #


def generate_section(client, topic, date_str):
    """Generate a single topic section using pre-fetched RSS feed context.
    Never raises — degrades gracefully. Returns a dict with:
      title, header, text, headline, sources, search_ok, usage.
    """
    header = "{emoji} {title}".format(emoji=topic["emoji"], title=topic["title"])
    system = BASE_SYSTEM_PROMPT.format(date=date_str) + "\n\n" + READER_PROFILE

    feeds = topic.get("feeds", [])
    context, item_count, url_to_outlet = build_feed_context(feeds)
    logger.info("Topic '%s': %d items across %d feed(s).",
                topic["title"], item_count, len(feeds))

    if item_count == 0:
        text = header + "\n- News for this topic is unavailable today (feeds were unreachable)."
        return {"title": topic["title"], "header": header, "text": text,
                "headline": "no fresh news today", "sources": [],
                "search_ok": False, "usage": dict(_ZERO_USAGE)}

    user_message = (
        "Write the \"{title}\" section of today's brief using ONLY the items below. "
        "Do not invent facts or sources.\n"
        "Focus: {focus}\n\n"
        "Recent items from the listed outlets (use only these):\n\n"
        "{context}\n\n"
        "Output exactly:\n"
        "Line 1: the header exactly as: {header}\n"
        "Line 2: HEADLINE: ONE punchy sentence (under 16 words) capturing the single "
        "biggest story for this topic, with a number or named entity.\n"
        "Then exactly 3 dash-prefixed bullet points giving the fuller detail. "
        "Each bullet is one short sentence with a specific number, name, company "
        "or product, AND ends with the article URL in angle brackets, like: "
        "<https://...>. Use the EXACT URL of the item from the outlet list above "
        "that this bullet is based on — do not invent URLs. If you cite two "
        "outlets in one bullet, pick the primary one for the link.\n"
        "Then ONE final dash-prefixed bullet that begins with 'So what for you:' "
        "and gives a short, specific implication for THIS reader's situation "
        "(fractional GTM advisor, AI-product builder, UK personal finance, "
        "parent in Walthamstow). One sentence. No URL on this last bullet.\n\n"
        "Do NOT add a 'Sources:' line — each bullet already has its own URL.\n"
        "Plain text only. Start with the header line. No preamble and no sign-off."
    ).format(title=topic["title"], focus=topic["focus"], header=header, context=context)

    try:
        response = _create_with_retry(
            client,
            model=MODEL,
            max_tokens=1500,
            system=system,
            messages=[{"role": "user", "content": user_message}],
        )
    except Exception as exc:  # noqa: BLE001 — degrade gracefully
        logger.error("Topic '%s' API call failed: %s", topic["title"], exc)
        text = header + "\n- News for this topic is unavailable today (the request failed)."
        return {"title": topic["title"], "header": header, "text": text,
                "headline": "no fresh news today", "sources": [],
                "search_ok": False, "usage": dict(_ZERO_USAGE)}

    text, _searches, _errors = _parse_response(response)

    # Strip any narration before the emoji header (Haiku sometimes adds preamble).
    idx = text.find(topic["emoji"])
    if idx > 0:
        text = text[idx:].strip()
    elif idx == -1 and text:
        text = header + "\n" + text  # model omitted the header; restore it

    # Parse out HEADLINE, Sources, and bullets.
    headline = ""
    sources = []
    body_lines = []
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            continue
        upper = s.upper()
        if upper.startswith("HEADLINE:"):
            headline = s.split(":", 1)[1].strip()
        elif upper.startswith("SOURCES:"):
            payload = s.split(":", 1)[1].strip()
            # Find every URL; the words before each are its label/outlet.
            prev_end = 0
            for m in re.finditer(r"https?://[^\s;|<>]+", payload):
                label = payload[prev_end:m.start()].strip(" ;|-,")
                url = m.group(0).rstrip(".,)>]")
                # Fall back to the outlet name we know for this URL.
                if not label or label.lower() in ("source", "url", "link"):
                    label = _outlet_for_url(url, url_to_outlet)
                sources.append({"label": label, "url": url})
                prev_end = m.end()
        else:
            body_lines.append(s)

    text = "\n".join(body_lines)
    if not headline:
        for s in body_lines:
            if s.startswith("- "):
                headline = s[2:].strip()
                break
    if not headline:
        headline = "no top story today"
    if not text:
        text = header + "\n- No content was returned for this topic today."

    return {"title": topic["title"], "header": header, "text": text,
            "headline": headline, "sources": sources,
            "search_ok": True, "usage": _usage_of(response)}


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


def build_main_text(sections, closing, now_london, weather_line="", currency_line=""):
    """Full plain-text brief: greeting + morning extras + sections + closing."""
    greeting = "Good morning. Here is your daily brief for {d}.".format(
        d=now_london.strftime("%A %-d %B %Y")
    )
    intro_lines = [greeting]
    if weather_line:
        intro_lines.append(weather_line)
    if currency_line:
        intro_lines.append(currency_line)
    intro = "\n\n".join(intro_lines)
    body = "\n\n".join(s["text"] for s in sections)
    return intro + "\n\n" + body + "\n\n" + closing


def build_short_text(sections, closing, now_london, weather_line=""):
    """The short spoken brief: greeting + optional weather + one headline per
    topic + closing. Sections with skip_spoken=True (e.g. HN) are skipped."""
    greeting = "Good morning. Here are your headlines for {d}.".format(
        d=now_london.strftime("%A %-d %B %Y")
    )
    parts = [greeting]
    if weather_line:
        parts.append(weather_line)
    for s in sections:
        if s.get("skip_spoken"):
            continue
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
 .sources {{ margin: .35rem 0 0; font-size: .85rem; color: #888; }}
 .sources a {{ color: #0a7; text-decoration: none; margin: 0 .25rem; }}
 .sources a:hover {{ text-decoration: underline; }}
 .morning {{ margin: 0 0 1.6rem; padding: .7rem 1rem; background: #f4f6f8;
            border-radius: 10px; font-size: .95rem; }}
 .morning p {{ margin: .15rem 0; }}
 li a {{ color: #0a7; text-decoration: none; }}
 li a:hover {{ text-decoration: underline; }}
 .closing {{ margin-top: 2rem; padding: 1rem 1.1rem; background: #f4f6f8;
            border-radius: 12px; font-weight: 600; }}
 .foot {{ margin-top: 2.5rem; color: #999; font-size: .8rem; }}
 @media (prefers-color-scheme: dark) {{
   body {{ background: #000; color: #eee; }}
   .closing, .morning {{ background: #1c1c1e; }}
   .date, .foot, .sources {{ color: #888; }}
   .sources a, li a {{ color: #4cd9a9; }}
 }}
</style>
</head>
<body>
<h1>Your Daily Brief</h1>
<p class="date">{date}</p>
{morning}{body}
<p class="closing">{closing}</p>
<p class="foot">Updated {updated}. Generated automatically each weekday.</p>
</body>
</html>
"""


def _bullet_html(bullet_text):
    """Render a bullet to HTML. If it ends with '<URL>', the bullet text is
    rendered as a clickable link to that URL."""
    m = re.search(r"\s<(https?://[^>]+)>$", bullet_text)
    if m:
        text_part = bullet_text[:m.start()].strip().lstrip('"').rstrip('"')
        url = m.group(1)
        return '<li><a href="{u}" target="_blank" rel="noopener">{t}</a></li>'.format(
            u=html.escape(url, quote=True), t=html.escape(text_part))
    return "<li>{}</li>".format(html.escape(bullet_text))


def _sources_html(sources, inline_style=""):
    """Render a section's sources as clickable links."""
    if not sources:
        return ""
    links = [
        '<a href="{u}" target="_blank" rel="noopener">{l}</a>'.format(
            u=html.escape(src["url"], quote=True),
            l=html.escape(src["label"]),
        )
        for src in sources
    ]
    style = ' style="{}"'.format(inline_style) if inline_style else ""
    cls = "" if inline_style else ' class="sources"'
    return '<p{cls}{style}>Sources: {body}</p>'.format(
        cls=cls, style=style, body=" · ".join(links))


def build_html(sections, closing, now_london, weather_line="", currency_line=""):
    morning_html = ""
    if weather_line or currency_line:
        bits = []
        if weather_line:
            bits.append("<p>☀️ {}</p>".format(html.escape(weather_line)))
        if currency_line:
            bits.append("<p>💱 {}</p>".format(html.escape(currency_line)))
        morning_html = '<div class="morning">' + "".join(bits) + "</div>\n"

    blocks = []
    for s in sections:
        lines = [ln.strip() for ln in s["text"].split("\n") if ln.strip()]
        header = lines[0] if lines else s["title"]
        bullets = "".join(_bullet_html(ln[2:].strip() if ln.startswith("- ") else ln)
                          for ln in lines[1:])
        sources_html = _sources_html(s.get("sources", []))
        blocks.append("<section><h2>{h}</h2><ul>{b}</ul>{s}</section>".format(
            h=html.escape(header), b=bullets, s=sources_html))
    return HTML_TEMPLATE.format(
        date=now_london.strftime("%A %-d %B %Y"),
        morning=morning_html,
        body="\n".join(blocks),
        closing=html.escape(closing),
        updated=now_london.strftime("%H:%M %Z"),
    )


def _email_bullet(bullet_text):
    """Bullet HTML for email, with inline link if URL is embedded."""
    m = re.search(r"\s<(https?://[^>]+)>$", bullet_text)
    if m:
        text_part = bullet_text[:m.start()].strip().lstrip('"').rstrip('"')
        url = m.group(1)
        return ('<li style="margin:0 0 6px;">'
                '<a href="{u}" style="color:#0a7;text-decoration:none;">{t}</a></li>'
                ).format(u=html.escape(url, quote=True), t=html.escape(text_part))
    return '<li style="margin:0 0 6px;">{}</li>'.format(html.escape(bullet_text))


def build_email_html(sections, closing, now_london, weather_line="", currency_line=""):
    """Full brief as an email body with inline styles (robust across clients)."""
    date_str = now_london.strftime("%A %-d %B %Y")
    p = ['<div style="font-family:-apple-system,Segoe UI,Arial,sans-serif;'
         'max-width:640px;margin:0 auto;color:#1c1c1e;line-height:1.5;">']
    p.append('<h1 style="font-size:20px;margin:0 0 2px;">Your Daily Brief</h1>')
    p.append('<p style="color:#666;margin:0 0 20px;font-size:14px;">{}</p>'.format(
        html.escape(date_str)))
    if weather_line or currency_line:
        p.append('<div style="margin:0 0 16px;padding:10px 14px;background:#f4f6f8;'
                 'border-radius:8px;font-size:14px;">')
        if weather_line:
            p.append('<p style="margin:2px 0;">☀️ {}</p>'.format(
                html.escape(weather_line)))
        if currency_line:
            p.append('<p style="margin:2px 0;">\U0001F4B1 {}</p>'.format(
                html.escape(currency_line)))
        p.append('</div>')
    for s in sections:
        lines = [ln.strip() for ln in s["text"].split("\n") if ln.strip()]
        header = lines[0] if lines else s["title"]
        p.append('<h2 style="font-size:16px;margin:18px 0 6px;">{}</h2>'.format(
            html.escape(header)))
        p.append('<ul style="margin:0;padding-left:18px;">')
        for ln in lines[1:]:
            b = ln[2:].strip() if ln.startswith("- ") else ln
            p.append(_email_bullet(b))
        p.append('</ul>')
        srcs = _sources_html(
            s.get("sources", []),
            inline_style="margin:6px 0 0;font-size:13px;color:#888;",
        )
        if srcs:
            p.append(srcs)
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
    body = {
        "from": EMAIL_FROM,
        "to": [EMAIL_TO],
        "subject": subject,
        "html": html_body,
    }
    if EMAIL_CC:
        body["cc"] = EMAIL_CC
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "User-Agent": _UA,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            logger.info("Email sent to %s (HTTP %s).", EMAIL_TO, resp.status)
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8")
        except Exception:  # noqa: BLE001
            detail = ""
        logger.error("Email send failed: HTTP %s — %s", exc.code, detail)
    except Exception as exc:  # noqa: BLE001 — email failure must not fail the run
        logger.error("Email send failed: %s", exc)


def _tts(text, voice, out_path, key):
    """One OpenAI TTS call -> MP3 file. Raises on failure."""
    payload = json.dumps({
        "model": OPENAI_TTS_MODEL,
        "voice": voice,
        "input": text,
        "instructions": OPENAI_TTS_INSTRUCTIONS,
        "response_format": "mp3",
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/audio/speech",
        data=payload,
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json",
                 "User-Agent": _UA},
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        out_path.write_bytes(resp.read())


def generate_audio_segments(text):
    """Generate one MP3 per segment of `text`, alternating across the voices in
    OPENAI_TTS_VOICES, plus an ffmpeg concat list (output/segments.txt). The
    workflow stitches + transcodes them into brief.mp3. Returns True on success;
    no-op (False) if OPENAI_API_KEY isn't set, so the skill falls back to text."""
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        logger.info("OPENAI_API_KEY not set; skipping audio generation.")
        return False

    voices = [v.strip() for v in OPENAI_TTS_VOICES.split(",") if v.strip()] or [OPENAI_TTS_VOICE]
    segments = [s.strip() for s in text.split("\n\n") if s.strip()]

    # Clear any stale segment files (matters for local re-runs).
    for old in list(OUTPUT_DIR.glob("seg_*.mp3")) + list(OUTPUT_DIR.glob("segments.txt")):
        old.unlink()

    manifest = []
    for i, seg in enumerate(segments):
        voice = voices[i % len(voices)]
        out = OUTPUT_DIR / "seg_{:03d}.mp3".format(i)
        try:
            _tts(seg, voice, out, key)
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8")[:300]
            except Exception:  # noqa: BLE001
                detail = ""
            logger.error("TTS failed (segment %d, %s): HTTP %s — %s", i, voice, exc.code, detail)
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("TTS failed (segment %d, %s): %s", i, voice, exc)
            return False
        manifest.append("file '{}'".format(out.name))
        logger.info("TTS segment %d/%d (voice=%s).", i + 1, len(segments), voice)

    (OUTPUT_DIR / "segments.txt").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    logger.info("Audio: %d segments alternating voices %s.", len(segments), voices)
    return True


def write_outputs(short_text, full_text, sections, closing, audio_url,
                  weather_line, currency_line, now_london, now_utc):
    date_compact = now_london.strftime("%Y%m%d")

    txt_path = OUTPUT_DIR / "{d}_brief.txt".format(d=date_compact)
    txt_path.write_text(full_text, encoding="utf-8")

    feed = {
        "uid": "daily-brief-{d}".format(d=date_compact),
        "updateDate": now_utc.strftime("%Y-%m-%dT%H:%M:%S.0Z"),
        "titleText": "Your Daily Brief",
        "mainText": short_text,   # what Alexa speaks — short headlines
        "fullText": full_text,    # full detail (also rendered on the web page)
        "audioUrl": audio_url,    # MP3 of the short brief (OpenAI TTS); "" if none
        "redirectionUrl": REDIRECT_URL,
    }
    feed_path = OUTPUT_DIR / "alexa_feed.json"
    feed_path.write_text(json.dumps(feed, ensure_ascii=False, indent=2), encoding="utf-8")

    html_path = OUTPUT_DIR / "index.html"
    html_path.write_text(
        build_html(sections, closing, now_london,
                   weather_line=weather_line, currency_line=currency_line),
        encoding="utf-8",
    )

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

    todays_topics = select_topics(now_london)
    logger.info("Today's topics: %s", ", ".join(t["title"] for t in todays_topics))
    sections = []
    for topic in todays_topics:
        logger.info("Generating section: %s", topic["title"])
        sections.append(generate_section(client, topic, date_str))

    # Always-on extras: Hacker News top 3 at the end of every brief.
    hn = fetch_hn_section(count=3)
    if hn is not None:
        sections.append(hn)

    # Free one-line morning extras (weather + currency).
    weather_line = fetch_weather()
    currency_line = fetch_currency()
    if weather_line:
        logger.info("Weather: %s", weather_line)
    if currency_line:
        logger.info("Currency: %s", currency_line)

    interim = "\n\n".join(s["text"] for s in sections)
    closing, closing_usage = generate_closing(client, interim, date_str)

    full_text = build_main_text(sections, closing, now_london,
                                weather_line=weather_line,
                                currency_line=currency_line)
    short_text = build_short_text(sections, closing, now_london,
                                  weather_line=weather_line)

    # Generate smooth spoken audio (OpenAI TTS), alternating voices per segment.
    # The workflow stitches + transcodes the segments into brief.mp3.
    audio_ok = generate_audio_segments(short_text)
    audio_url = ""
    if audio_ok:
        # Versioned query so Alexa fetches the fresh clip each run (no caching).
        audio_url = "{base}/{f}?v={v}".format(
            base=REDIRECT_URL.rstrip("/"), f=AUDIO_FILE,
            v=now_london.strftime("%Y%m%d%H%M"),
        )

    txt_path, feed_path, html_path = write_outputs(
        short_text, full_text, sections, closing, audio_url,
        weather_line, currency_line, now_london, now_utc,
    )

    # Email the full brief (no-op unless RESEND_API_KEY is set).
    send_email(
        "Your Daily Brief — {}".format(date_str),
        build_email_html(sections, closing, now_london,
                         weather_line=weather_line,
                         currency_line=currency_line),
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
