"""Lineups page: one Live row per display, the rest Queued or Waiting.

Reproduces the Door Frame on a Sunday at 13:10: three all-day lineups, two of
them gated on a printer printing. The H3C is printing, so its lineup won the
display but sleeps until the next check-in; the photos lineup is still on the
glass; the P1S lineup waits for its printer. Before, all three said Live.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from flask import Flask

from app.lineups_view import (
    DashboardInfo,
    DisplayInput,
    LineupInput,
    OnScreenInput,
    PublishedInput,
    StepInput,
    assign_lanes,
    build_view,
    short_name,
)
from app.next_update import MODE_CONNECTED, MODE_SLEEPS, Reach

TZ = ZoneInfo("America/Phoenix")
NOW = datetime(2026, 11, 15, 13, 10, tzinfo=TZ)  # a Sunday


def at(hh: int, mm: int) -> float:
    return NOW.replace(hour=hh, minute=mm).timestamp()


DASH = {
    k: DashboardInfo(id=k, name=v)
    for k, v in {
        "photos": "icloud photos",
        "h3c": "Print · Big Bertha",
        "p1s": "Print · P1S",
    }.items()
}

PHOTOS = LineupInput(
    "photos_all_day",
    "Photos All day",
    "interval",
    (StepInput("photos", conditional=True),),
    interval_minutes=60,
    ready=False,
    waits_for="Big-Bertha Print status is not running",
)
H3C = LineupInput(
    "print_h3c_door",
    "Printing now: H3C (Door Frame)",
    "cycle",
    (StepInput("h3c", 5, conditional=True),),
    start="00:00",
    priority=110,
)
P1S = LineupInput(
    "print_p1s_door",
    "Printing now: P1S (Door Frame)",
    "cycle",
    (StepInput("p1s", 5, conditional=True),),
    start="00:00",
    priority=100,
    ready=False,
    waits_for="P1S Printer Print status is running",
)


def door(*lineups: LineupInput, **kw: Any) -> DisplayInput:
    base: dict[str, Any] = {
        "id": "reterminal_e1004_d856d4",
        "name": "Door Frame",
        "width": 1600,
        "height": 1200,
        "reach": Reach(MODE_SLEEPS, next_checkin_at=at(13, 25), gap_s=1800),
        "lineups": lineups or (PHOTOS, H3C, P1S),
        # The photos frame went up at 13:00; the H3C print page was published
        # at 13:05 and waits for the frame's next wake.
        "on_screen": OnScreenInput(
            "photos", at(13, 0), lineup_id="photos_all_day", origin_known=True
        ),
        "published": PublishedInput("h3c", at(13, 5)),
        "published_known": True,
    }
    base.update(kw)
    return DisplayInput(**base)


def rows(d: DisplayInput) -> dict[str, Any]:
    view = build_view([d], DASH, now=NOW).displays[0]
    return {lu.id: lu for lu in view.lineups}


def statuses(d: DisplayInput) -> dict[str, str]:
    return {k: v.status for k, v in rows(d).items()}


def test_door_frame_has_one_live_row_the_one_on_the_glass() -> None:
    out = build_view([door()], DASH, now=NOW).displays[0]
    by_id = {lu.id: lu for lu in out.lineups}
    assert [lu.status for lu in out.lineups].count("live") == 1
    assert by_id["photos_all_day"].status == "live"
    # The header agrees: it shows the photos, the H3C page lands at the wake.
    assert out.on_screen is not None and out.on_screen.dashboard_id == "photos"
    assert out.queued is not None and out.queued.dashboard_id == "h3c"
    # Live first, then queued, then waiting.
    assert [lu.id for lu in out.lineups] == ["photos_all_day", "print_h3c_door", "print_p1s_door"]


def test_the_winner_waiting_for_the_wake_is_queued_with_when_it_lands() -> None:
    h3c = rows(door())["print_h3c_door"]
    assert (h3c.status, h3c.status_label) == ("queued", "Queued")
    assert h3c.status_reason == "lands ~1:25 PM" or h3c.status_reason == "lands ~13:25"
    assert "check-in" in h3c.status_title
    assert not h3c.folded
    # Its step carries the Queued pill, not "Next".
    step = h3c.steps[0]
    assert step.queued and step.pill is not None and step.pill.kind == "queued"


def test_a_lineup_whose_conditions_fail_waits_greyed_with_the_condition() -> None:
    p1s = rows(door())["print_p1s_door"]
    assert (p1s.status, p1s.status_label) == ("waiting", "Waiting")
    assert p1s.status_reason == "until P1S Printer Print status is running"
    assert p1s.folded
    assert p1s.off_why is not None and p1s.off_why.label == "Conditions not met"
    assert not any(s.pill for s in p1s.steps)


def test_an_out_ranked_lineup_is_queued_behind_the_winner() -> None:
    # Both printers printing: H3C (110) out-ranks P1S (100); the photos are
    # still on the glass.
    both = door(PHOTOS, H3C, replace(P1S, ready=True, waits_for=""))
    got = rows(both)
    assert got["photos_all_day"].status == "live"
    assert got["print_h3c_door"].status == "queued"
    assert got["print_p1s_door"].status == "queued"
    assert got["print_p1s_door"].status_reason == "behind Printing now: H3C"


def test_a_lineup_that_only_fired_earlier_is_queued_for_its_next_step() -> None:
    """Same priority, overlapping: the one that fired last holds the panel and
    the other's next step lands over it, so it is queued for that time."""
    early = LineupInput("early", "Early", "cycle", (StepInput("photos", 30),), start="12:00")
    late = LineupInput("late", "Late", "cycle", (StepInput("h3c", 60),), start="13:05")
    d = door(
        early,
        late,
        reach=Reach(MODE_CONNECTED, live=True),
        on_screen=OnScreenInput("h3c", at(13, 5), lineup_id="late", origin_known=True),
        published=PublishedInput("h3c", at(13, 5)),
    )
    got = rows(d)
    assert got["late"].status == "live"
    assert got["early"].status == "queued"
    assert got["early"].status_reason in ("next 1:30 PM", "next 13:30")


