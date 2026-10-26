"""The data dir: rotation cursors, the run lock, pictures and atomic writes."""

from __future__ import annotations

import os
from pathlib import Path

from plugins.news_illustrated import store

T0 = 1_800_000_000.0


def test_rotation_moves_on_at_most_once_per_dwell(tmp_path: Path) -> None:
    assert store.pick(tmp_path, "cell", 3, advance=True, now=T0) == 0
    assert store.pick(tmp_path, "cell", 3, advance=True, now=T0 + 10) == 0, "same send"
    later = T0 + store.ROTATE_MIN_S
    assert store.pick(tmp_path, "cell", 3, advance=True, now=later) == 1
    assert store.pick(tmp_path, "cell", 3, advance=True, now=later + store.ROTATE_MIN_S) == 2
    assert store.pick(tmp_path, "cell", 3, advance=True, now=later + 2 * store.ROTATE_MIN_S) == 0


def test_previews_do_not_move_the_cursor(tmp_path: Path) -> None:
    store.pick(tmp_path, "cell", 3, advance=True, now=T0)
    for step in range(1, 4):
        assert store.pick(tmp_path, "cell", 3, advance=False, now=T0 + step * 1000) == 0


def test_placements_rotate_independently_and_survive_fewer_stories(tmp_path: Path) -> None:
    store.pick(tmp_path, "a", 3, advance=True, now=T0)
    store.pick(tmp_path, "a", 3, advance=True, now=T0 + store.ROTATE_MIN_S)
    assert store.pick(tmp_path, "b", 3, advance=True, now=T0 + store.ROTATE_MIN_S) == 0
    assert store.pick(tmp_path, "a", 1, advance=False, now=T0) == 0
    assert store.pick(tmp_path, "a", 0, advance=True, now=T0) == 0


def test_forgotten_placements_are_dropped(tmp_path: Path) -> None:
    store.pick(tmp_path, "old", 3, advance=True, now=T0)
    store.pick(tmp_path, "new", 3, advance=True, now=T0 + store.CURSOR_KEEP_S + 1)
    assert set(store.read_json(tmp_path / store.CURSOR, {})) == {"new"}


def test_run_lock(tmp_path: Path) -> None:
    assert store.take_lock(tmp_path, now=T0)
    assert store.lock_held(tmp_path)
    assert not store.take_lock(tmp_path)
    store.release_lock(tmp_path)
    assert not store.lock_held(tmp_path)
    assert store.take_lock(tmp_path)


def test_a_dead_runs_lock_is_taken_over(tmp_path: Path) -> None:
    assert store.take_lock(tmp_path)
    stale = (tmp_path / store.LOCK).stat().st_mtime - store.LOCK_STALE_S - 5
    os.utime(tmp_path / store.LOCK, (stale, stale))
    assert not store.lock_held(tmp_path)
    assert store.take_lock(tmp_path)


def test_pictures_are_named_by_content_and_only_ours_are_served(tmp_path: Path) -> None:
    name = store.save_image(tmp_path, "tech-1234abcd", b"\xff\xd8\xffjpeg")
    assert name.startswith("tech-1234abcd-") and name.endswith(".jpg")
    assert store.image_path(tmp_path, name) == tmp_path / "images" / name
    assert store.image_path(tmp_path, "../stories.json") is None
    assert store.image_path(tmp_path, "missing-1.jpg") is None
    assert store.prune_images(tmp_path, set()) == 1


def test_stories_round_trip_and_bad_files_read_as_empty(tmp_path: Path) -> None:
    store.save_stories(tmp_path, [{"title": "A"}, {"title": ""}, "junk"])  # type: ignore[list-item]
    assert store.load_stories(tmp_path) == [{"title": "A"}]
    (tmp_path / store.STORIES).write_text("{not json", encoding="utf-8")
    assert store.load_stories(tmp_path) == []
    assert not list(tmp_path.glob(".*.tmp")), "no temp files left behind"
