"""Name normalisation for matching athletes across sources.

Handles umlauts (Müller / Mueller / Muller), accents (Mériguet), hyphens (Lena-Marie),
and case. Aliases are stored in both the accent-stripped and the German-transliterated form,
so a query in either spelling hits an exact alias; typos fall through to trigram search.
"""

from __future__ import annotations

import re
import unicodedata

_GERMAN = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"})
_NON_WORD = re.compile(r"[^a-z0-9 ]+")
_SPACES = re.compile(r"\s+")


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.replace("ß", "ss"))
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def normalize(text: str) -> str:
    """'Lena-Marie Lütz' -> 'lena marie lutz'."""
    folded = strip_accents(text).lower().replace("-", " ").replace("'", "")
    return _SPACES.sub(" ", _NON_WORD.sub(" ", folded)).strip()


def german_transliteration(text: str) -> str:
    """'Müller' -> 'Mueller' (how the name is spelled where umlauts are unavailable)."""
    return text.translate(_GERMAN)


def name_variants(full_name: str) -> set[str]:
    """All normalised spellings we index for a name."""
    variants = {normalize(full_name), normalize(german_transliteration(full_name))}
    parts = normalize(full_name).split()
    if len(parts) >= 3:  # 'lena marie lutz' -> also 'lena lutz'
        variants.add(f"{parts[0]} {parts[-1]}")
    return {v for v in variants if v}


def handle_tokens(handle: str) -> str:
    """'alinamueller_' -> 'alinamueller' for comparing handles with names."""
    return re.sub(r"[^a-z]", "", handle.lower())


def name_in_handle(full_name: str, handle: str) -> float:
    """Share of name parts found in the handle, in any spelling (0..1)."""
    tokens = handle_tokens(handle)
    parts = [p for p in normalize(full_name).split() if len(p) > 1]
    if not parts:
        return 0.0
    variants = {
        p: {p, normalize(german_transliteration(w)).replace(" ", "")}
        for p, w in zip(parts, _original_parts(full_name), strict=False)
    }
    hits = sum(1 for p in parts if any(v in tokens for v in variants.get(p, {p})))
    return hits / len(parts)


def _original_parts(full_name: str) -> list[str]:
    return [p for p in re.split(r"[\s\-]+", full_name) if len(normalize(p)) > 1]