def test_a_hand_pushed_frame_makes_no_row_live() -> None:
    d = door(on_screen=OnScreenInput("photos", at(13, 0), lineup_id=None, origin_known=True))
    got = statuses(d)
    assert "live" not in got.values()
    assert got["print_h3c_door"] == "queued"
    assert got["print_p1s_door"] == "waiting"


def test_without_a_named_source_the_glass_still_picks_the_live_row() -> None:
    d = door(on_screen=OnScreenInput("photos", at(13, 0)))
    assert statuses(d)["photos_all_day"] == "live"


def test_day_bar_puts_overlapping_lineups_in_their_own_lanes() -> None:
    out = build_view([door()], DASH, now=NOW).displays[0]
    day = out.day
    assert day.is_today and day.lanes == 3
    wins = {s.lineup_id: s for s in day.segments if s.kind == "window"}
    assert {s.lane for s in wins.values()} == {0, 1, 2}
    # The live one takes the main row and is the only one drawn live.
    assert wins["photos_all_day"].lane == 0 and wins["photos_all_day"].live
    assert [s.lineup_id for s in day.segments if s.live] == ["photos_all_day"]
    assert wins["print_h3c_door"].state == "queued"
    assert wins["print_p1s_door"].state == "waiting"
    assert "Waiting, until P1S" in wins["print_p1s_door"].title
    # Lanes never overlap.
    for a in wins.values():
        for b in wins.values():
            if a is not b and a.lane == b.lane:
                assert a.end_min <= b.start_min or b.end_min <= a.start_min
    # Other days have no live row but still lane the overlap.
    assert all(w.lanes == 3 for w in out.week)
    json.dumps(out.to_dict())


def test_assign_lanes_keeps_back_to_back_windows_on_one_row() -> None:
    from app.lineups_view import SegmentView

    def seg(lid: str, s: int, e: int, kind: str = "window") -> SegmentView:
        return SegmentView(lid, lid, kind, s, e, 0, 0, False, False, "", "", [], [])  # type: ignore[arg-type]

    segs = [seg("a", 0, 600), seg("b", 600, 900), seg("c", 500, 700), seg("h", 900, 1440, "hold")]
    assert assign_lanes(segs) == 2
    assert [s.lane for s in segs] == [0, 0, 1, 0]
    assert assign_lanes(segs, first="c") == 2
    assert segs[2].lane == 0 and segs[0].lane == 1 and segs[1].lane == 1


def test_short_name_drops_the_display_it_is_on() -> None:
    assert short_name("Printing now: H3C (Door Frame)", "Door Frame") == "Printing now: H3C"
    assert short_name("Night", "Door Frame") == "Night"


