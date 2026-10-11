import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from domain_view_world import add_resolver, resolve_times, seen
from domains_world import NOW, count, paste
from groups_world import domain, join, make_group, route
from subnet_tracker.core.db import connect, migrate, transaction
from subnet_tracker.core.events import EventFilter, EventKind, list_events
from subnet_tracker.core.export_state import read_export
from subnet_tracker.core.groups import find_group
from subnet_tracker.core.memberships import SetChange
from subnet_tracker.core.requests import TickWork, claim_requests, finish_requests
from subnet_tracker.core.services.importing import apply_import, preview_import
from subnet_tracker.core.settings_store import set_setting
from subnet_tracker.core.tags import edit_domain_tags, ensure_tags
from subnet_tracker.textio.changes import ImportMode, import_summary
from subnet_tracker.textio.export import render_export
from subnet_tracker.types import DomainId, DomainName, DomainSource, FieldError, ReconnectMode

ACTOR = "cli:alice"
MERGE, REPLACE = ImportMode.MERGE, ImportMode.REPLACE
NONE: tuple[str, ...] = ()


def names_of(db: sqlite3.Connection, domain_id: int) -> list[tuple[int, str, int]]:
    return [tuple(row) for row in db.execute("SELECT id, name, canonical FROM names WHERE domain_id = ? ORDER BY id", (domain_id,))]


def address_rows(db: sqlite3.Connection) -> list[tuple[int, int, str]]:
    return [tuple(row) for row in db.execute("SELECT id, name_id, ip FROM addresses ORDER BY id")]


def work(db: sqlite3.Connection) -> TickWork | None:
    claim = claim_requests(db)
    if claim is None:
        return None
    finish_requests(db, claim)
    return claim.work


def tag(db: sqlite3.Connection, domain_id: DomainId, name: str) -> None:
    tags = ensure_tags(db, [name])
    assert not isinstance(tags, FieldError)
    edit_domain_tags(db, [domain_id], SetChange(add=tags), NOW)


def test_export_reads_stored_settings_groups_and_domains_without_addresses(db: sqlite3.Connection) -> None:
    video = make_group(db, "Video", prefix_v4=24)
    db.execute("UPDATE groups SET description = ? WHERE id = ?", ("hosting", video.id))
    paste(db, "example.com")
    tracked = domain(db, "example.com")
    join(db, tracked.id, video)
    tag(db, tracked.id, "video")
    db.execute("UPDATE domains SET prefix_v4 = 20, comment = ? WHERE id = ?", ("note", tracked.id))
    route(db, "10.0.0.0/8", video)
    assert set_setting(db, "resolve.interval", "2h", NOW)
    resolver = add_resolver(db, "dns", 1)
    seen(db, "example.com", "9.0.0.1", resolver, 10, 20)
    model = read_export(db)
    assert model.addresses is None
    assert [s.key for s in model.settings] == ["resolve.interval"]
    assert model.settings[0].value == "2h"
    assert model.groups == tuple(g for g in model.groups if g.name == "Video")
    assert model.groups[0].description == "hosting"
    assert model.groups[0].prefix_v4 == 24
    exported = model.domains[0]
    assert (exported.name, exported.groups, exported.tags, exported.prefix_v4, exported.comment) == (DomainName("example.com"), frozenset({"Video"}), frozenset({"video"}), 20, "note")
    assert model.routes[0].network.with_prefixlen == "10.0.0.0/8"
    with_addresses = read_export(db, addresses=True)
    assert with_addresses.addresses is not None
    assert with_addresses.addresses[0].ip.compressed == "9.0.0.1"
    text = render_export(model)
    assert isinstance(text, str)
    assert "routes.default_prefix_v4" not in text
    assert "[addresses]" not in text
    addressed = render_export(with_addresses)
    assert isinstance(addressed, str)
    assert "[addresses]" in addressed


