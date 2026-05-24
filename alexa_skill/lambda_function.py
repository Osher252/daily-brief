"""
Alexa custom skill handler for "the lowdown" / "my daily brief".

Paste this into the Code tab (lambda_function.py) of an Alexa-hosted (Python)
custom skill, then Save + Deploy. Saying "Alexa, open the lowdown" makes Alexa
read the latest brief from the GitHub Pages feed.

Voice: uses a natural British neural voice with a newscaster delivery so it
doesn't sound robotic. Change VOICE / USE_NEWS_STYLE below to taste:
  British female: "Amy"   British male: "Arthur" or "Brian"
  US conversational vibe: set VOICE="Matthew" or "Joanna" and USE_NEWS_STYLE
  can stay True (newscaster) — those two also support a "conversational" style.

The Alexa-hosted Python environment already includes ask-sdk-core, so no extra
packages are needed. urllib / json / re / html are part of the standard library.
"""

import html
import json
import logging
import re
import urllib.request

import ask_sdk_core.utils as ask_utils
from ask_sdk_core.skill_builder import SkillBuilder
from ask_sdk_core.dispatch_components import (
    AbstractRequestHandler,
    AbstractExceptionHandler,
)

# The public feed produced by the daily GitHub Action.
FEED_URL = "https://osher252.github.io/daily-brief/alexa_feed.json"

# --- Voice / delivery settings -------------------------------------------- #
VOICE = "Joanna"             # US neural voice (supports the 'excited' emotion)
EMOTION = "excited"          # "excited" / "disappointed" / "" to disable
EMOTION_INTENSITY = "high"   # low | medium | high  (dial to "medium" if too much)
USE_NEWS_STYLE = False       # newscaster (serious) — off; using emotion instead
# -------------------------------------------------------------------------- #

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def get_brief_text():
    """Fetch the brief as plain text (emojis removed, newlines kept)."""
    try:
        req = urllib.request.Request(FEED_URL, headers={"User-Agent": "alexa-skill"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data.get("mainText", "")
        text = re.sub(r"[^\x00-\x7f]", "", text)   # drop emojis / non-ASCII
        text = text.replace("&", " and ")
        text = re.sub(r"[ \t]+", " ", text).strip()  # tidy spaces, keep newlines
        return text or "Your daily brief is empty right now."
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to fetch brief: %s", exc)
        return ("Sorry, I couldn't fetch your daily brief right now. "
                "Please try again in a little while.")


def to_speech(text):
    """Wrap the brief in SSML: a natural voice, newscaster style, and pauses
    between sections so it reads like a presenter rather than a robot."""
    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
    rendered = []
    for block in blocks:
        lines = [html.escape(ln.strip(), quote=False)
                 for ln in block.split("\n") if ln.strip()]
        rendered.append(' <break time="300ms"/> '.join(lines))
    body = ' <break time="650ms"/> '.join(rendered) or html.escape(text, quote=False)

    if USE_NEWS_STYLE:
        body = '<amazon:domain name="news">' + body + '</amazon:domain>'
    if EMOTION:
        body = ('<amazon:emotion name="' + EMOTION + '" intensity="'
                + EMOTION_INTENSITY + '">' + body + '</amazon:emotion>')
    return '<voice name="' + VOICE + '">' + body + '</voice>'


class LaunchRequestHandler(AbstractRequestHandler):
    """'Alexa, open the lowdown' -> read the brief, then end."""

    def can_handle(self, handler_input):
        return ask_utils.is_request_type("LaunchRequest")(handler_input)

    def handle(self, handler_input):
        return (
            handler_input.response_builder
            .speak(to_speech(get_brief_text()))
            .set_should_end_session(True)
            .response
        )


class ReadAgainIntentHandler(AbstractRequestHandler):
    """Read the brief for repeat / navigate-home / fallback too."""

    def can_handle(self, handler_input):
        return (
            ask_utils.is_intent_name("AMAZON.FallbackIntent")(handler_input)
            or ask_utils.is_intent_name("AMAZON.NavigateHomeIntent")(handler_input)
            or ask_utils.is_intent_name("AMAZON.RepeatIntent")(handler_input)
        )

    def handle(self, handler_input):
        return (
            handler_input.response_builder
            .speak(to_speech(get_brief_text()))
            .set_should_end_session(True)
            .response
        )


class HelpIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("AMAZON.HelpIntent")(handler_input)

    def handle(self, handler_input):
        speech = "Just say, open the lowdown, to hear today's news."
        return handler_input.response_builder.speak(speech).ask(speech).response


class CancelOrStopIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return (
            ask_utils.is_intent_name("AMAZON.CancelIntent")(handler_input)
            or ask_utils.is_intent_name("AMAZON.StopIntent")(handler_input)
        )

    def handle(self, handler_input):
        return (
            handler_input.response_builder
            .speak("Goodbye.")
            .set_should_end_session(True)
            .response
        )


class SessionEndedRequestHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_request_type("SessionEndedRequest")(handler_input)

    def handle(self, handler_input):
        return handler_input.response_builder.response


class CatchAllExceptionHandler(AbstractExceptionHandler):
    def can_handle(self, handler_input, exception):
        return True

    def handle(self, handler_input, exception):
        logger.error("Unhandled exception: %s", exception, exc_info=True)
        speech = ("Sorry, something went wrong reading your brief. "
                  "Please try again.")
        return handler_input.response_builder.speak(speech).response


sb = SkillBuilder()
sb.add_request_handler(LaunchRequestHandler())
sb.add_request_handler(ReadAgainIntentHandler())
sb.add_request_handler(HelpIntentHandler())
sb.add_request_handler(CancelOrStopIntentHandler())
sb.add_request_handler(SessionEndedRequestHandler())
sb.add_exception_handler(CatchAllExceptionHandler())

lambda_handler = sb.lambda_handler()