# -- the server reads the scheduler's own condition gate -------------------------------


@pytest.fixture
def app(tmp_path: Path) -> Flask:
    from app.main import REPO_ROOT, create_app
    from app.state.page_store import Page

    a = create_app(
        testing=False,
        data_root=tmp_path,
        plugins_dir=REPO_ROOT / "plugins",
        renderers_dir=REPO_ROOT / "renderers",
    )
    a.config["TESTING"] = True
    a.config["SETTINGS_STORE"].patch_section("app", {"locale": "en-GB"})
    for pid in ("photos", "h3c", "p1s"):
        a.config["PAGE_STORE"].save(Page(id=pid, name=pid.upper(), device_ids=["panel"]))
    return a


@pytest.fixture
def client(app: Flask) -> Any:
    from app.state.conditions import Condition
    from app.state.deck_model import Deck, DeckPage

    c = app.test_client()
    c.post("/setup", data={"password": "abcdefgh", "password_confirm": "abcdefgh"})
    code = app.config["PAIRING_STORE"].issue(note="test").code
    resp = c.post(
        "/api/v1/device/register",
        headers={"X-Pairing-Code": code, "Content-Type": "application/json"},
        data=json.dumps({"device_id": "panel", "kind": "esp32_client"}),
    )
    assert resp.status_code == 201

    def printing(entity: str, op: str = "==") -> list[Condition]:
        return [Condition(source_kind="ha_entity", source_id=entity, operator=op, value="running")]

    def cycle(id_: str, name: str, page: str, prio: int, conds: list[Condition]) -> Deck:
        return Deck(
            id=id_,
            name=name,
            device_ids=["panel"],
            pages=[DeckPage(page_id=page, dwell_minutes=5, conditions=conds)],
            advance="timer",
            advance_interval_minutes=5,
            advance_anchor="00:00",
            advance_priority=prio,
            advance_mode="priority",
        )

    store = app.config["DECK_STORE"]
    store.upsert(
        Deck(
            id="photos",
            name="Photos All day",
            device_ids=["panel"],
            pages=[DeckPage(page_id="photos", conditions=printing("sensor.h3c", "!="))],
            advance="timer",
            advance_trigger="interval",
            advance_interval_minutes=60,
        )
    )
    store.upsert(cycle("h3c", "Printing now: H3C", "h3c", 110, printing("sensor.h3c")))
    store.upsert(cycle("p1s", "Printing now: P1S", "p1s", 100, printing("sensor.p1s")))
    evaluator = app.config["CONDITION_EVALUATOR"]
    evaluator._ha_cache = {
        "sensor.h3c": {
            "entity_id": "sensor.h3c",
            "state": "running",
            "attributes": {"friendly_name": "Big-Bertha Print status"},
        },
        "sensor.p1s": {
            "entity_id": "sensor.p1s",
            "state": "finish",
            "attributes": {"friendly_name": "P1S Printer Print status"},
        },
    }
    # What was on the glass: the photos frame, put up by the photos lineup.
    app.config["DECK_NAV_STORE"].set("panel", "photos", "photos")
    return c


def _view_rows(client: Any) -> dict[str, dict[str, Any]]:
    data = client.get("/decks/view.json").get_json()
    panel = next(d for d in data["displays"] if d["id"] == "panel")
    return {lu["id"]: lu for lu in panel["lineups"]}


def test_view_json_marks_one_live_and_names_the_failing_condition(client: Any) -> None:
    got = _view_rows(client)
    assert [lu["status"] for lu in got.values()].count("live") == 1
    assert got["photos"]["status"] == "live"
    assert got["h3c"]["status"] == "queued"
    assert got["p1s"]["status"] == "waiting"
    assert got["p1s"]["status_reason"] == "until P1S Printer Print status is running"


def test_scheduler_gate_matches_the_picker(app: Flask, client: Any) -> None:
    scheduler = app.config["SCHEDULER"]
    store = app.config["DECK_STORE"]
    ready, failing = scheduler.lineup_gate(store.get("p1s"))
    assert not ready and failing and failing[0].condition.source_id == "sensor.p1s"
    assert scheduler.lineup_gate(store.get("h3c")) == (True, [])
    ready, failing = scheduler.lineup_gate(store.get("photos"))
    assert not ready and failing[0].condition.operator == "!="
    # The H3C finishes: the photos and the H3C swap.
    app.config["CONDITION_EVALUATOR"]._ha_cache["sensor.h3c"]["state"] = "finish"
    assert scheduler.lineup_gate(store.get("photos")) == (True, [])
    assert scheduler.lineup_gate(store.get("h3c"))[0] is False
    got = _view_rows(client)
    assert got["photos"]["status"] == "live"
    assert got["h3c"]["status"] == "waiting"
    assert got["h3c"]["status_reason"] == "until Big-Bertha Print status is running"