def test_replace_round_trip_matches_the_export_and_keeps_ids(db: sqlite3.Connection, tmp_path: Path) -> None:
    video = make_group(db, "Video", prefix_v4=24)
    db.execute("UPDATE groups SET description = ? WHERE id = ?", ("hosting", video.id))
    paste(db, "example.com")
    tracked = domain(db, "example.com")
    join(db, tracked.id, video)
    tag(db, tracked.id, "video")
    route(db, "10.0.0.0/8", video)
    set_setting(db, "resolve.interval", "2h", NOW)
    text = render_export(read_export(db))
    assert isinstance(text, str)
    again = apply_import(db, text, REPLACE, NONE, NONE, (), ACTOR, NOW)
    assert not again.applied
    assert render_export(read_export(db)) == text
    assert domain(db, "example.com").id == tracked.id
    with closing(connect(tmp_path / "fresh.db")) as fresh:
        migrate(fresh)
        assert apply_import(fresh, text, REPLACE, NONE, NONE, (), ACTOR, NOW).applied
        assert render_export(read_export(fresh)) == text


def test_merge_changes_only_attributes_written_in_the_file(db: sqlite3.Connection) -> None:
    paste(db, "example.com extra.com")
    tracked = domain(db, "example.com")
    db.execute("UPDATE domains SET enabled = 0, prefix_v4 = 20, comment = ? WHERE id = ?", ("kept", tracked.id))
    set_setting(db, "resolve.interval", "2h", NOW)
    result = apply_import(db, "example.com #video\n", MERGE, NONE, NONE, (), ACTOR, NOW)
    assert result.applied
    tracked = domain(db, "example.com")
    assert (tracked.enabled, tracked.prefix_v4, tracked.comment) == (False, 20, "kept")
    assert db.execute("SELECT 1 FROM settings WHERE key = 'resolve.interval'").fetchone() is not None
    assert domain(db, "extra.com").id
    assert work(db) == TickWork(apply=ReconnectMode.AUTO)


def test_replace_resets_omitted_attributes_settings_and_deletes_empty_tags(db: sqlite3.Connection) -> None:
    video = make_group(db, "Video", prefix_v4=24)
    paste(db, "example.com extra.com")
    tracked = domain(db, "example.com")
    join(db, tracked.id, video)
    tag(db, tracked.id, "video")
    db.execute("UPDATE domains SET enabled = 0, prefix_v4 = 20, comment = ? WHERE id = ?", ("kept", tracked.id))
    route(db, "10.0.0.0/8", video)
    set_setting(db, "resolve.interval", "2h", NOW)
    result = apply_import(db, "example.com\n", REPLACE, NONE, NONE, (), ACTOR, NOW)
    assert result.applied
    kept = domain(db, "example.com")
    assert kept.id == tracked.id
    assert (kept.enabled, kept.prefix_v4, kept.comment) == (True, None, "")
    assert find_group(db, "Video") is None
    assert db.execute("SELECT 1 FROM names WHERE name = 'extra.com'").fetchone() is None
    assert db.execute("SELECT 1 FROM tags").fetchone() is None
    assert db.execute("SELECT 1 FROM global_routes").fetchone() is None
    assert db.execute("SELECT 1 FROM settings").fetchone() is None
    kinds = {event.kind for event in list_events(db, EventFilter(20))}
    assert EventKind.TAG_DELETED in kinds
    assert EventKind.SETTING_CHANGED in kinds
    assert EventKind.DOMAIN_DELETED in kinds


