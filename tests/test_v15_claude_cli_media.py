"""v15: image analysis with the Claude Code CLI (Ondřej's subscription): several posts per call, reels as three
still frames, results mapped back per post, subscription limits stop the run (no API, no network in tests)."""

import json
import shutil
import subprocess

import httpx
import pytest

from klugscheisser.config import Settings
from klugscheisser.interfaces import MediaRef
from klugscheisser.media import analyzer as an

SETTINGS = Settings(_env_file=None, media_provider="claude-cli")
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _http(files: dict[str, bytes]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        body = files.get(str(request.url))
        if body is None:
            return httpx.Response(403)
        mime = "video/mp4" if str(request.url).endswith(".mp4") else "image/png"
        return httpx.Response(200, content=body, headers={"content-type": mime})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_batch_maps_answers_back_to_posts():
    seen = {}

    def run(prompt, folder):
        seen["prompt"] = prompt
        seen["files"] = sorted(p.name for p in folder.iterdir())
        return (
            "```json\n"
            + json.dumps(
                {
                    "posts": [
                        {"post": "P1", "description": "Jubel nach dem Tor.", "visible_text": "EVZ"},
                        {"post": "P2", "description": "Training auf dem Eis.", "visible_text": None},
                    ]
                }
            )
            + "\n```"
        )

    a = an.ClaudeCliMediaAnalyzer(
        SETTINGS, http=_http({"https://cdn/a.png": PNG, "https://cdn/b.png": PNG}), run=run
    )
    out = a.analyze_batch(
        [([MediaRef("image", "https://cdn/a.png")], "Sieg!"), ([MediaRef("image", "https://cdn/b.png")], "")]
    )
    assert [o.description for o in out] == ["Jubel nach dem Tor.", "Training auf dem Eis."]
    assert out[0].visible_text == "EVZ" and out[1].visible_text is None
    assert out[0].model == "claude-cli:sonnet" and out[0].analyzed == ["image"]
    assert "Post P1 – Bildunterschrift: Sieg!" in seen["prompt"] and seen["files"] == ["p1_1.png", "p2_1.png"]


def test_post_without_media_and_expired_links():
    a = an.ClaudeCliMediaAnalyzer(SETTINGS, http=_http({}), run=lambda p, f: pytest.fail("no call"))
    with pytest.raises(an.MediaDownloadError):
        a.analyze_batch([([MediaRef("image", "https://cdn/gone.png")], "x")])


def test_used_up_subscription_window_stops(monkeypatch):
    class Done:
        returncode, stdout, stderr = 1, "", "Claude usage limit reached. Your limit will reset at 7pm."

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Done())
    a = an.ClaudeCliMediaAnalyzer(SETTINGS, http=_http({"https://cdn/a.png": PNG}))
    with pytest.raises(an.MediaRateLimited):
        a.analyze_batch([([MediaRef("image", "https://cdn/a.png")], "")])


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_reel_becomes_three_still_frames(tmp_path):
    clip = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=2:size=160x120:rate=10",
            str(clip),
        ],
        check=True,
    )
    frames = an._video_frames(tmp_path, "p1_1", clip.read_bytes(), 2.0)
    assert len(frames) == 3 and all(f.suffix == ".jpg" and f.stat().st_size > 0 for f in frames)


def test_selected_by_setting():
    assert isinstance(an.get_media_analyzer(SETTINGS), an.ClaudeCliMediaAnalyzer)
