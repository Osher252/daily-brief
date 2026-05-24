"""
Alexa custom skill handler for "my daily brief".

Paste this into the Code tab (lambda_function.py) of an Alexa-hosted (Python)
custom skill, then Save + Deploy. Saying "Alexa, open my daily brief" makes
Alexa read the latest brief from the GitHub Pages feed.

The Alexa-hosted Python environment already includes ask-sdk-core, so no extra
packages are needed. urllib / json / re are part of the standard library.
"""

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

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def get_brief_text():
    """Fetch the brief and clean it for text-to-speech."""
    try:
        req = urllib.request.Request(FEED_URL, headers={"User-Agent": "alexa-skill"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data.get("mainText", "")
        # Remove emojis / any non-ASCII so Alexa doesn't read them aloud.
        text = re.sub(r"[^\x00-\x7f]", "", text)
        text = text.replace("&", " and ")
        text = re.sub(r"[ \t]+", " ", text).strip()
        return text or "Your daily brief is empty right now."
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to fetch brief: %s", exc)
        return ("Sorry, I couldn't fetch your daily brief right now. "
                "Please try again in a little while.")


class LaunchRequestHandler(AbstractRequestHandler):
    """'Alexa, open my daily brief' -> read the brief, then end."""

    def can_handle(self, handler_input):
        return ask_utils.is_request_type("LaunchRequest")(handler_input)

    def handle(self, handler_input):
        speech = get_brief_text()
        return (
            handler_input.response_builder
            .speak(speech)
            .set_should_end_session(True)
            .response
        )


class ReadAgainIntentHandler(AbstractRequestHandler):
    """Also read the brief for navigate-home / fallback so it 'just works'."""

    def can_handle(self, handler_input):
        return (
            ask_utils.is_intent_name("AMAZON.FallbackIntent")(handler_input)
            or ask_utils.is_intent_name("AMAZON.NavigateHomeIntent")(handler_input)
            or ask_utils.is_intent_name("AMAZON.RepeatIntent")(handler_input)
        )

    def handle(self, handler_input):
        return (
            handler_input.response_builder
            .speak(get_brief_text())
            .set_should_end_session(True)
            .response
        )


class HelpIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return ask_utils.is_intent_name("AMAZON.HelpIntent")(handler_input)

    def handle(self, handler_input):
        speech = "Just say, open my daily brief, to hear today's news."
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