def test_replace_flips_the_canonical_name_in_place(db: sqlite3.Connection) -> None:
    paste(db, "example.com")
    tracked = domain(db, "example.com")
    tag(db, tracked.id, "video")
    resolver = add_resolver(db, "dns", 1)
    seen(db, "example.com", "9.0.0.1", resolver, 10, 20)
    seen(db, "www.example.com", "9.0.0.2", resolver, 10, 20)
    resolve_times(db, "example.com", 10, 11)
    db.execute("INSERT INTO events (ts, kind, actor, domain_id, name, details) VALUES (?, 'domain.added', ?, ?, 'example.com', '{}')", (NOW, ACTOR, tracked.id))
    before_names = names_of(db, tracked.id)
    before_addresses = address_rows(db)
    result = apply_import(db, "www.example.com\n", REPLACE, NONE, NONE, (), ACTOR, NOW)
    summary = import_summary(result.diff)
    assert (summary.domains.updated, summary.canonical, summary.domains.added, summary.domains.removed) == (1, 1, 0, 0)
    flipped = domain(db, "www.example.com")
    assert flipped.id == tracked.id
    assert (flipped.canonical.name, flipped.pair.name) == ("www.example.com", "example.com")
    assert names_of(db, tracked.id) == [(before_names[0][0], "example.com", 0), (before_names[1][0], "www.example.com", 1)]
    assert address_rows(db) == before_addresses
    assert tuple(db.execute("SELECT last_attempt_at, last_success_at FROM names WHERE name = 'example.com'").fetchone()) == (10, 11)
    events = list_events(db, EventFilter(10))
    changed = next(event for event in events if event.kind is EventKind.DOMAIN_CHANGED)
    assert changed.actor == ACTOR
    assert changed.domain_id == tracked.id
    assert changed.name == "www.example.com"
    assert changed.details["name"] == {"old": "example.com", "new": "www.example.com"}
    assert changed.details["tags"] == {"old": ["video"], "new": []}
    assert any(event.kind == "domain.added" and event.domain_id == tracked.id for event in events)
    assert db.execute("SELECT count(*) FROM domains").fetchone()[0] == 1


def test_overlapping_names_are_removed_and_added(db: sqlite3.Connection) -> None:
    paste(db, "www.www.a.com")
    result = apply_import(db, "www.a.com\n", REPLACE, NONE, NONE, (), ACTOR, NOW)
    summary = import_summary(result.diff)
    assert (summary.canonical, summary.domains.added, summary.domains.removed) == (0, 1, 1)
    assert db.execute("SELECT 1 FROM names WHERE name = 'www.www.a.com'").fetchone() is None
    added = domain(db, "www.a.com")
    assert added.source is DomainSource.IMPORT
    kinds = {event.kind for event in list_events(db, EventFilter(10))}
    assert EventKind.DOMAIN_DELETED in kinds
    assert EventKind.DOMAIN_ADDED in kinds


def test_filters_limit_domains_and_replace_keeps_omitted_rows(db: sqlite3.Connection) -> None:
    video = make_group(db, "Video", prefix_v4=24)
    paste(db, "a.com b.com")
    grouped = domain(db, "a.com")
    join(db, grouped.id, video)
    route(db, "10.0.0.0/8", video)
    set_setting(db, "resolve.interval", "2h", NOW)
    scoped = apply_import(db, "b.com\nc.com\n", REPLACE, ("none",), NONE, (), ACTOR, NOW)
    assert scoped.applied
    assert domain(db, "b.com").id
    assert domain(db, "c.com").source is DomainSource.IMPORT
    assert domain(db, "a.com").id == grouped.id
    kept = find_group(db, "Video")
    assert kept is not None
    assert kept.prefix_v4 == 24
    assert db.execute("SELECT g.name FROM domain_groups dg JOIN groups g ON g.id = dg.group_id WHERE dg.domain_id = ?", (grouped.id,)).fetchone()["name"] == "Video"
    assert db.execute("SELECT 1 FROM global_routes").fetchone() is not None
    assert db.execute("SELECT 1 FROM settings WHERE key = 'resolve.interval'").fetchone() is not None


def test_group_none_is_lowercase_and_unknown_filters_write_nothing(db: sqlite3.Connection) -> None:
    make_group(db, "Video")
    paste(db, "a.com")
    missing = apply_import(db, "b.com\n", MERGE, ("NONE",), NONE, (), ACTOR, NOW)
    assert missing.rejected[0].field == "group"
    assert not missing.applied
    unknown_tag = apply_import(db, "b.com\n", MERGE, NONE, ("music",), (), ACTOR, NOW)
    assert unknown_tag.rejected[0].field == "tag"
    assert db.execute("SELECT count(*) FROM domains").fetchone()[0] == 1
    assert work(db) is None


