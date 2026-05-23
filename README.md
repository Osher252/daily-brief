# Daily News Briefing → Alexa Flash Briefing

Generates a personalised daily news brief across six topics using the Anthropic
API with live web search, then serves it as an Alexa Flash Briefing feed.

The brief is written for one specific reader (a Walthamstow-based, half-Israeli /
half-British fractional go-to-market consultant — see the profile in `main.py`).
Six sections each day:

1. AI models and products
2. UK personal finance
3. Israeli politics
4. UK politics
5. Sleep and wearables
6. B2B SaaS and go-to-market

Each section is 3–5 tight bullets (one fact + its implication), followed by one
sharp closing question. Total ~400–600 words, plain text, written for Alexa's
text-to-speech.

## How it works

- `main.py` makes one web-search API call per topic (so one failing topic can't
  kill the whole brief), assembles the sections, then makes one more call to
  write the closing question. It writes two files into `output/`:
  - `YYYYMMDD_brief.txt` — the plain-text brief
  - `alexa_feed.json` — the Alexa Flash Briefing feed
- `server.py` is a FastAPI app that serves `alexa_feed.json` at `GET /alexa-brief`.
- `scheduler.py` runs `main.run()` every day at 07:00 Europe/London.

## Requirements

- Python 3.11+ recommended (the code also runs on 3.9).
- An Anthropic API key.

## Run locally

```bash
cd ~/Desktop/daily-brief

# 1. Create a virtual environment and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Add your API key
cp .env.example .env
#   then edit .env and set ANTHROPIC_API_KEY=sk-ant-...

# 3. Generate today's brief (prints to stdout, writes to output/)
python main.py

# 4. Serve the Alexa feed
uvicorn server:app --host 0.0.0.0 --port 8000
#   then open http://localhost:8000/alexa-brief
```

Choose the model with `BRIEF_MODEL` in `.env`:
- `claude-sonnet-4-6` (default) — strong and cost-effective for a daily run.
- `claude-opus-4-7` — maximum quality.

## Scheduling

### Option A — the bundled scheduler (foreground process)

```bash
source .venv/bin/activate
python scheduler.py          # runs daily at 07:00 Europe/London
RUN_ON_START=1 python scheduler.py   # also generate one immediately (testing)
```

Keep it alive with `launchd` (macOS), `tmux`, `nohup`, or a systemd service.

### Option B — system crontab (recommended for an always-on box)

Edit your crontab with `crontab -e` and add (adjust the absolute paths):

```cron
# Generate the brief at 07:00 London time. Setting TZ makes cron interpret
# the schedule in London time regardless of the server's locale.
TZ=Europe/London
0 7 * * * cd /Users/phangan/Desktop/daily-brief && .venv/bin/python main.py >> output/cron.log 2>&1
```

Weekdays only? Change the day-of-week field: `0 7 * * 1-5`.

Each run is timestamped in `output/run.log`, including how many topics returned
live web results.

## Deploy

You need (a) something that runs `main.py` daily, and (b) the feed served over
HTTPS (Alexa requires HTTPS).

### Recommended: GitHub Actions + GitHub Pages (no server, free)

This repo ships with `.github/workflows/daily-brief.yml`, which does both:

- Runs daily at **07:00 Europe/London** (it fires at 06:00 and 07:00 UTC and a
  "gate" job only proceeds when it is actually 7am in London, so daylight saving
  is handled automatically). You can also trigger it manually from the
  repository's **Actions** tab ("Run workflow").
- Generates the brief, copies `output/alexa_feed.json` to `docs/alexa_feed.json`,
  and commits it back to the repo.
- **GitHub Pages** serves `docs/` over HTTPS, so the feed is available at
  `https://<user>.github.io/<repo>/alexa_feed.json`.

One-time setup:

1. Push this repo to GitHub (public, so free GitHub Pages can serve it).
2. Repo **Settings → Secrets and variables → Actions → New repository secret**:
   name `ANTHROPIC_API_KEY`, value your key.
