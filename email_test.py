"""Tiny Resend diagnostic — sends one test email and prints the exact response.

Run via the 'Email Test' GitHub Action (workflow_dispatch). Costs nothing —
it does not call the Anthropic API.
"""

import json
import os
import urllib.error
import urllib.request

key = os.environ.get("RESEND_API_KEY")
frm = os.environ.get("BRIEF_EMAIL_FROM") or "Daily Brief <onboarding@resend.dev>"
to = os.environ.get("BRIEF_EMAIL_TO") or "imjohnny252@gmail.com"

print("from:", frm)
print("to:", to)
print("RESEND_API_KEY set:", bool(key))

payload = json.dumps({
    "from": frm,
    "to": [to],
    "subject": "Daily Brief — test email",
    "html": "<p>This is a test from your daily-brief setup. If you got this, email works.</p>",
}).encode("utf-8")

req = urllib.request.Request(
    "https://api.resend.com/emails",
    data=payload,
    headers={"Authorization": "Bearer " + (key or ""),
             "Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        print("SUCCESS HTTP", r.status, r.read().decode("utf-8"))
except urllib.error.HTTPError as e:
    print("HTTPError", e.code, "BODY:", e.read().decode("utf-8"))
except Exception as e:  # noqa: BLE001
    print("ERROR", repr(e))
