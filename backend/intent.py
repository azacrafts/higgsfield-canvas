"""Intent classifier — decides if a transcript is a canvas command."""

import logging

import anthropic

from agent.prompts import CLASSIFIER_SYSTEM
from config import get_settings

logger = logging.getLogger(__name__)

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=get_settings().anthropic_api_key)
    return _client


def is_canvas_command(transcript: str) -> bool:
    """Return True if the transcript looks like a canvas command."""
    response = _get_client().messages.create(
        model=get_settings().classifier_model,
        max_tokens=5,
        system=CLASSIFIER_SYSTEM,
        messages=[{"role": "user", "content": transcript}],
    )
    result = response.content[0].text.strip().upper().startswith("YES")
    logger.debug("classifier '%s' → %s", transcript[:60], result)
    return result
