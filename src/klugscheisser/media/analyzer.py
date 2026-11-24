"""MediaAnalyzer implementations: Gemini (images + reels), Claude (images only), fake (tests).

v17: reels need native video input, which only Gemini has, so MEDIA_PROVIDER=auto still prefers Gemini here.

Media bytes are downloaded from the transient CDN URL into memory, sent to the model and dropped;
nothing is written to disk or the database.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from klugscheisser.config import Settings
from klugscheisser.interfaces import MediaAnalysis, MediaRef

log = logging.getLogger(__name__)

MEDIA_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "spoken_quotes": {"type": "array", "items": {"type": "string"}},
        "visible_text": {"type": ["string", "null"]},
    },
    "required": ["description", "spoken_quotes", "visible_text"],
}

MEDIA_PROMPT = """\
Du analysierst die Bilder/Videos eines Instagram-Posts (Thema: {topic}) \
für eine SRF-Sportredaktion. Antworte als JSON:
- description: 1 bis 3 sachliche deutsche Sätze. Wer/was/wo ist zu sehen (Spiel, Training, Feier, \
privat, Werbung)? Nenne sichtbare Details wie Trikots und Teams, Medaillen, Pokale, Krücken, Verbände, \
Gips, Rollstuhl, Spital. Keine Vermutungen über Gefühle, keine Wertungen. Namen nur, wenn sie im \
Bild sichtbar sind oder in der Bildunterschrift stehen. Mehrere Bilder zusammen beschreiben.
- spoken_quotes: im Video gesprochene Sätze wörtlich in der Originalsprache (Deutsch, Englisch, \
Französisch), nicht übersetzen, nichts erfinden. Keine Liedtexte. Leere Liste bei Bildern oder \
wenn niemand spricht.
- visible_text: im Bild/Video sichtbarer Text (Einblendungen, Schilder, Plakate), sonst null.
Bildunterschrift: {caption}"""

USER_AGENT = "Mozilla/5.0 (compatible; klugscheisser/0.2; media analysis)"
RETRY_CODES = {429, 500, 503, 504}
MAX_TRIES = 3

TOO_LONG = "nicht analysiert (zu lang)"
TOO_BIG = "nicht analysiert (zu gross)"
DOWNLOAD_FAILED = "nicht analysiert (Download fehlgeschlagen)"
UNSUPPORTED = "nicht analysiert (Video vom Modell nicht unterstützt)"
NOT_ANALYZED = "nicht analysiert"


class MediaAnalysisError(Exception):
    pass


class MediaRateLimited(MediaAnalysisError):
    """The model kept answering 429/503 after all retries; try again later (not the item's fault)."""


class MediaDownloadError(MediaAnalysisError):
    """No media could be downloaded (CDN link expired or blocked)."""


@dataclass
class Blob:
    kind: str
    data: bytes
    mime: str


def plan_media(media: list[MediaRef], settings: Settings) -> tuple[list[MediaRef], list[str]]:
    """Which refs to analyse; skipped ones become notes like 'video: nicht analysiert (zu lang)'."""
    chosen: list[MediaRef] = []
    notes: list[str] = []
    images = 0
    for ref in media:
        if ref.kind == "video":
            if ref.duration_s is not None and ref.duration_s > settings.media_max_video_seconds:
                notes.append(f"video: {TOO_LONG}")
                continue
        else:
            if images >= settings.media_max_images:
                continue
            images += 1
        chosen.append(ref)
    return chosen, notes


def _mime(ref: MediaRef, header: str | None) -> str:
    mime = (header or "").split(";")[0].strip().lower()
    if mime.startswith(("image/", "video/")):
        return mime
    return "image/jpeg" if ref.kind == "image" else "video/mp4"


def download(http: httpx.Client, ref: MediaRef, max_bytes: int) -> Blob | None:
    """Fetch one media file into memory; None when it exceeds max_bytes."""
    with http.stream("GET", ref.url) as r:
        r.raise_for_status()
        if int(r.headers.get("content-length") or 0) > max_bytes:
            return None
        buf = bytearray()
        for chunk in r.iter_bytes():
            buf.extend(chunk)
            if len(buf) > max_bytes:
                return None
        return Blob(kind=ref.kind, data=bytes(buf), mime=_mime(ref, r.headers.get("content-type")))


def _http() -> httpx.Client:
    return httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": USER_AGENT})


def _parse(text: str, model: str, analyzed: list[str]) -> MediaAnalysis:
    data = json.loads(text or "{}")
    quotes = [q.strip() for q in data.get("spoken_quotes") or [] if q and q.strip()]
    visible = (data.get("visible_text") or "").strip() or None
    return MediaAnalysis(
        description=(data.get("description") or "").strip() or None,
        spoken_quotes=quotes,
        visible_text=visible,
        model=model,
        analyzed=analyzed,
    )


class GeminiMediaAnalyzer:
    """Images (up to 5) and reels (<= MEDIA_MAX_VIDEO_SECONDS) of one post in a single request."""

    name = "gemini"

    def __init__(
        self,
        settings: Settings,
        client=None,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if client is None:
            from google import genai

            client = genai.Client(api_key=settings.gemini_api_key.get_secret_value())
        self.client = client
        self.settings = settings
        self.model = settings.gemini_model
        # free tier: each model has its own daily request quota, so fall back along a chain
        self.models = list(dict.fromkeys([settings.gemini_model, *settings.media_fallback_models]))
        self._exhausted: dict[str, float] = {}  # model -> monotonic time its quota is back
        self.http = http or _http()
        self.sleep = sleep

    def analyze(self, media: list[MediaRef], caption: str) -> MediaAnalysis:
        from google.genai import types

        chosen, notes = plan_media(media, self.settings)
        inline_budget = int(self.settings.media_max_inline_mb * 1024 * 1024)
        upload_max = int(self.settings.media_max_upload_mb * 1024 * 1024)
        parts: list = []
        analyzed: list[str] = []
        uploaded: list = []
        failed = 0
        try:
            for ref in chosen:
                limit = upload_max if ref.kind == "video" else inline_budget
                try:
                    blob = download(self.http, ref, limit)
                except httpx.HTTPError as exc:
                    log.info("media download failed (%s): %s", ref.kind, exc)
                    notes.append(f"{ref.kind}: {DOWNLOAD_FAILED}")
                    failed += 1
                    continue
                if blob is None:
                    notes.append(f"{ref.kind}: {TOO_BIG}")
                    continue
                if len(blob.data) <= inline_budget:
                    parts.append(types.Part.from_bytes(data=blob.data, mime_type=blob.mime))
                    inline_budget -= len(blob.data)
                elif ref.kind == "video":
                    f = self._upload(blob)
                    uploaded.append(f)
                    parts.append(types.Part.from_uri(file_uri=f.uri, mime_type=blob.mime))
                else:
                    notes.append(f"{ref.kind}: {TOO_BIG}")
                    continue
                analyzed.append(ref.kind)
            if not parts:
                if failed and failed == len(chosen):
                    raise MediaDownloadError("no media could be downloaded (CDN link expired?)")
                return MediaAnalysis(description=None, model="", analyzed=notes)
            parts.append(_prompt(caption))
            config = types.GenerateContentConfig(
                response_mime_type="application/json",
                response_json_schema=MEDIA_SCHEMA,
                temperature=0.2,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            )
            model, response = self._generate(parts, config)
            out = _parse(response.text, model, analyzed + notes)
            usage = getattr(response, "usage_metadata", None)
            if usage is not None:  # v8: real cost goes to the ledger
                out.input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
                out.output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0) + int(
                    getattr(usage, "thoughts_token_count", 0) or 0
                )
            return out
        finally:
            for f in uploaded:
                try:
                    self.client.files.delete(name=f.name)
                except Exception as exc:  # cleanup only; files expire after 48 h anyway
                    log.info("could not delete uploaded file %s: %s", f.name, exc)

    def available_models(self) -> list[str]:
        now = time.monotonic()
        return [x for x in self.models if self._exhausted.get(x, 0) <= now]

    def _generate(self, parts: list, config):
        """Try each model in turn: retries with exponential backoff on 429/5xx (max 3 tries per model);
        a model whose free-tier daily quota is used up is skipped until its quota resets."""
        from google.genai import errors

        last = "no model available"
        for model in self.available_models():
            for attempt in range(MAX_TRIES):
                try:
                    response = self.client.models.generate_content(model=model, contents=parts, config=config)
                    return model, response
                except errors.APIError as exc:
                    last = f"{model}: Gemini {exc.code}: {exc.message}"
                    if exc.code not in RETRY_CODES:
                        raise MediaAnalysisError(last) from exc
                    delay = _retry_delay(exc)
                    if exc.code == 429 and (_is_daily_quota(exc) or (delay or 0) > 300):
                        self._exhausted[model] = time.monotonic() + (delay or 3600)
                        log.warning("Gemini daily quota used up for %s; next model", model)
                        break
                    if attempt == MAX_TRIES - 1:
                        log.warning("Gemini %s still failing after %s tries; next model", model, MAX_TRIES)
                        break
                    delay = min(60.0, delay or 4.0 * 2**attempt)
                    log.info("Gemini %s on %s, retry in %.1f s", exc.code, model, delay)
                    self.sleep(delay)
        raise MediaRateLimited(f"all Gemini models busy or out of quota ({last})")

    def _upload(self, blob: Blob):
        """Files API for videos above the inline limit; waits until the file is processed."""
        f = self.client.files.upload(file=io.BytesIO(blob.data), config={"mime_type": blob.mime})
        deadline = time.monotonic() + 120
        while getattr(f.state, "name", str(f.state)) == "PROCESSING":
            if time.monotonic() > deadline:
                raise MediaAnalysisError("Gemini file processing timed out")
            self.sleep(2)
            f = self.client.files.get(name=f.name)
        if getattr(f.state, "name", str(f.state)) == "FAILED":
            raise MediaAnalysisError("Gemini file processing failed")
        return f


def _is_daily_quota(exc) -> bool:
    try:
        for d in (exc.details or {}).get("error", {}).get("details", []):
            for v in d.get("violations") or []:
                if "PerDay" in str(v.get("quotaId", "")):
                    return True
    except (AttributeError, TypeError):
        return False
    return False


def _retry_delay(exc) -> float | None:
    """Honour the server's RetryInfo ('retryDelay': '17s') on 429 when present."""
    try:
        for d in (exc.details or {}).get("error", {}).get("details", []):
            if str(d.get("@type", "")).endswith("RetryInfo"):
                return float(str(d["retryDelay"]).rstrip("s"))
    except (AttributeError, TypeError, ValueError, KeyError):
        return None
    return None


class ClaudeMediaAnalyzer:
    """Images only (Claude has no video input); videos are noted as not analysed."""

    name = "claude"

    def __init__(self, settings: Settings, provider=None, http: httpx.Client | None = None) -> None:
        if provider is None:
            from klugscheisser.llm.providers import ClaudeProvider

            provider = ClaudeProvider(settings)
        self.provider = provider
        self.settings = settings
        self.model = provider.model
        self.http = http or _http()

    def analyze(self, media: list[MediaRef], caption: str) -> MediaAnalysis:
        chosen, notes = plan_media(media, self.settings)
        budget = int(self.settings.media_max_inline_mb * 1024 * 1024)
        content: list[dict] = []
        analyzed: list[str] = []
        failed = 0
        images = [r for r in chosen if r.kind == "image"]
        notes += [f"video: {UNSUPPORTED}" for r in chosen if r.kind == "video"]
        for ref in images:
            try:
                blob = download(self.http, ref, min(budget, 5 * 1024 * 1024))  # Claude: 5 MB per image
            except httpx.HTTPError:
                notes.append(f"image: {DOWNLOAD_FAILED}")
                failed += 1
                continue
            if blob is None:
                notes.append(f"image: {TOO_BIG}")
                continue
            data = base64.standard_b64encode(blob.data).decode()
            content.append(
                {"type": "image", "source": {"type": "base64", "media_type": blob.mime, "data": data}}
            )
            analyzed.append("image")
        if not content:
            if images and failed == len(images):
                raise MediaDownloadError("no media could be downloaded (CDN link expired?)")
            return MediaAnalysis(description=None, model="", analyzed=notes)
        content.append({"type": "text", "text": _prompt(caption)})
        text = self.provider.message([{"role": "user", "content": content}], 2000, MEDIA_SCHEMA)
        out = _parse(text, self.model, analyzed + notes)
        usage = getattr(self.provider, "last_usage", None) or {}
        out.input_tokens, out.output_tokens = int(usage.get("input", 0)), int(usage.get("output", 0))
        return out


CLI_SYSTEM = (
    "Du beschreibst Bilder von Instagram-Posts sachlich für eine Sportredaktion. Antworte nur mit dem "
    "verlangten JSON, ohne Erklärungen."
)
CLI_FRAMES = (0.15, 0.5, 0.85)  # where in a reel the still frames are taken


class ClaudeCliMediaAnalyzer:
    """v15 (08.10.2026): Claude via the Claude Code CLI of Ondřej's subscription (`claude -p`), no API key.
    Several posts per call (`analyze_batch`), images as files the CLI reads; reels as three still frames
    (ffmpeg, no sound → no spoken quotes). Media are written to a temp folder only for the call and deleted."""

    name = "claude-cli"

    def __init__(self, settings: Settings, http: httpx.Client | None = None, run=None) -> None:
        self.settings = settings
        self.model = f"claude-cli:{settings.claude_cli_model}"
        self.http = http or _http()
        self.batch_size = max(1, settings.claude_cli_batch_posts)
        self._run = run or self._run_cli  # injectable for tests: run(prompt, folder) -> str (JSON text)

    # --- one post (the worker's normal interface) -----------------------------------------------------
    def analyze(self, media: list[MediaRef], caption: str) -> MediaAnalysis:
        return self.analyze_batch([(media, caption)])[0]

    # --- several posts in one CLI call --------------------------------------------------------------------
    def analyze_batch(self, posts: list[tuple[list[MediaRef], str]]) -> list[MediaAnalysis]:
        import shutil
        import tempfile
        from pathlib import Path

        folder = Path(tempfile.mkdtemp(prefix="klug-media-"))
        try:
            prepared = [self._prepare(folder, i, media) for i, (media, _caption) in enumerate(posts, 1)]
            todo = [
                (i, files, cap)
                for i, ((files, _kinds, _notes), (_m, cap)) in enumerate(zip(prepared, posts, strict=True), 1)
                if files
            ]
            results: dict[int, dict] = {}
            if todo:
                lines = []
                for i, files, cap in todo:
                    lines.append(f"Post P{i} – Bildunterschrift: {(cap or '').strip()[:500] or '(keine)'}")
                    lines.append("  Dateien: " + " ".join(str(f) for f in files))
                from klugscheisser.projects import wording

                prompt = (
                    f"Lies mit dem Read-Werkzeug alle Dateien der folgenden Instagram-Posts (Thema: {wording().topic}) "
                    "und beschreibe jeden Post für eine SRF-Sportredaktion.\n"
                    "- description: 1 bis 3 sachliche deutsche Sätze. Wer/was/wo ist zu sehen (Spiel, Training, "
                    "Feier, privat, Werbung)? Sichtbare Details wie Trikots und Teams, Medaillen, Pokale, Krücken, "
                    "Verbände, Gips, Rollstuhl, Spital. Keine Vermutungen über Gefühle, keine Wertungen. Namen nur, "
                    "wenn sie im Bild sichtbar sind oder in der Bildunterschrift stehen. Mehrere Bilder eines Posts "
                    "zusammen beschreiben. Standbilder aus einem Video als Video beschreiben.\n"
                    "- visible_text: sichtbarer Text (Einblendungen, Schilder, Plakate), sonst null.\n\n"
                    + "\n".join(lines)
                    + '\n\nAntworte NUR mit JSON: {"posts": [{"post": "P1", "description": "...", '
                    '"visible_text": "..." oder null}]}'
                )
                text = self._run(prompt, folder)
                results = _parse_cli(text)
            out = []
            for i, (files, kinds, notes) in enumerate(prepared, 1):
                r = results.get(f"P{i}")
                if not files or r is None:
                    out.append(MediaAnalysis(description=None, model="", analyzed=notes))
                    continue
                visible = (r.get("visible_text") or "").strip() or None
                out.append(
                    MediaAnalysis(
                        description=(r.get("description") or "").strip() or None,
                        spoken_quotes=[],
                        visible_text=None if visible in ("null", "None") else visible,
                        model=self.model,
                        analyzed=kinds + notes,
                    )
                )
            return out
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def _prepare(self, folder, i: int, media: list[MediaRef]):
        """Download this post's media into the temp folder; reels → three jpg frames."""
        chosen, notes = plan_media(media, self.settings)
        files, kinds, failed = [], [], 0
        for j, ref in enumerate(chosen, 1):
            limit = (
                int(self.settings.media_max_upload_mb * 1024 * 1024)
                if ref.kind == "video"
                else 5 * 1024 * 1024
            )
            try:
                blob = download(self.http, ref, limit)
            except httpx.HTTPError:
                notes.append(f"{ref.kind}: {DOWNLOAD_FAILED}")
                failed += 1
                continue
            if blob is None:
                notes.append(f"{ref.kind}: {TOO_BIG}")
                continue
            if ref.kind == "image":
                ext = {"image/png": "png", "image/webp": "webp"}.get(blob.mime, "jpg")
                f = folder / f"p{i}_{j}.{ext}"
                f.write_bytes(blob.data)
                files.append(f)
                kinds.append("image")
                continue
            frames = _video_frames(folder, f"p{i}_{j}", blob.data, ref.duration_s)
            if frames:
                files += frames
                kinds.append("video (Standbilder, ohne Ton)")
            else:
                notes.append(f"video: {UNSUPPORTED}")
        if chosen and failed == len(chosen):
            raise MediaDownloadError("no media could be downloaded (CDN link expired?)")
        return files, kinds, notes

    def _run_cli(self, prompt: str, folder) -> str:
        import subprocess

        st = self.settings
        cmd = [
            st.claude_cli_path,
            "-p",
            prompt,
            "--model",
            st.claude_cli_model,
            "--output-format",
            "json",
            "--allowedTools",
            "Read",
            "--system-prompt",
            CLI_SYSTEM,
            "--add-dir",
            str(folder),
        ]
        try:
            done = subprocess.run(
                cmd, cwd=folder, capture_output=True, text=True, timeout=st.claude_cli_timeout_s
            )
        except subprocess.TimeoutExpired as exc:
            raise MediaRateLimited(f"claude cli timeout after {st.claude_cli_timeout_s}s") from exc
        if done.returncode != 0:
            msg = (done.stderr or done.stdout or "").strip()[:300]
            if any(w in msg.lower() for w in ("limit", "usage", "rate", "quota", "overloaded")):
                raise MediaRateLimited(f"claude cli: {msg}")  # subscription window used up: stop, retry later
            raise MediaAnalysisError(f"claude cli failed ({done.returncode}): {msg}")
        data = json.loads(done.stdout or "{}")
        if data.get("is_error"):
            msg = str(data.get("result") or "")[:300]
            if any(w in msg.lower() for w in ("limit", "usage", "rate", "quota")):
                raise MediaRateLimited(f"claude cli: {msg}")
            raise MediaAnalysisError(f"claude cli error: {msg}")
        log.info(
            "claude cli media batch: %s turns, %.3f USD api-equivalent (subscription)",
            data.get("num_turns"),
            float(data.get("total_cost_usd") or 0),
        )
        return str(data.get("result") or "")


def _parse_cli(text: str) -> dict[str, dict]:
    """{"posts": [{"post": "P1", …}]} from the CLI's answer (tolerates ```json fences and text around it)."""
    import re as _re

    t = (text or "").strip()
    if "{" in t:
        t = t[t.index("{") : t.rindex("}") + 1]
    t = _re.sub(r"^```(json)?|```$", "", t).strip()
    try:
        data = json.loads(t)
    except ValueError:
        log.warning("claude cli media: no JSON in answer: %s", (text or "")[:200])
        return {}
    return {str(p.get("post")): p for p in data.get("posts") or [] if isinstance(p, dict)}


def _video_frames(folder, stem: str, data: bytes, duration_s: float | None) -> list:
    """Three jpg still frames of a reel (ffmpeg); [] when ffmpeg is missing or fails."""
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        return []
    src = folder / f"{stem}.mp4"
    src.write_bytes(data)
    dur = duration_s
    if not dur:
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(src)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            dur = float(probe.stdout.strip() or 0)
        except (subprocess.SubprocessError, ValueError):
            dur = 0
    frames = []
    for k, frac in enumerate(CLI_FRAMES, 1):
        out = folder / f"{stem}_f{k}.jpg"
        at = max(0.0, (dur or 3) * frac)
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{at:.2f}",
                    "-i",
                    str(src),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale='min(1080,iw)':-2",
                    str(out),
                ],
                capture_output=True,
                timeout=60,
            )
        except subprocess.SubprocessError:
            continue
        if out.exists() and out.stat().st_size > 0:
            frames.append(out)
    src.unlink(missing_ok=True)
    return frames


