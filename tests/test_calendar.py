import concurrent.futures

import pytest

from scheduler.calendar import Calendar, CalendarError


@pytest.fixture
def cal(tmp_path):
    return Calendar(tmp_path / "test.sqlite3", "Asia/Shanghai")


def event(**kw):
    return dict(
        title="和老王开会",
        start="2026-10-18T15:00:00+08:00",
        end="2026-10-18T16:00:00+08:00",
        location="会议室A",
        **kw,
    )


def test_crud_readback_persistence(cal):
    saved = cal.create(event())
    assert saved["version"] == 1
    again = Calendar(cal.path, "Asia/Shanghai")
    assert again.get(saved["id"]) == saved
    updated = again.update(saved["id"], 1, {"location": "会议室B"})
    assert updated["location"] == "会议室B"
    assert updated["start"] == saved["start"]
    with pytest.raises(CalendarError, match="stale"):
        again.delete(saved["id"], 1)
    again.delete(saved["id"], 2)
    assert again.get(saved["id"]) is None


def test_overlap_boundaries_and_utc(cal):
    saved = cal.create(event())
    assert (
        cal.search(start="2026-10-18T07:30:00Z", end="2026-10-18T08:30:00Z")[0]["id"] == saved["id"]
    )
    with pytest.raises(CalendarError, match="conflict"):
        cal.create({**event(), "title": "另一个会"})
    adjacent = cal.create(
        {**event(), "start": "2026-10-18T16:00:00+08:00", "end": "2026-10-18T17:00:00+08:00"}
    )
    assert adjacent["id"] != saved["id"]
    assert cal.conflicts(saved["start"], saved["end"], exclude_id=saved["id"]) == []


@pytest.mark.parametrize(
    "patch",
    [
        {"title": ""},
        {"title": " " * 5},
        {"start": "bad"},
        {"end": "2026-10-18T14:00:00+08:00"},
        {"start": "2026-10-18"},
        {"end": "2026-11-18T16:00:00+08:00"},
        {"unknown": "value"},
    ],
)
def test_validation(cal, patch):
    with pytest.raises((CalendarError, ValueError)):
        cal.create({**event(), **patch})
    assert cal.search() == []


def test_dst_nonexistent_and_ambiguous(tmp_path):
    c = Calendar(tmp_path / "ny.sqlite3", "America/New_York")
    for start, end in [
        ("2026-03-08T02:30:00", "2026-03-08T03:30:00"),
        ("2026-11-01T01:30:00", "2026-11-01T02:30:00"),
    ]:
        with pytest.raises(CalendarError):
            c.create(dict(title="DST", start=start, end=end))
    assert c.create(
        dict(title="DST", start="2026-11-01T01:30:00-04:00", end="2026-11-01T01:30:00-05:00")
    )


def test_fuzzy_location_notes_date_range(cal):
    saved = cal.create({**event(), "notes": "季度预算讨论"})
    assert cal.search(query="老王")[0]["id"] == saved["id"]
    assert cal.search(query="季度预算")[0]["id"] == saved["id"]
    assert cal.search(query="会议室A")[0]["id"] == saved["id"]
    assert cal.search(query="和老汪开会")[0]["id"] == saved["id"]
    assert cal.search(start="2026-10-19T00:00:00+08:00") == []
    assert cal.search(query="'; DROP TABLE events; --") == []
    assert len(cal.search()) == 1


def test_available_slots_and_exclude(cal):
    saved = cal.create(event())
    slots = cal.slots("2026-10-18T15:00:00+08:00", "2026-10-18T18:00:00+08:00", 60)
    assert slots[0]["start"].startswith("2026-10-18T16:00")
    assert cal.slots(saved["start"], saved["end"], 60, exclude_id=saved["id"])


def test_concurrent_conflict_check_is_atomic(cal):
    def create(_):
        try:
            return cal.create(event())
        except CalendarError:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))
    assert sum(x is not None for x in results) == 1
    assert len(cal.search()) == 1


def test_short_fuzzy_keyword_matches_a_typo(cal):
    saved = cal.create(event())
    assert cal.search(query="老汪")[0]["id"] == saved["id"]
    assert cal.search(query="不存在") == []
