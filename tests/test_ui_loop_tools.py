"""scripts/ui_loop: safety guards of the preview helper and pure helpers of the page checker (no DB, no browser)."""

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "ui_loop"))
import check_pages
import preview


@pytest.mark.parametrize(
    "name", ["klugscheisser", "klugscheisser_allgemein", "klugscheisser_test", "postgres", "x"]
)
def test_only_preview_db_may_be_recreated(name):
    with pytest.raises(SystemExit):
        preview.guard_db(name)


def test_preview_db_passes_guard():
    preview.guard_db(preview.PREVIEW_DB)  # no exit


def test_safe_env_has_no_keys_and_points_at_preview():
    assert preview.PREVIEW_DB in preview.SAFE_ENV["DATABASE_URL"]
    assert not any(k.endswith(("_API_KEY", "_TOKEN")) for k in preview.SAFE_ENV)
    for flag in ("AUTO_REFRESH", "WEB_AUTO_REFRESH", "PROFILES_ENABLED", "SIHF_DATA_ENABLED", "EP_ENABLED"):
        assert preview.SAFE_ENV[flag] == "false"
    assert preview.SAFE_ENV["MEDIA_PROVIDER"] == "off"
    assert preview.SAFE_ENV["NEWS_POLL_MINUTES"] == "0"
    assert "scheduled_jobs: false" in preview.PROJECTS_YAML
    assert f"database: {preview.PREVIEW_DB}" in preview.PROJECTS_YAML
    assert "allgemein" not in preview.PROJECTS_YAML


def test_live_install_and_dotenv_worktrees_are_refused(tmp_path, monkeypatch):
    with pytest.raises(SystemExit):
        preview.check_worktree(tmp_path)  # not a checkout
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "src/klugscheisser").mkdir(parents=True)
    assert preview.check_worktree(tmp_path) == tmp_path.resolve()
    (tmp_path / ".env").write_text("X=1")
    with pytest.raises(SystemExit):
        preview.check_worktree(tmp_path)
    (tmp_path / ".env").unlink()
    monkeypatch.setattr(preview, "LIVE", tmp_path)
    with pytest.raises(SystemExit):
        preview.check_worktree(tmp_path)


def test_read_live_env_value(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("A=1\nWEB_PASSWORD='geheim wert'\n#WEB_PASSWORD=alt\nexport WEB_SECRET=\"s\"\nEMPTY=\n")
    monkeypatch.setattr(preview, "LIVE_ENV", env)
    assert preview.read_live_env_value("WEB_PASSWORD") == "geheim wert"
    assert preview.read_live_env_value("WEB_SECRET") == "s"
    assert preview.read_live_env_value("EMPTY") is None
    assert preview.read_live_env_value("MISSING") is None


def test_skip_paths_block_work_and_chat():
    for path in (
        "/chat",
        "/chat/3",
        "/chats",
        "/p/eishockey/chat",
        "/aktualisieren",
        "/alles-neu",
        "/frage",
        "/person/3/aussprache.wav",
        "/person/3/funfacts",
        "/person/3/tier",
        "/hinzufuegen/4/ja",
        "/logout",
    ):
        assert check_pages.SKIP_PATH.search(path), path
    for path in (
        "/person/3",
        "/p/eishockey/suche",
        "/team/2",
        "/event/wch-2026",
        "/hinzufuegen/4",
        "/quellen",
    ):
        assert not check_pages.SKIP_PATH.search(path), path


def test_typo_changes_one_word_slightly():
    rnd = random.Random(1)
    for name in ("Lara Stalder", "Nicola Hadraschek-Eisenschmid", "Alina Müller"):
        t = check_pages.typo(name, rnd)
        assert t != name and abs(len(t) - len(name)) <= 1 and t.split()[0][0] == name.split()[0][0]


def test_norm_strips_accents_and_case():
    assert check_pages.norm("  Genève-Servette  Ü18 ") == "geneve-servette u18"


def test_ignore_rules():
    rules = [{"type": "tap_target", "page": "^person-", "match": "logo", "why": "Logo"}]
    hit = {"type": "tap_target", "page": "person-3", "viewport": "phone", "detail": {"sel": "a.logo"}}
    assert check_pages.ignored(hit, rules) == "Logo"
    assert check_pages.ignored({**hit, "page": "home"}, rules) is None
    assert check_pages.ignored({**hit, "type": "overlap"}, rules) is None
