import base64
import re
import tempfile

from groq import Groq

from config import get_settings

_client: Groq | None = None


def _get_client() -> Groq:
    global _client
    if _client is None:
        _client = Groq(api_key=get_settings().groq_api_key)
    return _client


def transcribe(audio_b64: str) -> str:
    audio_bytes = base64.b64decode(audio_b64)
    with tempfile.NamedTemporaryFile(suffix='.webm', delete=False) as f:
        f.write(audio_bytes)
        fname = f.name
    with open(fname, 'rb') as af:
        result = _get_client().audio.transcriptions.create(
            model="whisper-large-v3-turbo",
            file=af,
            language="en",
            prompt="Higgs.",
        )
    text = result.text.strip()
    if len(text) < 3:
        return ""

    low = text.lower()

    HALLUCINATIONS = {
        "thank you", "thanks for watching", "bye", "goodbye",
        "subscribe", "you", ".", "thanks.",
    }
    if low in HALLUCINATIONS:
        return ""

    PROMPT_WORDS = {"higgs", "canvas", "uml", "diagram", "flowchart", "brainstorm", "sticky", "note"}
    words = set(re.sub(r"[^a-z ]", "", low).split())
    if len(words & PROMPT_WORDS) >= 4:
        return ""

    return text
