"""Generate short TTS samples for several OpenAI voices + a comparison page.

Run via the 'Voice Sampler' GitHub Action (workflow_dispatch). Produces
docs/voice-<name>.mp3 for each voice and docs/voices.html with play buttons,
so you can listen and pick a favourite. Costs a few cents total.
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

DOCS = Path(__file__).resolve().parent / "docs"
DOCS.mkdir(exist_ok=True)

VOICES = (os.environ.get("VOICES") or "nova,coral,shimmer,ash,onyx").split(",")
MODEL = os.environ.get("OPENAI_TTS_MODEL") or "gpt-4o-mini-tts"
INSTRUCTIONS = (os.environ.get("OPENAI_TTS_INSTRUCTIONS") or
    "Speak like a warm, upbeat morning news presenter: energetic and friendly, "
    "smooth and natural, with lively but clear pacing.")
SAMPLE = ("Good morning! Here are your headlines. Gemini 3.5 Flash just launched, "
          "running four times faster than rival models. Trading 212 is offering "
          "four point six percent on its cash ISA. And Reform UK has taken its "
          "first London council.")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

key = os.environ.get("OPENAI_API_KEY")
if not key:
    raise SystemExit("OPENAI_API_KEY not set")

made = []
for voice in [v.strip() for v in VOICES if v.strip()]:
    payload = json.dumps({
        "model": MODEL, "voice": voice, "input": SAMPLE,
        "instructions": INSTRUCTIONS, "response_format": "mp3",
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/audio/speech", data=payload,
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            (DOCS / "voice-{}.mp3".format(voice)).write_bytes(resp.read())
        print("wrote voice-{}.mp3".format(voice))
        made.append(voice)
    except urllib.error.HTTPError as exc:
        print("FAILED", voice, exc.code, exc.read().decode("utf-8")[:200])
    except Exception as exc:  # noqa: BLE001
        print("FAILED", voice, repr(exc))

players = "\n".join(
    '<div style="margin:18px 0;"><strong>{v}</strong><br>'
    '<audio controls preload="none" src="voice-{v}.mp3"></audio></div>'.format(v=v)
    for v in made)
page = """<!doctype html><html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Voice samples</title>
<style>body{{font-family:-apple-system,system-ui,sans-serif;max-width:32rem;
margin:2rem auto;padding:0 1rem;line-height:1.5;}}audio{{width:100%;margin-top:6px;}}</style>
</head><body><h1>Pick a voice</h1>
<p>Tap play on each, then tell Claude which name you like.</p>
{players}
</body></html>""".format(players=players)
(DOCS / "voices.html").write_text(page, encoding="utf-8")
print("wrote voices.html with", len(made), "voices")
