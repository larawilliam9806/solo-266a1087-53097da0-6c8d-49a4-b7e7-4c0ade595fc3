"""End-to-end tests for scheduled (press-release coordinated) releases."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

Q = "2026Q1"

DISTRICTS = ["东城区", "西城区", "南城区", "北城区"]
INDUSTRIES = ["制造业", "建筑业", "批发零售"]

VALUES = [
    [30, 50, 40],
    [25, 35, 35],
    [10, 20, 30],
    [45, 3, 2],
]
ROW_TOTALS = [sum(r) for r in VALUES]
COL_TOTALS = [sum(VALUES[i][j] for i in range(len(VALUES)))
              for j in range(len(VALUES[0]))]

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
RELEASE = "2026-01-02T09:00:00Z"
RELEASE_ISO = "2026-01-02T09:00:00+00:00"


class Clock:
    """Mutable UTC clock handed to the service; supports scripted reads."""

    def __init__(self, start: datetime = START) -> None:
        self.t = start
        self.script: list[datetime] = []

    def __call__(self) -> datetime:
        if self.script:
            return self.script.pop(0)
        return self.t

    def advance(self, **kwargs) -> None:
        self.t += timedelta(**kwargs)


@pytest.fixture()
def client(tmp_path):
    clock = Clock()
    db = tmp_path / "sched.sqlite3"
    app = create_app(str(db), clock=clock)
    token = app.state.bootstrap_token
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {token}"})
        c.clock = clock
        c.db_path = str(db)
        yield c


def register(client, quarter=Q):
    body = {
        "districts": [{"name": n, "total": t}
                      for n, t in zip(DISTRICTS, ROW_TOTALS)],
        "industries": [{"name": n, "total": t}
                       for n, t in zip(INDUSTRIES, COL_TOTALS)],
    }
    return client.put(f"/api/v1/editor/quarters/{quarter}/totals", json=body)


def submit(client, values=VALUES, quarter=Q, **extra):
    body = {"values": values, "must_public": []}
    body.update(extra)
    return client.post(f"/api/v1/editor/quarters/{quarter}/detail", json=body)


def mask(client, cells, quarter=Q, **extra):
    body = {"cells": [list(c) for c in cells]}
    body.update(extra)
    return client.put(f"/api/v1/editor/quarters/{quarter}/mask", json=body)


def valid_mask(client, quarter=Q):
    advice = client.post(
        f"/api/v1/editor/quarters/{quarter}/suppression-advice", json={}
    ).json()
    assert advice["valid"], advice
    cells = [(c["row"], c["col"]) for c in advice["masked"]]
    assert mask(client, cells, quarter).status_code == 200
    return cells


def schedule(client, quarter=Q, **extra):
    body = {"release_at": RELEASE, "detail_version": 1, "mask_version": 1}
    body.update(extra)
    return client.put(f"/api/v1/editor/quarters/{quarter}/schedule", json=body)


def replace(client, quarter=Q, **extra):
    body = {"schedule_version": 1, "detail_version": 1, "mask_version": 1}
    body.update(extra)
    return client.post(f"/api/v1/editor/quarters/{quarter}/schedule/replace",
                       json=body)


def cancel(client, quarter=Q, **extra):
    body = {"schedule_version": 1}
    body.update(extra)
    return client.post(f"/api/v1/editor/quarters/{quarter}/schedule/cancel",
                       json=body)


def prepare(client, quarter=Q):
    """Register totals, submit a detail and save a fully valid mask."""
    assert register(client, quarter).status_code == 200
    assert submit(client, quarter=quarter).status_code == 200
    return valid_mask(client, quarter)


def cycled(values):
    """Same margins, different cell values (a routine daily edit)."""
    alt = [row[:] for row in values]
    alt[0][0], alt[0][1] = 29, 51
    alt[1][0], alt[1][1] = 26, 34
    return alt


# ------------------------------------------------------------------ lifecycle

def test_schedule_seals_and_releases_at_the_agreed_moment(client):
    cells = prepare(client)
    response = schedule(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "scheduled"
    assert body["schedule_version"] == 1
    assert body["release_at"] == RELEASE_ISO
    assert body["detail_version"] == 1
    assert body["mask_version"] == 1
    assert body["payload"]["published_at"] == RELEASE_ISO

    # Before the release moment the pending bulletin is invisible.
    assert client.get(f"/api/v1/bulletins/{Q}").status_code == 404
    assert client.get("/api/v1/bulletins").json() == {"bulletins": []}

    # Immediate publication is refused while the schedule is active.
    refused = client.post(f"/api/v1/editor/quarters/{Q}/publish", json={})
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "schedule_active"

    client.clock.advance(days=2)

    public = client.get(f"/api/v1/bulletins/{Q}")
    assert public.status_code == 200
    payload = public.json()
    assert payload == body["payload"]
    assert payload["published_at"] == RELEASE_ISO
    for i, row in enumerate(payload["cells"]):
        for j, value in enumerate(row):
            if (i, j) in cells:
                assert value is None
            else:
                assert value == VALUES[i][j]
    listing = client.get("/api/v1/bulletins").json()["bulletins"]
    assert [b["quarter"] for b in listing] == [Q]
    assert listing[0]["published_at"] == RELEASE_ISO

    # Once released, the direct entry treats the quarter as published.
    again = client.post(f"/api/v1/editor/quarters/{Q}/publish", json={})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "already_published"


def test_schedule_requires_a_future_utc_release_moment(client):
    prepare(client)
    past = schedule(client, release_at="2025-12-31T23:59:59Z")
    assert past.status_code == 422
    assert past.json()["error"]["code"] == "release_at_not_future"

    naive = schedule(client, release_at="2026-01-02T09:00:00")
    assert naive.status_code == 422
    assert naive.json()["error"]["code"] == "invalid_request"

    garbage = schedule(client, release_at="tomorrow morning")
    assert garbage.status_code == 422

    # Offsets are normalised to UTC.
    ok = schedule(client, release_at="2026-01-02T18:00:00+09:00")
    assert ok.status_code == 200
    assert ok.json()["release_at"] == RELEASE_ISO


def test_schedule_reviews_like_publish(client):
    register(client)
    response = schedule(client)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "detail_required"

    submit(client)
    response = schedule(client)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "mask_required"

    # A mask with protection gaps saves fine but fails the sealing review.
    assert mask(client, [(3, 1), (3, 2)]).status_code == 200
    response = schedule(client)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "mask_conflict"

    valid_mask(client)
    response = schedule(client, detail_version=99)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stale_detail_version"
    response = schedule(client, mask_version=99)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stale_mask_version"

    # A mask plan belonging to an outdated detail version is named.
    assert submit(client, cycled(VALUES)).status_code == 200
    response = schedule(client, detail_version=2, mask_version=1)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "mask_plan_superseded"

    # Nothing was sealed along the way.
    assert client.get(f"/api/v1/editor/quarters/{Q}/schedule").status_code == 404


def test_schedule_conflicts_with_existing_schedule_or_bulletin(client):
    prepare(client)
    assert schedule(client).status_code == 200
    again = schedule(client)
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "schedule_already_exists"

    other = "2026Q2"
    prepare(client, other)
    assert client.post(f"/api/v1/editor/quarters/{other}/publish",
                       json={}).status_code == 200
    response = schedule(client, quarter=other)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "already_published"


def test_schedule_after_release_counts_as_published(client):
    prepare(client)
    assert schedule(client).status_code == 200
    client.clock.advance(days=2)
    response = schedule(client)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "already_published"


def test_schedule_unknown_quarter_is_404(client):
    response = schedule(client)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "quarter_not_found"


def test_schedule_endpoints_require_the_editor_token(client):
    prepare(client)
    no_auth = TestClient(client.app)
    body = {"release_at": RELEASE, "detail_version": 1, "mask_version": 1}
    assert no_auth.put(f"/api/v1/editor/quarters/{Q}/schedule",
                       json=body).status_code == 401
    no_auth.headers.update({"Authorization": "Bearer nope"})
    assert no_auth.put(f"/api/v1/editor/quarters/{Q}/schedule",
                       json=body).status_code == 403


# ------------------------------------------------------------------- replace

def test_replace_swaps_the_draft_but_keeps_the_moment(client):
    prepare(client)
    assert schedule(client).status_code == 200

    # Daily edits move the working versions forward.
    assert submit(client, cycled(VALUES)).status_code == 200
    valid_mask(client)
    state = client.get(f"/api/v1/editor/quarters/{Q}").json()

    response = replace(
        client,
        schedule_version=1,
        detail_version=state["current_detail_version"],
        mask_version=state["mask_version"],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schedule_version"] == 2
    assert body["release_at"] == RELEASE_ISO          # moment unchanged
    assert body["detail_version"] == state["current_detail_version"]

    client.clock.advance(days=2)
    # The replacement draft — the last successfully sealed one — goes out.
    assert client.get(f"/api/v1/bulletins/{Q}").json() == body["payload"]


def test_replace_requires_the_seen_schedule_version(client):
    prepare(client)
    assert schedule(client).status_code == 200
    response = replace(client, schedule_version=9)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stale_schedule_version"

    current = client.get(f"/api/v1/editor/quarters/{Q}/schedule").json()
    assert current["schedule_version"] == 1
    assert current["detail_version"] == 1


def test_failed_replace_keeps_the_original_schedule(client):
    prepare(client)
    sealed = schedule(client).json()["payload"]

    # Loosen the working mask so the replacement review fails.
    assert mask(client, [(3, 1), (3, 2)]).status_code == 200
    response = replace(client, schedule_version=1, detail_version=1,
                       mask_version=2)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "mask_conflict"

    # The originally sealed draft is still the one that goes out.
    current = client.get(f"/api/v1/editor/quarters/{Q}/schedule").json()
    assert current["schedule_version"] == 1
    client.clock.advance(days=2)
    assert client.get(f"/api/v1/bulletins/{Q}").json() == sealed


def test_replace_and_cancel_are_refused_after_release(client):
    prepare(client)
    assert schedule(client).status_code == 200
    client.clock.advance(days=2)

    response = replace(client)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "schedule_locked"

    response = cancel(client)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "schedule_locked"

    assert client.get(f"/api/v1/bulletins/{Q}").status_code == 200


def test_replacement_still_under_review_at_the_deadline_loses(client):
    prepare(client)
    assert schedule(client).status_code == 200

    # The clock crosses the release moment between the first deadline check
    # and the sealing check: the original draft wins.
    release = datetime(2026, 1, 2, 9, tzinfo=timezone.utc)
    client.clock.script = [release - timedelta(seconds=1),
                           release + timedelta(seconds=1)]
    response = replace(client)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "schedule_locked"

    client.clock.advance(days=2)
    current = client.get(f"/api/v1/editor/quarters/{Q}/schedule").json()
    assert current["schedule_version"] == 1
    assert client.get(f"/api/v1/bulletins/{Q}").json() == current["payload"]


def test_replace_or_cancel_without_schedule_is_404(client):
    prepare(client)
    response = replace(client)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "schedule_not_found"
    response = cancel(client)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "schedule_not_found"


# -------------------------------------------------------------------- cancel

def test_cancel_discards_the_schedule(client):
    prepare(client)
    assert schedule(client).status_code == 200
    response = cancel(client)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"

    assert client.get(f"/api/v1/editor/quarters/{Q}/schedule").status_code == 404
    client.clock.advance(days=2)
    assert client.get(f"/api/v1/bulletins/{Q}").status_code == 404
    assert client.get("/api/v1/bulletins").json() == {"bulletins": []}

    # The quarter is free again: direct publication works.
    assert client.post(f"/api/v1/editor/quarters/{Q}/publish",
                       json={}).status_code == 200


def test_cancel_requires_the_seen_schedule_version(client):
    prepare(client)
    assert schedule(client).status_code == 200
    response = cancel(client, schedule_version=2)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stale_schedule_version"
    assert client.get(f"/api/v1/editor/quarters/{Q}/schedule").status_code == 200


# ------------------------------------------------------- isolation & restart

def test_daily_edits_do_not_touch_the_sealed_draft(client):
    prepare(client)
    sealed = schedule(client).json()["payload"]

    # Routine edits after scheduling: new detail version and mask.
    assert submit(client, cycled(VALUES)).status_code == 200
    valid_mask(client)

    client.clock.advance(days=2)
    assert client.get(f"/api/v1/bulletins/{Q}").json() == sealed


def test_restart_across_the_release_moment_keeps_bulletin_queryable(client):
    prepare(client)
    assert schedule(client).status_code == 200
    client.clock.advance(days=2)

    # Simulate a full service restart on the same database file.
    restarted = create_app(client.db_path, clock=client.clock)
    with TestClient(restarted) as c2:
        public = c2.get(f"/api/v1/bulletins/{Q}")
        assert public.status_code == 200
        assert public.json()["published_at"] == RELEASE_ISO
        listing = c2.get("/api/v1/bulletins").json()["bulletins"]
        assert [b["quarter"] for b in listing] == [Q]


def test_formal_bulletins_stay_queryable_alongside_schedules(client):
    prepare(client)
    assert schedule(client).status_code == 200

    other = "2025Q4"
    prepare(client, other)
    assert client.post(f"/api/v1/editor/quarters/{other}/publish",
                       json={}).status_code == 200

    # Before the scheduled release only the formal bulletin is listed.
    listing = client.get("/api/v1/bulletins").json()["bulletins"]
    assert [b["quarter"] for b in listing] == [other]
    assert client.get(f"/api/v1/bulletins/{other}").status_code == 200

    client.clock.advance(days=2)
    listing = client.get("/api/v1/bulletins").json()["bulletins"]
    assert [b["quarter"] for b in listing] == [other, Q]


def test_admin_state_reports_the_schedule(client):
    prepare(client)
    assert schedule(client).status_code == 200
    state = client.get(f"/api/v1/editor/quarters/{Q}").json()
    assert state["schedule"]["schedule_version"] == 1
    assert state["schedule"]["release_at"] == RELEASE_ISO
    assert state["schedule"]["released"] is False

    client.clock.advance(days=2)
    state = client.get(f"/api/v1/editor/quarters/{Q}").json()
    assert state["schedule"]["released"] is True