class FakeMediaAnalyzer:
    """Deterministic analyzer for tests. `descriptions` maps media URL -> description;
    `fail` raises that exception on every call."""

    name = "fake"
    model = "fake-media-1"

    def __init__(
        self,
        settings: Settings | None = None,
        descriptions: dict[str, str] | None = None,
        quotes: dict[str, list[str]] | None = None,
        visible_text: str | None = None,
        fail: Exception | None = None,
    ) -> None:
        self.settings = settings or Settings(_env_file=None)
        self.descriptions = descriptions or {}
        self.quotes = quotes or {}
        self.visible_text = visible_text
        self.fail = fail
        self.calls: list[list[MediaRef]] = []

    def analyze(self, media: list[MediaRef], caption: str) -> MediaAnalysis:
        self.calls.append(media)
        if self.fail is not None:
            raise self.fail
        chosen, notes = plan_media(media, self.settings)
        if not chosen:
            return MediaAnalysis(description=None, model="", analyzed=notes)
        desc = [
            self.descriptions.get(r.url) or f"[Test{r.kind} {hashlib.sha1(r.url.encode()).hexdigest()[:6]}]"
            for r in chosen
        ]
        quotes = [q for r in chosen for q in self.quotes.get(r.url, [])]
        return MediaAnalysis(
            description=" ".join(desc),
            spoken_quotes=quotes,
            visible_text=self.visible_text,
            model=self.model,
            analyzed=[r.kind for r in chosen] + notes,
        )


def get_media_analyzer(settings: Settings):
    """Configured analyzer, or None when media analysis is off (no key)."""
    choice, f = settings.media_provider, settings.features
    if choice == "off":
        return None
    if choice == "fake":
        return FakeMediaAnalyzer(settings)
    if choice == "gemini" or (choice == "auto" and f["gemini"]):
        return GeminiMediaAnalyzer(settings) if f["gemini"] else None
    if choice == "claude-cli":  # v15: Ondřej's Claude subscription via the CLI on the server
        return ClaudeCliMediaAnalyzer(settings)
    if choice == "claude" or (choice == "auto" and f["claude"]):
        return ClaudeMediaAnalyzer(settings) if f["claude"] else None
    return None


def _prompt(caption: str | None) -> str:
    from klugscheisser.projects import wording

    return MEDIA_PROMPT.format(caption=(caption or "")[:800], topic=wording().topic)