def test_group_filter_matches_either_group_and_any_tag(db: sqlite3.Connection) -> None:
    video, audio = make_group(db, "Video"), make_group(db, "Audio")
    paste(db, "a.com b.com c.com")
    join(db, domain(db, "a.com").id, video)
    join(db, domain(db, "b.com").id, audio)
    tag(db, domain(db, "a.com").id, "music")
    tag(db, domain(db, "b.com").id, "music")
    one_group = apply_import(db, "a.com @Video #music\n", REPLACE, ("Video",), ("music",), (), ACTOR, NOW)
    assert import_summary(one_group.diff).domains.removed == 0
    assert domain(db, "b.com").id
    assert domain(db, "c.com").id
    either = apply_import(db, "a.com @Video #music\n", REPLACE, ("Video", "Audio"), ("music",), (), ACTOR, NOW)
    assert import_summary(either.diff).domains.removed == 1
    assert db.execute("SELECT 1 FROM names WHERE name = 'b.com'").fetchone() is None
    assert domain(db, "a.com").id
    assert domain(db, "c.com").id


def test_a_line_outside_the_filter_is_not_imported(db: sqlite3.Connection) -> None:
    make_group(db, "Video")
    result = apply_import(db, 'b.com\n[group "Video"]\na.com\n', MERGE, ("none",), NONE, (), ACTOR, NOW)
    assert result.applied
    assert domain(db, "b.com").source is DomainSource.IMPORT
    assert db.execute("SELECT 1 FROM names WHERE name = 'a.com'").fetchone() is None


def test_a_line_that_names_a_domain_outside_the_filter_is_skipped(db: sqlite3.Connection) -> None:
    video = make_group(db, "Video")
    paste(db, "a.com b.com")
    outside = domain(db, "a.com")
    join(db, outside.id, video)
    db.execute("UPDATE domains SET prefix_v4 = 20, comment = ? WHERE id = ?", ("kept", outside.id))
    text = "b.com ; fresh\nwww.a.com /24 ; overwritten\nc.com\n"
    preview = preview_import(db, text, REPLACE, ("none",), NONE, ())
    planned = {item.name for item in (*preview.diff.domains.added, *preview.diff.domains.removed)}
    planned.update(side.name for update in preview.diff.domains.updated for side in (update.old, update.new))
    assert planned.isdisjoint({"a.com", "www.a.com"})
    result = apply_import(db, text, REPLACE, ("none",), NONE, (), ACTOR, NOW)
    assert result.applied
    assert result.rejected == ()
    kept = domain(db, "a.com")
    assert kept.id == outside.id
    assert (kept.prefix_v4, kept.comment) == (20, "kept")
    assert db.execute("SELECT g.name FROM domain_groups dg JOIN groups g ON g.id = dg.group_id WHERE dg.domain_id = ?", (outside.id,)).fetchone()["name"] == "Video"
    assert domain(db, "b.com").comment == "fresh"
    added = domain(db, "c.com")
    assert added.source is DomainSource.IMPORT
    assert db.execute("SELECT count(*) FROM domains").fetchone()[0] == 3


def test_replace_with_a_filter_defaults_a_group_the_file_only_references(db: sqlite3.Connection) -> None:
    video = make_group(db, "Video", prefix_v4=24)
    audio = make_group(db, "Audio", prefix_v4=20)
    extra = make_group(db, "Extra", prefix_v4=22)
    db.execute("UPDATE groups SET description = ? WHERE id = ?", ("hosting", video.id))
    db.execute("UPDATE groups SET description = ? WHERE id = ?", ("sound", audio.id))
    db.execute("UPDATE groups SET description = ? WHERE id = ?", ("side", extra.id))
    paste(db, "a.com b.com")
    join(db, domain(db, "a.com").id, video)
    route(db, "10.0.0.0/8", audio)
    db.execute("UPDATE global_routes SET comment = ? WHERE cidr = ?", ("lan", "10.0.0.0/8"))
    set_setting(db, "resolve.interval", "2h", NOW)
    text = "a.com @Video\n[settings]\nresolve.timeout = 10s\n\n[routes]\n10.0.0.0/8\n192.0.2.0/24 @Extra\n"
    preview = preview_import(db, text, REPLACE, ("Video",), NONE, ())
    updated = {item.old.name: item.new for item in preview.diff.groups.updated}
    assert (updated["Video"].description, updated["Video"].prefix_v4, updated["Video"].prefix_v6, updated["Video"].enabled) == ("", None, None, True)
    assert (updated["Extra"].description, updated["Extra"].prefix_v4, updated["Extra"].prefix_v6, updated["Extra"].enabled) == ("", None, None, True)
    assert "Audio" not in updated
    assert preview.diff.groups.removed == ()
    assert preview.diff.routes.removed == ()
    assert {item.network.with_prefixlen for item in preview.diff.routes.added} == {"192.0.2.0/24"}
    assert {item.new.network.with_prefixlen for item in preview.diff.routes.updated} == {"10.0.0.0/8"}
    assert all(change.key != "resolve.interval" for change in preview.diff.settings)
    assert any(change.key == "resolve.timeout" for change in preview.diff.settings)
    result = apply_import(db, text, REPLACE, ("Video",), NONE, (), ACTOR, NOW)
    assert result.applied
    assert result.errors == ()
    assert result.rejected == ()
    for name in ("Video", "Extra"):
        reset = find_group(db, name)
        assert reset is not None
        assert (reset.description, reset.prefix_v4, reset.prefix_v6, reset.enabled) == ("", None, None, True)
    kept_group = find_group(db, "Audio")
    assert kept_group is not None
    assert (kept_group.description, kept_group.prefix_v4) == ("sound", 20)
    assert domain(db, "b.com").id
    assert db.execute("SELECT comment FROM global_routes WHERE cidr = '10.0.0.0/8'").fetchone()["comment"] == ""
    assert db.execute("SELECT 1 FROM settings WHERE key = 'resolve.interval'").fetchone() is not None
    assert db.execute("SELECT 1 FROM settings WHERE key = 'resolve.timeout'").fetchone() is not None


