"""The daily edition: stories made per source, the keep-previous rules, when
a run is due, and the background runner."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from plugins.news_illustrated import feeds, painters, pipeline, store, summarise
from plugins.news_illustrated.tests.fakes import FakeNet, article_page, ok, ok_json, picture, rss

FEEDS = {
    "Tech": "https://hnrss.example.com/frontpage",
    "Business": "https://feeds.example.com/business.xml",
    "Global": "https://feeds.example.com/world.xml",
}
BODY = "The minister said the plan would go ahead next spring. " * 20


@pytest.fixture(autouse=True)
def _no_retry_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feeds, "FEED_RETRY_PAUSE_S", 0.0)


def _cfg(**extra: Any) -> pipeline.Config:
    settings = {
        "sources": "\n".join(f"{label} | {url}" for label, url in FEEDS.items()),
        "groq_api_key": "gsk",
        "huggingface_api_key": "hf",
        **extra,
    }
    return pipeline.Config.from_settings(settings)


def _news(fake: FakeNet, tag: str = "", *, painted: bool = True) -> None:
    for label, url in FEEDS.items():
        link = f"https://news.example.com/{label.lower()}{tag}"
        fake.on(url, ok(rss(f"{label} headline{tag}‑now", link, "Feed text."), "text/xml"))
        fake.on(link, ok(article_page(BODY)))
    fake.on(
        summarise.GROQ_URL, ok_json({"choices": [{"message": {"content": f"Summary{tag} here."}}]})
    )
    if painted:
        fake.on(painters.HUGGINGFACE_URL, ok(picture(), "image/jpeg"))
    else:
        fake.on(painters.HUGGINGFACE_URL, ok("<html>error</html>"))


def _run(data_dir: Path, cfg: pipeline.Config | None = None, **kw: Any) -> pipeline.Outcome:
    log: list[str] = []
    out = pipeline.run_edition(cfg or _cfg(), data_dir, log=log.append, **kw)
    return out


def test_an_edition_makes_one_illustrated_story_per_source(
    fake_net: FakeNet, tmp_path: Path
) -> None:
    _news(fake_net)
    outcome = _run(tmp_path)
    assert outcome == pipeline.Outcome(made=3)
    stories = store.load_stories(tmp_path)
    assert [s["source"] for s in stories] == ["Tech", "Business", "Global"]
    tech = stories[0]
    assert tech["title"] == "Tech headline-now"
    assert tech["summary"] == "Summary here."
    assert tech["url"] == "https://news.example.com/tech"
    assert tech["provider"] == "huggingface"
    assert tech["date"] == datetime.now().date().isoformat()
    assert store.image_path(tmp_path, tech["image"]) is not None
    assert len(fake_net.to(summarise.GROQ_URL)) == 3
    assert (
        "Content:\nThe minister said"
        in fake_net.to(summarise.GROQ_URL)[0].json()["messages"][0]["content"]
    )


def test_no_picture_keeps_the_whole_previous_story(fake_net: FakeNet, tmp_path: Path) -> None:
    _news(fake_net)
    _run(tmp_path)
    before = store.load_stories(tmp_path)
    fake_net.routes.clear()
    _news(fake_net, "-2", painted=False)
    outcome = _run(tmp_path)
    assert outcome == pipeline.Outcome(kept=3)
    assert store.load_stories(tmp_path) == before
    assert all(store.image_path(tmp_path, s["image"]) for s in before)


def test_no_picture_and_nothing_earlier_saves_text_only(fake_net: FakeNet, tmp_path: Path) -> None:
    _news(fake_net, painted=False)
    _run(tmp_path)
    stories = store.load_stories(tmp_path)
    assert len(stories) == 3 and all(s["image"] == "" for s in stories)
    # A text-only story is not a picture worth protecting: the next one replaces it.
    fake_net.routes.clear()
    _news(fake_net, "-2", painted=False)
    _run(tmp_path)
    assert store.load_stories(tmp_path)[0]["title"] == "Tech headline-2-now"


def test_a_failed_feed_keeps_its_previous_story(fake_net: FakeNet, tmp_path: Path) -> None:
    _news(fake_net)
    _run(tmp_path)
    before = store.load_stories(tmp_path)
    fake_net.routes.clear()
    fake_net.calls.clear()
    _news(fake_net, "-2")
    fake_net.routes.insert(0, (FEEDS["Business"], [ok("<html>down</html>")]))
    outcome = _run(tmp_path)
    assert outcome == pipeline.Outcome(made=2, kept=1)
    after = store.load_stories(tmp_path)
    assert after[1] == before[1]
    assert after[0]["title"] == "Tech headline-2-now"
    assert len(fake_net.to(FEEDS["Business"])) == 2  # retried once


def test_an_unchanged_story_is_not_paid_for_again(fake_net: FakeNet, tmp_path: Path) -> None:
    _news(fake_net)
    _run(tmp_path)
    fake_net.calls.clear()
    assert _run(tmp_path) == pipeline.Outcome(unchanged=3)
    assert fake_net.to(summarise.GROQ_URL) == [] and fake_net.to(painters.HUGGINGFACE_URL) == []
    # By hand it is made again.
    assert _run(tmp_path, force=True) == pipeline.Outcome(made=3)
    assert len(fake_net.to(painters.HUGGINGFACE_URL)) == 3


def test_removed_sources_and_their_pictures_go(fake_net: FakeNet, tmp_path: Path) -> None:
    _news(fake_net)
    _run(tmp_path)
    old = {s["image"] for s in store.load_stories(tmp_path)}
    fake_net.routes.clear()
    _news(fake_net, "-2")
    _run(tmp_path, _cfg(sources=f"Global | {FEEDS['Global']}"))
    stories = store.load_stories(tmp_path)
    assert [s["source"] for s in stories] == ["Global"]
    left = {p.name for p in (tmp_path / "images").iterdir()}
    assert left == {stories[0]["image"]}
    assert len(old - left) == 2  # Tech's and Business's pictures were pruned


def test_a_stop_ends_the_run_between_stories(fake_net: FakeNet, tmp_path: Path) -> None:
    _news(fake_net)
    calls = {"n": 0}

    def stopped() -> bool:
        calls["n"] += 1
        return calls["n"] > 4

    _run(tmp_path, stopped=stopped)
    assert len(store.load_stories(tmp_path)) == 1


def test_settings_parsing() -> None:
    cfg = pipeline.Config.from_settings(
        {
            "sources": "# comment\n\nTech | https://a.example/rss\nhttps://b.example/feed\nBad | ftp://x\n",
            "provider_order": "Gemini, nonsense > kie gemini",
            "run_at": "7:30",
            "summary_words": "9999",
            "kie_api_key": " k ",
        }
    )
    assert cfg.sources == (
        pipeline.Source("Tech", "https://a.example/rss"),
        pipeline.Source("News", "https://b.example/feed"),
    )
    assert cfg.paint.order == ("gemini", "kie")
    assert cfg.run_at == "07:30"
    assert cfg.summary_words == 300
    assert cfg.painters_ready == ["kie"]
    defaults = pipeline.Config.from_settings({})
    assert [s.label for s in defaults.sources] == ["Tech", "Business", "Global"]
    assert defaults.paint.order == painters.PROVIDERS and defaults.run_at == "06:05"
    assert defaults.groq_model == "openai/gpt-oss-120b"
    assert pipeline.parse_run_at("25:00") == "06:05"


def test_source_keys_survive_reordering_but_not_a_new_feed() -> None:
    a = pipeline.Source("Tech", "https://a.example/rss")
    assert a.key == pipeline.Source("Tech", "https://a.example/rss").key
    assert a.key.startswith("tech-")
    assert a.key != pipeline.Source("Tech", "https://b.example/rss").key


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def test_editions_turn_over_at_the_run_time() -> None:
    assert pipeline.edition(_at("2026-10-25 06:04"), "06:05") == "2026-10-24"
    assert pipeline.edition(_at("2026-10-25 06:05"), "06:05") == "2026-10-25"


def test_when_a_run_is_due(tmp_path: Path) -> None:
    morning = _at("2026-10-25 07:00")
    assert pipeline.due(tmp_path, "06:05", morning), "no stories yet"
    store.save_stories(tmp_path, [{"title": "x"}])
    store.save_state(tmp_path, {"edition": "2026-10-24"})
    assert not pipeline.due(tmp_path, "06:05", _at("2026-10-25 05:00")), "yesterday's is done"
    assert pipeline.due(tmp_path, "06:05", morning), "today's is not"
    store.save_state(tmp_path, {"edition": "2026-10-25"})
    assert not pipeline.due(tmp_path, "06:05", morning)


def test_a_failed_day_is_retried_a_little_later_then_left(tmp_path: Path) -> None:
    store.save_stories(tmp_path, [{"title": "x"}])
    first = _at("2026-10-25 06:10")
    store.save_state(
        tmp_path, {"edition": "2026-10-24", "attempts": {"2026-10-25": [first.timestamp()]}}
    )
    assert not pipeline.due(tmp_path, "06:05", _at("2026-10-25 07:00"))
    assert pipeline.due(tmp_path, "06:05", _at("2026-10-25 08:11"))
    two = [first.timestamp(), _at("2026-10-25 08:11").timestamp()]
    store.save_state(tmp_path, {"edition": "2026-10-24", "attempts": {"2026-10-25": two}})
    assert not pipeline.due(tmp_path, "06:05", _at("2026-10-25 23:00"))


def _wait(runner: pipeline.Runner, data_dir: Path) -> None:
    deadline = time.monotonic() + 10
    while runner.running(data_dir):
        assert time.monotonic() < deadline, "run did not finish"
        time.sleep(0.02)


def test_runner_makes_the_edition_in_the_background(fake_net: FakeNet, tmp_path: Path) -> None:
    _news(fake_net)
    runner = pipeline.Runner()
    assert runner.start(_cfg(), tmp_path, tz=None, reason="schedule")
    _wait(runner, tmp_path)
    assert len(store.load_stories(tmp_path)) == 3
    state = store.load_state(tmp_path)
    today = pipeline.edition(datetime.now(), "06:05")
    assert state["edition"] == today
    assert len(state["attempts"][today]) == 1
    last = state["last_run"]
    assert last["reason"] == "schedule" and last["outcome"] == "3 new" and not last["failed"]
    assert any("Story 2 of 3: Business" in line for line in last["log"])
    assert not (tmp_path / store.LOCK).exists()
    assert not pipeline.due(tmp_path, "06:05", datetime.now())


def test_only_one_run_at_a_time(tmp_path: Path) -> None:
    runner = pipeline.Runner()
    assert store.take_lock(tmp_path)  # another process is running
    assert not runner.start(_cfg(), tmp_path, tz=None, reason="manual")
    assert runner.running(tmp_path) and runner.pending(tmp_path) == 1
    store.release_lock(tmp_path)
    assert not runner.running(tmp_path)


def test_a_run_that_crashes_releases_the_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def boom(*a: Any, **k: Any) -> pipeline.Outcome:
        raise RuntimeError("bug")

    monkeypatch.setattr(pipeline, "run_edition", boom)
    runner = pipeline.Runner()
    assert runner.start(_cfg(), tmp_path, tz=None, reason="manual")
    _wait(runner, tmp_path)
    last = store.load_state(tmp_path)["last_run"]
    assert last["failed"] and last["outcome"] == "RuntimeError: bug"
    assert "edition" not in store.load_state(tmp_path)
    assert not (tmp_path / store.LOCK).exists()


def test_manual_runs_do_not_use_up_the_days_attempts(fake_net: FakeNet, tmp_path: Path) -> None:
    _news(fake_net, painted=False)
    runner = pipeline.Runner()
    assert runner.start(_cfg(), tmp_path, tz=None, reason="manual", force=True)
    _wait(runner, tmp_path)
    today = pipeline.edition(datetime.now(), "06:05")
    assert store.load_state(tmp_path)["attempts"] == {today: []}
