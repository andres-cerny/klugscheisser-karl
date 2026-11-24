"""LLM providers (Claude, Gemini, fake) behind one small interface.

Used for: fallback classification of uncertain items, image descriptions at ingest,
and LLM reranking. Answers and other texts go through llm/text.py (same LLM_PROVIDER setting).
"""

from __future__ import annotations

import hashlib
import json
from typing import Protocol

import httpx

from klugscheisser.config import Settings
from klugscheisser.llm.text import LlmRefusal, claude_message  # noqa: F401 (LlmRefusal re-exported)

IMAGE_PROMPT = (
    "Beschreibe sachlich in einem deutschen Satz, was auf diesem Instagram-Bild zu sehen ist "
    "(Personen, Ort, Situation, sichtbarer Text). Keine Wertungen, keine Vermutungen über Gefühle, "
    "keine Namen ausser sie stehen im Bild oder in der Bildunterschrift. Bildunterschrift: {caption}"
)


class LlmProvider(Protocol):
    name: str
    model: str

    def complete_json(self, system: str, user: str, schema: dict) -> dict: ...

    def describe_image(self, image_url: str, caption: str) -> str | None: ...


class ClaudeProvider:
    """Claude Sonnet (v17) for the classification fallback, reranking and image descriptions."""

    name = "claude"

    def __init__(self, settings: Settings, client=None) -> None:
        self.settings = settings
        self.client = client  # None = the shared client of llm/text.py
        self.model = settings.anthropic_model
        self.last_usage: dict = {}

    def message(
        self, messages: list[dict], max_tokens: int, schema: dict | None = None, system: str | None = None
    ):
        text, self.last_usage = claude_message(
            self.settings,
            messages,
            system=system,
            schema=schema,
            model_id=self.model,
            max_tokens=max_tokens,
            client=self.client,
        )
        return text

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        return json.loads(self.message([{"role": "user", "content": user}], 2000, schema, system))

    def describe_image(self, image_url: str, caption: str) -> str | None:
        text = self.message(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "url", "url": image_url}},
                        {"type": "text", "text": IMAGE_PROMPT.format(caption=caption[:500])},
                    ],
                }
            ],
            1000,  # room for (rare) thinking at low effort
        )
        return text.strip() or None


class GeminiProvider:
    name = "gemini"

    def __init__(self, settings: Settings) -> None:
        from google import genai

        self.client = genai.Client(api_key=settings.gemini_api_key.get_secret_value())
        self.model = settings.gemini_text_model  # images/reels use media/analyzer.py with gemini_model

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        from google.genai import types

        response = self.client.models.generate_content(
            model=self.model,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_json_schema=schema,
            ),
        )
        return json.loads(response.text)

    def describe_image(self, image_url: str, caption: str) -> str | None:
        from google.genai import types

        image = httpx.get(image_url, timeout=30, follow_redirects=True)
        image.raise_for_status()
        response = self.client.models.generate_content(
            model=self.model,
            contents=[
                types.Part.from_bytes(
                    data=image.content, mime_type=image.headers.get("content-type", "image/jpeg")
                ),
                IMAGE_PROMPT.format(caption=caption[:500]),
            ],
        )
        return (response.text or "").strip() or None


class FakeLlmProvider:
    """Deterministic stand-in for tests: answers from simple keyword rules."""

    name = "fake"
    model = "fake-1"

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        from klugscheisser.classify.rules import RuleBasedClassifier

        if "category" in schema.get("properties", {}):
            r = RuleBasedClassifier().classify_text(user)
            return {
                "relevant": r.relevant_p >= 0.5,
                "category": r.category,
                "sensitive": r.sensitive_p >= 0.5,
                "newsworthiness": r.newsworthiness,
                "storyline": r.storyline or "none",
                "mood": r.mood or "neutral",
            }
        if "scores" in schema.get("properties", {}):
            return {"scores": []}
        return {}

    def describe_image(self, image_url: str, caption: str) -> str | None:
        digest = hashlib.sha1(image_url.encode()).hexdigest()[:6]
        return f"[Testbild {digest}]"


def get_llm(settings: Settings) -> LlmProvider | None:
    """The configured provider, or None when no key is set (feature off)."""
    choice, f = settings.llm_provider, settings.features
    if choice == "fake":
        return FakeLlmProvider()
    if choice == "claude" or (choice == "auto" and f["claude"]):
        return ClaudeProvider(settings) if f["claude"] else None
    if choice == "gemini" or (choice == "auto" and f["gemini"]):
        return GeminiProvider(settings) if f["gemini"] else None
    return None