def test_server_address_and_parse_errors_write_nothing(db: sqlite3.Connection) -> None:
    from ipaddress import ip_address

    paste(db, "example.com")
    bad = apply_import(db, "[settings]\nresolve.interval = soon\n", MERGE, NONE, NONE, (), ACTOR, NOW)
    assert bad.errors
    assert bad.errors[0].line == 2
    assert not bad.applied
    covered = apply_import(db, "[routes]\n192.0.2.1\n", MERGE, NONE, NONE, (ip_address("192.0.2.1"),), ACTOR, NOW)
    assert any(error.line == 2 and "192.0.2.1" in error.message for error in covered.errors)
    assert not covered.applied
    assert count(db, "domains") == 1
    assert db.execute("SELECT 1 FROM global_routes").fetchone() is None
    assert work(db) is None


def test_import_rolls_back_with_the_callers_transaction_and_a_repeat_is_a_no_op(db: sqlite3.Connection) -> None:
    def boom() -> None:
        added = apply_import(db, "a.com\n", MERGE, NONE, NONE, (), ACTOR, NOW)
        assert added.applied
        raise RuntimeError("rollback")

    with pytest.raises(RuntimeError), transaction(db):
        boom()
    assert count(db, "domains") == 0
    assert list_events(db, EventFilter(10)) == ()
    assert work(db) is None
    first = apply_import(db, "a.com\n", MERGE, NONE, NONE, (), ACTOR, NOW)
    assert first.applied
    assert domain(db, "a.com").source is DomainSource.IMPORT
    assert db.execute("SELECT kind FROM add_batches").fetchone()["kind"] == "import"
    assert work(db) == TickWork(new_domains=frozenset({domain(db, "a.com").id}), apply=ReconnectMode.AUTO)
    recorded = len(list_events(db, EventFilter(10)))
    second = apply_import(db, "a.com\n", MERGE, NONE, NONE, (), ACTOR, NOW)
    assert not second.applied
    assert len(list_events(db, EventFilter(10))) == recorded
    assert work(db) is None


def test_a_group_description_does_not_queue_apply(db: sqlite3.Connection) -> None:
    make_group(db, "Video")
    result = apply_import(db, '[group "Video"]\ndescription = hosting\n', MERGE, NONE, NONE, (), ACTOR, NOW)
    assert not result.applied
    assert list_events(db, EventFilter(5))[0].kind is EventKind.GROUP_CHANGED
    assert work(db) is None


def test_preview_does_not_write(db: sqlite3.Connection) -> None:
    preview = preview_import(db, "a.com\n", MERGE, NONE, NONE, ())
    assert import_summary(preview.diff).domains.added == 1
    assert count(db, "domains") == 0
    assert work(db) is None