# -- a priority lineup reads as ranked choices, each conditional step in words ---------

DOOR_FRAME = LineupInput(
    "door_frame",
    "Door Frame",
    "cycle",
    (
        StepInput(
            "h3c",
            60,
            conditional=True,
            cond_label="Only while Big-Bertha Print status is running",
            cond_title="Shows only while Big-Bertha Print status is running.",
            cond_ok=True,
        ),
        StepInput(
            "p1s",
            60,
            conditional=True,
            cond_label="Only while P1S Printer Print status is running",
            cond_ok=False,
        ),
        StepInput("photos", 60),
    ),
    start="00:00",
    priority_mode=True,
)


def test_priority_lineup_steps_are_ranked_choices_not_a_loop() -> None:
    d = door(DOOR_FRAME, on_screen=OnScreenInput("photos", at(13, 0)))
    row = rows(d)["door_frame"]
    assert row.priority_mode
    assert [s.position for s in row.steps] == ["1st choice", "2nd choice", "3rd choice"]
    assert [s.separator for s in row.steps] == ["else", "else", ""]
    assert [s.fallback for s in row.steps] == [False, False, True]
    assert row.steps[0].cond_label == "Only while Big-Bertha Print status is running"
    assert row.steps[0].cond_ok is True and row.steps[1].cond_ok is False
    assert row.steps[2].cond_label == ""


def test_a_plain_rotation_keeps_its_loop() -> None:
    plain = replace(DOOR_FRAME, priority_mode=False)
    row = rows(door(plain, on_screen=OnScreenInput("photos", at(13, 0))))["door_frame"]
    assert [s.position for s in row.steps] == ["1/3", "2/3", "3/3"]
    assert [s.separator for s in row.steps] == ["arrow", "arrow", "loop"]
    assert not any(s.fallback for s in row.steps)


def test_a_waiting_step_chip_and_the_row_reason_name_the_same_condition(client: Any) -> None:
    got = _view_rows(client)
    p1s = got["p1s"]
    assert p1s["status"] == "waiting"
    chip = p1s["steps"][0]["cond_label"]
    assert chip == "Only while P1S Printer Print status is running"
    assert p1s["status_reason"] == "until " + chip.removeprefix("Only while ")
    assert p1s["steps"][0]["cond_ok"] is False
    assert got["h3c"]["steps"][0]["cond_ok"] is True


def test_view_json_ranks_a_merged_priority_lineup(app: Flask, client: Any) -> None:
    from app.state.conditions import Condition
    from app.state.deck_model import Deck, DeckPage

    def running(entity: str) -> list[Condition]:
        return [
            Condition(source_kind="ha_entity", source_id=entity, operator="==", value="running")
        ]

    app.config["DECK_STORE"].upsert(
        Deck(
            id="door_frame",
            name="Door Frame",
            device_ids=["panel"],
            pages=[
                DeckPage(page_id="h3c", dwell_minutes=60, conditions=running("sensor.h3c")),
                DeckPage(page_id="p1s", dwell_minutes=60, conditions=running("sensor.unnamed_p1s")),
                DeckPage(page_id="photos", dwell_minutes=60),
            ],
            advance="timer",
            advance_interval_minutes=60,
            advance_anchor="00:00",
            advance_mode="priority",
        )
    )
    row = _view_rows(client)["door_frame"]
    steps = row["steps"]
    assert row["priority_mode"]
    assert [s["position"] for s in steps] == ["1st choice", "2nd choice", "3rd choice"]
    assert [s["fallback"] for s in steps] == [False, False, True]
    assert steps[0]["cond_label"] == "Only while Big-Bertha Print status is running"
    # No friendly name: the entity id read as words.
    assert steps[1]["cond_label"] == "Only while Unnamed p1s is running"
    # The editor names the entities the same way.
    html = client.get("/decks/door_frame/edit").get_data(as_text=True)
    assert "Big-Bertha Print status" in html
