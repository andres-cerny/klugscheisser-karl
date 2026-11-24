"""Embeddings. `fake` is a deterministic hashed bag-of-words (lexical, good enough for tests);
`local` runs a multilingual sentence-transformers model on this machine (no key, no data leaves);
`gemini` uses the Gemini embedding API at 384 dims (no local model, fits a small server)."""

from __future__ import annotations

import hashlib
import math
import time
from itertools import pairwise
from typing import Protocol

from klugscheisser.config import Settings
from klugscheisser.domain.names import normalize


class Embedder(Protocol):
    model: str
    dim: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class FakeEmbedder:
    model = "fake-hash-384"

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        tokens = normalize(text).split()
        for tok in tokens + [a + b for a, b in pairwise(tokens)]:
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            v[h % self.dim] += 1.0 if (h >> 8) % 2 else -1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


class LocalEmbedder:
    def __init__(self, model: str) -> None:
        from sentence_transformers import SentenceTransformer

        self.model = model
        self._m = SentenceTransformer(model)
        self.dim = self._m.get_sentence_embedding_dimension()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        # e5 models expect role prefixes
        return self._m.encode([f"passage: {t}" for t in texts], normalize_embeddings=True).tolist()

    def embed_query(self, text: str) -> list[float]:
        return self._m.encode([f"query: {text}"], normalize_embeddings=True)[0].tolist()


class GeminiEmbedder:
    """Gemini API embeddings truncated to our 384-dim column (Matryoshka; re-normalised, because
    truncated vectors are not unit length). Needs GEMINI_API_KEY; texts leave this machine."""

    BATCH = 20

    def __init__(self, settings: Settings, client=None) -> None:
        if client is None:
            from google import genai

            client = genai.Client(api_key=settings.gemini_api_key.get_secret_value())
        self.client = client
        self.name = settings.gemini_embedding_model
        self.dim = settings.embedding_dim
        self.model = f"{self.name}-{self.dim}"  # stored in embeddings.model
        self.query_task = getattr(settings, "embedding_query_task", "RETRIEVAL_QUERY") or "RETRIEVAL_QUERY"

    def _embed(self, texts: list[str], task: str) -> list[list[float]]:
        from google.genai import types

        out: list[list[float]] = []
        for i in range(0, len(texts), self.BATCH):
            for attempt in range(6):  # per-minute quota: wait and retry instead of failing the ingest
                try:
                    r = self.client.models.embed_content(
                        model=self.name,
                        contents=[t[:8000] or " " for t in texts[i : i + self.BATCH]],
                        config=types.EmbedContentConfig(task_type=task, output_dimensionality=self.dim),
                    )
                    break
                except Exception as exc:
                    if "429" not in str(exc) or attempt == 5 or "PerDay" in str(exc):
                        raise  # daily quota: waiting does not help
                    time.sleep(10 * (attempt + 1))
            for e in r.embeddings:
                norm = math.sqrt(sum(x * x for x in e.values)) or 1.0
                out.append([x / norm for x in e.values])
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, "RETRIEVAL_DOCUMENT")

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], self.query_task)[0]


class GeminiChunkEmbedder:
    """gemini-embedding-2 for passage vectors (item_chunks, RETRIEVAL_CHUNKS). No task_type: the task goes into
    the text (`task: search result | query: …`; documents come pre-formatted as `title: … | text: …`, see
    embed/chunks.py). Every text is its own Content (several parts in one Content would give ONE aggregated
    vector). Output is normalised by the API for 768 dims; we normalise again (cheap, harmless)."""

    BATCH = 100
    chunked = True

    def __init__(self, settings: Settings, client=None) -> None:
        if client is None:
            from google import genai

            client = genai.Client(api_key=settings.gemini_api_key.get_secret_value())
        self.client = client
        self.name = settings.chunk_embedding_model
        self.dim = settings.chunk_embedding_dim
        self.model = f"{self.name}-{self.dim}"  # stored in item_chunks.model
        self.task = settings.chunk_query_task or "search result"

    def _embed(self, texts: list[str]) -> list[list[float]]:
        from google.genai import types

        out: list[list[float]] = []
        for i in range(0, len(texts), self.BATCH):
            contents = [
                types.Content(parts=[types.Part.from_text(text=t[:24000] or " ")])
                for t in texts[i : i + self.BATCH]
            ]
            for attempt in range(6):
                try:
                    r = self.client.models.embed_content(
                        model=self.name,
                        contents=contents,
                        config=types.EmbedContentConfig(output_dimensionality=self.dim),
                    )
                    break
                except Exception as exc:
                    if (
                        ("429" not in str(exc) and "503" not in str(exc))
                        or attempt == 5
                        or "PerDay" in str(exc)
                    ):
                        raise
                    time.sleep(10 * (attempt + 1))
            if len(r.embeddings) != len(contents):
                raise RuntimeError(f"embedding count {len(r.embeddings)} != {len(contents)}")
            for e in r.embeddings:
                norm = math.sqrt(sum(x * x for x in e.values)) or 1.0
                out.append([x / norm for x in e.values])
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._embed([f"task: {self.task} | query: {text}"])[0]


def get_chunk_embedder(settings: Settings):
    """The passage embedder (RETRIEVAL_CHUNKS) or None without Gemini key."""
    if not settings.features["gemini"]:
        return None
    return GeminiChunkEmbedder(settings)


def get_embedder(settings: Settings) -> Embedder:
    if settings.embedding_provider == "local":
        return LocalEmbedder(settings.embedding_model)
    if settings.embedding_provider == "gemini":
        if not settings.features["gemini"]:
            raise ValueError("EMBEDDING_PROVIDER=gemini needs GEMINI_API_KEY")
        return GeminiEmbedder(settings)
    return FakeEmbedder(settings.embedding_dim)
