"""Confidence routing over classifiers.

- confident-irrelevant (and not sensitive): dropped from answers (text purged after retention period)
- uncertain: sent to the fallback classifier (LLM) if one is configured
- sensitive above a low threshold or a Vorsicht category: always kept and flagged
"""

from __future__ import annotations

import json
import logging

from klugscheisser.classify.base import (
    CATEGORIES,
    MOODS,
    STORYLINES,
    VORSICHT_CATEGORIES,
    ClassificationResult,
    Classifier,
    ClassifyInput,
    vorsicht_reason,
)
from klugscheisser.classify.rules import RuleBasedClassifier
from klugscheisser.config import Settings
from klugscheisser.llm.providers import LlmProvider

log = logging.getLogger(__name__)

LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "category": {"type": "string", "enum": CATEGORIES},
        "sensitive": {"type": "boolean"},
        "newsworthiness": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "storyline": {"type": "string", "enum": STORYLINES},
        "mood": {"type": "string", "enum": MOODS},
    },
    "required": ["relevant", "category", "sensitive", "newsworthiness", "storyline", "mood"],
    "additionalProperties": False,
}
LLM_SYSTEM = (
    "Du klassifizierst Inhalte (Instagram-Posts, Artikel) über {persons} ({topic}) für eine "
    "SRF-Sportmoderatorin. sensitive=true bei Verletzung, Krankheit, psychischer Gesundheit, Trauer, "
    "Beziehung/Familie, Schwangerschaft, Kontroverse, Sperre, Karriereende. Im Zweifel sensitive=true. "
    "newsworthiness 1 (belanglos) bis 5 (grosse Story)."
)


class LlmClassifier:
    name = "llm"

    def __init__(self, llm: LlmProvider) -> None:
        self.llm = llm
        self.version = f"{llm.name}:{llm.model}"

    def classify(self, item: ClassifyInput) -> ClassificationResult:
        from klugscheisser.projects import wording

        w = wording()
        system = LLM_SYSTEM.format(persons=w.persons, topic=w.topic)
        out = self.llm.complete_json(system, item.as_state(), LLM_SCHEMA)
        # an LLM gives labels, not probabilities: map to conservative pseudo-probabilities
        return ClassificationResult(
            relevant_p=0.8 if out["relevant"] else 0.15,
            category=out["category"],
            category_p=None,
            sensitive_p=0.8 if out["sensitive"] else 0.05,
            newsworthiness=int(out["newsworthiness"]),
            storyline=None if out["storyline"] == "none" else out["storyline"],
            mood=out.get("mood"),
            classifier=self.name,
            version=self.version,
        )


class ClassifierRouter:
    def __init__(
        self,
        primary: Classifier,
        fallback: Classifier | None,
        settings: Settings,
        outage: Classifier | None = None,
    ) -> None:
        # fallback = second opinion for uncertain items; outage = replacement when the primary fails
        self.primary, self.fallback, self.s = primary, fallback, settings
        self.outage = outage or fallback
        self.primary_disabled = False
        self.fallback_disabled = False

    def classify(self, item: ClassifyInput) -> ClassificationResult:
        result = None
        if not self.primary_disabled:
            try:
                result = self.primary.classify(item)
            except Exception as exc:  # a classifier outage must not stop ingest
                log.warning("primary classifier %s failed: %s", self.primary.name, exc)
                if " 401" in str(exc) or " 403" in str(exc):
                    self.primary_disabled = True  # circuit breaker: bad key, stop retrying this run
        if result is None:
            try:
                result = (self.outage or RuleBasedClassifier()).classify(item)
            except Exception as exc:  # e.g. LLM quota: last resort rules
                log.warning("outage classifier failed: %s", str(exc).split(".")[0][:160])
                result = RuleBasedClassifier().classify(item)
        uncertain = self.s.classify_irrelevant_below <= result.relevant_p < self.s.classify_uncertain_below
        if (
            uncertain
            and self.fallback is not None
            and self.fallback is not self.primary
            and not self.fallback_disabled
        ):
            try:
                second = self.fallback.classify(item)
                second.sensitive_p = max(second.sensitive_p, result.sensitive_p)  # never lose a flag
                result = second
            except Exception as exc:
                text = str(exc)
                if any(code in text for code in ("429", "RESOURCE_EXHAUSTED", " 401", " 403")):
                    self.fallback_disabled = True  # quota/auth: stop for this run, keep the primary result
                log.warning("fallback classifier %s failed: %s", self.fallback.name, text.split(".")[0][:160])
        # Jev / LLM give a calibrated (or explicit) sensitivity; the category shortcut is only for the rules fallback
        sensitive = result.sensitive_p >= self.s.sensitive_threshold
        if result.classifier == "rules":
            sensitive = sensitive or result.category in VORSICHT_CATEGORIES
        # a caution flag needs a stated reason (injury, controversy, private life …); otherwise it is dropped
        result.vorsicht = sensitive and vorsicht_reason(result) is not None
        result.relevant = result.vorsicht or result.relevant_p >= self.s.classify_irrelevant_below
        return result


def build_classifier(settings: Settings, llm: LlmProvider | None) -> ClassifierRouter:
    from klugscheisser.classify.jev import JevClassifier

    llm_cls = LlmClassifier(llm) if llm is not None else None
    if settings.features["jev"]:
        # Jev's calibrated probabilities need no second opinion; the LLM only replaces Jev during an outage,
        # so the model budget stays for answers and image/reel analysis.
        return ClassifierRouter(
            JevClassifier(settings), None, settings, outage=llm_cls or RuleBasedClassifier()
        )
    if llm_cls is not None:
        return ClassifierRouter(llm_cls, None, settings, outage=RuleBasedClassifier())
    return ClassifierRouter(RuleBasedClassifier(), None, settings)


def summary_json(result: ClassificationResult) -> str:
    return json.dumps(result.__dict__, ensure_ascii=False)