3. Repo **Settings → Pages**: Source = "Deploy from a branch", Branch = `main`,
   Folder = `/docs`.
4. Run the workflow once from the **Actions** tab to generate the first brief.

Why not run the generation on a serverless platform (e.g. Vercel functions)?
The six topics are fetched sequentially (to stay under entry-tier API rate
limits) and take ~5 minutes, which bumps into typical serverless time limits.
GitHub Actions has no such limit, so it is the better fit for the generation
step. Any static host (including Vercel/Netlify) is fine for *serving* the
finished JSON file.

### Alternative: always-on server (VPS / Raspberry Pi)
1. Clone the project, create the venv, set `.env`.
2. Add the crontab entry above for daily generation.
3. Run the server: `uvicorn server:app --host 0.0.0.0 --port 8000`
   (use a process manager: systemd, pm2, or `tmux`).
4. Put HTTPS in front of it. Easiest is Caddy:
   ```
   brief.example.com {
       reverse_proxy localhost:8000
   }
   ```
   Or a Cloudflare Tunnel / `ngrok http 8000` for a quick HTTPS URL.

**Serverless note:** because the feed is just a static JSON file once generated,
you can also have the cron job upload `output/alexa_feed.json` to any static host
(S3 + CloudFront, Vercel, GitHub Pages) and point Alexa at that URL — then you
don't need `server.py` running at all.

## Connect to Alexa Flash Briefing

1. Go to the [Alexa Developer Console](https://developer.amazon.com/alexa/console/ask)
   and create a new skill → skill type **Flash Briefing**.
2. Add a new feed under **Flash Briefing**:
   - **Preamble**: e.g. "Here is your daily brief."
   - **Name**: Your Daily Brief
   - **Content type**: Text
   - **Content update frequency**: Daily
   - **Feed URL**: your HTTPS URL ending in `/alexa-brief`
     (for example `https://brief.example.com/alexa-brief`).
3. Save and run the validator. Fix any feed errors it reports.
4. Enable the skill on your account, then ask: "Alexa, what's my Flash Briefing?"

### Feed format notes

The feed served at `/alexa-brief` is a single Flash Briefing item:

```json
{
  "uid": "daily-brief-20260523",
  "updateDate": "2026-05-23T06:00:00.0Z",
  "titleText": "Your Daily Brief",
  "mainText": "Good morning. Here is your daily brief...",
  "redirectionUrl": "https://osher252.github.io/daily-brief/"
}
```

- `updateDate` is UTC in ISO 8601 (Alexa shows newer items first).
- `redirectionUrl` **must be a valid, absolute https URL** — Alexa rejects an
  empty string. It defaults to the GitHub Pages site; override with the
  `BRIEF_REDIRECT_URL` environment variable.
- `mainText` for a text item **must be 4500 characters or fewer** — Alexa
  rejects longer items. `main.py` keeps each topic to 3 short bullets and
  hard-trims the body to a sentence boundary under `MAX_MAINTEXT_CHARS` (4400)
  as a safety net.
- If your skill insists on a list feed, change the last line of `server.py` to
  `return JSONResponse([data])`.

## Error handling

- If a topic's web search fails, that section is still emitted with a note rather
  than crashing the run.
- If a whole topic's API call throws, the section says news is unavailable today.
- If `ANTHROPIC_API_KEY` is missing, the run exits with a clear message.
- Every run is logged to `output/run.log` with timestamps and per-topic search
  status.

## File structure

```
daily-brief/
  main.py            # generates the brief
  server.py          # serves the Flash Briefing endpoint
  scheduler.py       # daily scheduler (07:00 Europe/London)
  requirements.txt
  .env.example       # copy to .env and add your key
  README.md
  output/
    alexa_feed.json  # generated
    YYYYMMDD_brief.txt
    run.log
```
