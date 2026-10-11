import sqlite3
from ipaddress import ip_address

from addresses_world import answer, record, resolver, rows, tracked
from doh_samples import AAAA, CLOUDFLARE_CNAME_A, A, body
from doh_samples import record as doh_record
from domains_world import NOW
from subnet_tracker.core.addresses import Resolution, name_resolves, request_external
from subnet_tracker.core.events import EventFilter, EventKind, list_events
from subnet_tracker.core.external import (
    LEASE,
    MAX_RESULTS,
    SILENCE,
    Acceptance,
    AcceptRefusal,
    Assignment,
    AssignRefusal,
    ReportedResult,
    TaskCandidate,
    accept_browser_results,
    accept_results,
    assign_tasks,
    select_tasks,
)
from subnet_tracker.core.requests import Apply, RequestKind, decode_request
from subnet_tracker.core.settings import SettingError
from subnet_tracker.core.settings_store import set_setting
from subnet_tracker.types import DomainName, NameId, ReconnectMode, ResolverId, ResolverKind, ResolveStatus, Seconds, Timestamp

M = Seconds(1000)
T0 = Timestamp(1_000_000)


def candidate(name_id: int, attempted: int | None = None, requested: int | None = None, leased: int | None = None) -> TaskCandidate:
    def ts(value: int | None) -> Timestamp | None:
        return None if value is None else Timestamp(value)

    return TaskCandidate(NameId(name_id), DomainName(f"{name_id}.example"), ts(attempted), ts(requested), ts(leased))


def chosen(items: list[TaskCandidate], n: int = 10, now: int = 10_000) -> list[int]:
    return [item.name_id for item in select_tasks(items, n, M, Timestamp(now))]


def test_lease_is_five_minutes_silence_is_ten_and_a_request_holds_500() -> None:
    assert (Seconds(300), Seconds(600), 500) == (LEASE, SILENCE, MAX_RESULTS)


def test_never_answered_and_resolve_now_come_before_older_attempts() -> None:
    never = candidate(1)
    resolve_now = candidate(2, attempted=9_900, requested=9_950)
    older_resolve_now = candidate(3, attempted=100, requested=200)
    stale_old = candidate(4, attempted=1)
    stale_new = candidate(5, attempted=2)
    # priority first, and within it the names with no attempt, then the oldest attempt; stale fills what n leaves
    assert chosen([stale_old, stale_new, resolve_now, never, older_resolve_now], n=4) == [1, 3, 2, 4]
    assert chosen([stale_old, stale_new, resolve_now], n=1) == [2]


def test_an_attempt_exactly_m_old_waits_and_one_second_older_is_due() -> None:
    now = 10_000
    exact = candidate(1, attempted=now - M)
    older = candidate(2, attempted=now - M - 1)
    fresher = candidate(3, attempted=now - M + 1)
    assert chosen([exact, older, fresher], now=now) == [2]


def test_requested_at_equal_to_the_attempt_is_not_resolve_now() -> None:
    assert chosen([candidate(1, attempted=9_900, requested=9_900)]) == []


def test_an_active_lease_is_skipped_and_an_expired_lease_is_not() -> None:
    now = 10_000
    assert chosen([candidate(1, leased=now + 1), candidate(2, leased=now), candidate(3)], now=now) == [2, 3]


def test_equal_attempt_times_break_by_name_id() -> None:
    assert chosen([candidate(5, attempted=1), candidate(2, attempted=1), candidate(4), candidate(3)], n=3) == [3, 4, 2]


def test_new_names_are_capped_at_n() -> None:
    assert chosen([candidate(index) for index in range(1, 6)], n=2) == [1, 2]


def external(db: sqlite3.Connection, kind: ResolverKind, name: str) -> ResolverId:
    return resolver(db, name, kind)


def tune(db: sqlite3.Connection, key: str, value: str) -> None:
    stored = set_setting(db, key, value, NOW)
    assert not isinstance(stored, SettingError)


def names_of(result: Assignment | AssignRefusal) -> list[str]:
    assert isinstance(result, Assignment)
    return [item.name for item in result.names]


def accepted(result: Acceptance | AcceptRefusal) -> Acceptance:
    assert isinstance(result, Acceptance)
    return result


def reported(name: str, status: str = "nxdomain", ipv4: tuple[str, ...] = (), error: str | None = None, cnames: tuple[str, ...] = ()) -> ReportedResult:
    return ReportedResult(name, status, ipv4, (), cnames, error)


def element(name: str, resolver_id: int, qtype: str, payload: object = None, http_status: int | None = 200, error: str | None = None) -> dict[str, object]:
    return {"name": name, "resolver_id": resolver_id, "type": qtype, "http_status": http_status, "body": payload, "error": error}


def runs(db: sqlite3.Connection) -> list[tuple[object, ...]]:
    return rows(db, "SELECT trigger, resolver_id, started_at, finished_at, domains_total, addresses_new, addresses_expired FROM resolve_runs ORDER BY id")


def leases(db: sqlite3.Connection) -> list[tuple[object, ...]]:
    return rows(db, "SELECT name_id, resolver_id, leased_until FROM resolve_leases ORDER BY resolver_id, name_id")


def test_assign_reads_n_and_m_for_the_resolver_kind_and_leases_for_five_minutes(db: sqlite3.Connection) -> None:
    browser, agent = external(db, ResolverKind.BROWSER, "browser"), external(db, ResolverKind.AGENT, "laptop")
    tune(db, "browser.batch", "1")
    tune(db, "agent.batch", "10")
    tune(db, "agent.max_age", "60s")
    a, www = tracked(db, "a.com www.a.com")
    record(db, answer(a, agent, Resolution(ResolveStatus.ERROR, error="timeout")), at=NOW - 61)
    browser_tasks = assign_tasks(db, browser, NOW)
    agent_tasks = assign_tasks(db, agent, NOW)
    assert isinstance(browser_tasks, Assignment)
    assert browser_tasks.session_closed is False
    assert names_of(browser_tasks) == ["a.com"]
    assert names_of(agent_tasks) == ["www.a.com", "a.com"]
    assert leases(db) == [(a, browser, NOW + LEASE), (a, agent, NOW + LEASE), (www, agent, NOW + LEASE)]
    assert runs(db) == []


def test_a_second_assign_skips_the_lease_until_it_expires(db: sqlite3.Connection) -> None:
    browser = external(db, ResolverKind.BROWSER, "browser")
    tune(db, "browser.batch", "10")
    a, www = tracked(db, "a.com www.a.com")
    assert names_of(assign_tasks(db, browser, NOW)) == ["a.com", "www.a.com"]
    second = assign_tasks(db, browser, Timestamp(NOW + LEASE - 1))
    assert names_of(second) == []
    assert isinstance(second, Assignment)
    assert second.session_closed is False
    assert names_of(assign_tasks(db, browser, Timestamp(NOW + LEASE))) == ["a.com", "www.a.com"]
    assert leases(db) == [(a, browser, NOW + 2 * LEASE), (www, browser, NOW + 2 * LEASE)]


def test_www_and_a_recent_error_stay_out_while_an_old_error_and_resolve_now_are_issued(db: sqlite3.Connection) -> None:
    browser = external(db, ResolverKind.BROWSER, "browser")
    tune(db, "browser.batch", "10")
    tune(db, "browser.max_age", "1000s")
    a, www = tracked(db, "a.com www.a.com")
    record(db, answer(a, browser, Resolution(ResolveStatus.ERROR, error="timeout")), answer(www, browser, Resolution(ResolveStatus.NXDOMAIN)), at=NOW)
    assert names_of(assign_tasks(db, browser, Timestamp(NOW + M))) == []
    assert names_of(assign_tasks(db, browser, Timestamp(NOW + M + 1))) == ["a.com", "www.a.com"]
    db.execute("DELETE FROM resolve_leases")
    record(db, answer(a, browser, Resolution(ResolveStatus.OK, frozenset({ip_address("192.0.2.1")}))), at=NOW + M + 1)
    request_external(db, Timestamp(NOW + M + 2), [a])
    tune(db, "browser.batch", "1")
    # a.com was answered a second ago, www.a.com is older than m; Resolve now still goes first
    assert names_of(assign_tasks(db, browser, Timestamp(NOW + M + 2))) == ["a.com"]


def test_assign_refuses_a_missing_resolver_and_a_server_resolver(db: sqlite3.Connection) -> None:
    dns = external(db, ResolverKind.DNS, "dns")
    assert assign_tasks(db, ResolverId(999), NOW) == AssignRefusal("unknown resolver")
    assert assign_tasks(db, dns, NOW) == AssignRefusal("not an external resolver")
    assert leases(db) == []


def test_a_result_is_recorded_at_server_time_the_lease_drops_and_apply_is_queued(db: sqlite3.Connection) -> None:
    agent = external(db, ResolverKind.AGENT, "laptop")
    tune(db, "agent.batch", "10")
    a, _www = tracked(db, "a.com www.a.com")
    record(db, answer(a, agent, Resolution(ResolveStatus.NXDOMAIN)), at=NOW)
    request_external(db, Timestamp(NOW + 1), [a])
    assign_tasks(db, agent, Timestamp(NOW + 1))
    outcome = accepted(accept_results(db, agent, [reported("a.com", "ok", ("192.0.2.9",), cnames=("edge.a.net",))], "agent:laptop", Timestamp(NOW + 5)))
    assert outcome.recorded == 1
    assert outcome.rejected == ()
    stored = name_resolves(db, [a])[0]
    assert (stored.attempted_at, stored.answered_at, stored.succeeded_at, stored.status) == (NOW + 5, NOW + 5, NOW + 5, ResolveStatus.OK)
    assert stored.reporter == "agent:laptop"
    assert stored.cnames == (DomainName("edge.a.net"),)
    assert stored.requested_at == NOW + 1
    assert rows(db, "SELECT ip FROM addresses WHERE name_id = ?", a) == [("192.0.2.9",)]
    assert rows(db, "SELECT last_attempt_at, last_success_at FROM names WHERE id = ?", a) == [(NOW + 5, NOW + 5)]
    assert leases(db) == [(_www, agent, NOW + 1 + LEASE)]
    assert [decode_request(RequestKind(kind), payload) for kind, payload in rows(db, "SELECT kind, payload FROM requests")] == [Apply(ReconnectMode.AUTO)]
    events = list_events(db, EventFilter(10))
    assert [(event.kind, event.actor, event.details["ip"]) for event in events] == [(EventKind.ADDRESS_NEW, "agent:laptop", "192.0.2.9")]


def test_an_invalid_result_keeps_its_lease_and_writes_nothing(db: sqlite3.Connection) -> None:
    agent = external(db, ResolverKind.AGENT, "laptop")
    a, _www = tracked(db, "a.com www.a.com")
    assign_tasks(db, agent, NOW)
    outcome = accepted(
        accept_results(
            db,
            agent,
            [
                reported("a.com", "ok", ("192.0.2.1",), error="nope"),
                reported("a.com", "ok"),
                reported("a.com", "nxdomain", ("192.0.2.1",)),
                reported("a.com", "nodata", error="timeout"),
                reported("a.com", "bogus"),
                reported("a.com", "ok", ("not-an-ip",)),
                ReportedResult("a.com", "ok", (), ("::ffff:192.0.2.1",)),
                ReportedResult("a.com", "ok", (), ("fe80::1%eth0",)),
                reported("No.Such.example", "nxdomain"),
                reported("missing.example", "nxdomain"),
                reported("a.com", "ok", ("192.0.2.1",), cnames=("Edge.example.",)),
                ReportedResult("a.com", "ok", (), tuple(f"2001:db8::{i}" for i in range(65))),
            ],
            "agent:laptop",
            NOW,
        )
    )
    assert [item.reason for item in outcome.rejected] == [
        "error text is only for a failure",
        "status does not match the addresses",
        "status does not match the addresses",
        "error text is only for a failure",
        "unknown status",
        "invalid address",
        "invalid address",
        "invalid address",
        "name is not in canonical form",
        "unknown name",
        "cname is not in canonical form",
        "more than 64 addresses in one family",
    ]
    assert outcome.recorded == 0
    assert name_resolves(db, [a]) == ()
    assert leases(db) == [(a, agent, NOW + LEASE), (_www, agent, NOW + LEASE)]
    assert rows(db, "SELECT count(*) FROM requests") == [(0,)]


def test_sixty_four_addresses_are_recorded_and_a_browser_unreachable_is_refused(db: sqlite3.Connection) -> None:
    browser = external(db, ResolverKind.BROWSER, "browser")
    _a, www = tracked(db, "a.com www.a.com")
    ipv4 = tuple(f"192.0.2.{i}" for i in range(64))
    outcome = accepted(accept_results(db, browser, [reported("a.com", "ok", ipv4), reported("www.a.com", "unreachable", error="down")], "browser:10.8.0.6", NOW))
    assert outcome.recorded == 1
    assert [item.reason for item in outcome.rejected] == ["browser results are not unreachable"]
    assert rows(db, "SELECT count(*) FROM addresses") == [(64,)]
    assert name_resolves(db, [www]) == ()


def test_more_than_500_results_refuse_the_request_and_500_are_checked_one_by_one(db: sqlite3.Connection) -> None:
    agent = external(db, ResolverKind.AGENT, "laptop")
    tracked(db, "a.com www.a.com")
    one = reported("a.com", "nxdomain")
    assert accept_results(db, agent, [one] * (MAX_RESULTS + 1), "agent:laptop", NOW) == AcceptRefusal("more than 500 results")
    outcome = accepted(accept_results(db, agent, [reported("missing.example", "nxdomain")] * MAX_RESULTS, "agent:laptop", NOW))
    assert outcome.recorded == 0
    assert len(outcome.rejected) == MAX_RESULTS
    assert name_resolves(db, []) == ()
    assert rows(db, "SELECT count(*) FROM name_resolves") == [(0,)]
    assert rows(db, "SELECT count(*) FROM requests") == [(0,)]


def test_accept_refuses_a_missing_or_server_resolver(db: sqlite3.Connection) -> None:
    dns = external(db, ResolverKind.DNS, "dns")
    assert accept_results(db, ResolverId(999), [], "agent:laptop", NOW) == AcceptRefusal("unknown resolver")
    assert accept_results(db, dns, [], "agent:laptop", NOW) == AcceptRefusal("not an external resolver")


def test_a_rejection_does_not_open_a_session_or_queue_apply(db: sqlite3.Connection) -> None:
    agent = external(db, ResolverKind.AGENT, "laptop")
    tracked(db, "a.com www.a.com")
    accepted(accept_results(db, agent, [reported("missing.example", "nxdomain")], "agent:laptop", NOW))
    assert runs(db) == []
    assert rows(db, "SELECT count(*) FROM requests") == [(0,)]


def test_the_first_result_opens_a_session_and_an_empty_assignment_closes_it(db: sqlite3.Connection) -> None:
    browser = external(db, ResolverKind.BROWSER, "browser")
    tune(db, "browser.batch", "10")
    a, www = tracked(db, "a.com www.a.com")
    assert runs(db) == []
    accepted(accept_results(db, browser, [reported("a.com", "nxdomain"), reported("www.a.com", "nodata")], "browser:10.8.0.6", NOW))
    assert runs(db) == [("browser", browser, NOW, None, 2, 0, 0)]
    closed = assign_tasks(db, browser, Timestamp(NOW + 10))
    assert names_of(closed) == []
    assert isinstance(closed, Assignment)
    assert closed.session_closed is True
    assert runs(db) == [("browser", browser, NOW, NOW + 10, 2, 0, 0)]
    accepted(accept_results(db, browser, [reported("a.com", "nxdomain")], "browser:10.8.0.6", Timestamp(NOW + 20)))
    assert [row[2:4] for row in runs(db)] == [(NOW, NOW + 10), (NOW + 20, None)]
    assert name_resolves(db, [a, www])[0].attempted_at == NOW + 20


def test_silence_closes_at_ten_minutes_and_the_next_result_opens_the_next_session(db: sqlite3.Connection) -> None:
    agent = external(db, ResolverKind.AGENT, "laptop")
    tracked(db, "a.com www.a.com")
    accepted(accept_results(db, agent, [reported("a.com", "ok", ("192.0.2.1",)), reported("www.a.com", "nxdomain")], "agent:laptop", T0))
    later = Timestamp(T0 + SILENCE)
    accepted(accept_results(db, agent, [ReportedResult("www.a.com", "ok", (), ("2001:db8::2",))], "agent:laptop", later))
    assert runs(db) == [
        ("agent", agent, T0, T0 + SILENCE, 2, 1, 0),
        ("agent", agent, later, None, 1, 1, 0),
    ]


def _handed_out(db: sqlite3.Connection) -> tuple[ResolverId, Timestamp]:
    browser = external(db, ResolverKind.BROWSER, "browser")
    tune(db, "browser.batch", "10")
    tune(db, "browser.max_age", "1000s")
    tracked(db, "a.com www.a.com")
    accepted(accept_results(db, browser, [reported("www.a.com", "nxdomain")], "browser:10.8.0.6", T0))
    opened = Timestamp(T0 + 5_000)
    accepted(accept_results(db, browser, [reported("a.com", "nxdomain")], "browser:10.8.0.6", opened))
    handout = Timestamp(opened + 100)
    handed = assign_tasks(db, browser, handout)
    assert names_of(handed) == ["www.a.com"]
    assert isinstance(handed, Assignment)
    assert handed.session_closed is False
    # the first session ended at its own silence; the second is still open because the hand-out is contact
    assert [row[3] for row in runs(db)] == [T0 + SILENCE, None]
    return browser, handout


def test_silence_has_not_elapsed_while_the_handout_is_inside_ten_minutes(db: sqlite3.Connection) -> None:
    browser, handout = _handed_out(db)
    # without the hand-out, silence would already have started at the previous result (699s earlier)
    early = assign_tasks(db, browser, Timestamp(handout + SILENCE - 1))
    assert isinstance(early, Assignment)
    assert early.session_closed is False
    assert runs(db)[1][3] is None


def test_silence_closes_ten_minutes_after_the_handout(db: sqlite3.Connection) -> None:
    browser, handout = _handed_out(db)
    due = assign_tasks(db, browser, Timestamp(handout + SILENCE))
    assert isinstance(due, Assignment)
    assert due.session_closed is True
    assert names_of(due) == ["www.a.com"]
    assert runs(db)[1][3] == handout + SILENCE


def test_when_silence_has_already_passed_an_empty_list_keeps_the_silence_time(db: sqlite3.Connection) -> None:
    browser = external(db, ResolverKind.BROWSER, "browser")
    tune(db, "browser.batch", "10")
    tracked(db, "a.com www.a.com")
    accepted(accept_results(db, browser, [reported("a.com", "nxdomain"), reported("www.a.com", "nxdomain")], "browser:10.8.0.6", T0))
    closed = assign_tasks(db, browser, Timestamp(T0 + 1_000))
    assert names_of(closed) == []
    assert isinstance(closed, Assignment)
    assert closed.session_closed is True
    assert runs(db) == [("browser", browser, T0, T0 + SILENCE, 2, 0, 0)]


def test_a_browser_doh_pair_is_recorded_through_the_resolve_service(db: sqlite3.Connection) -> None:
    browser = external(db, ResolverKind.BROWSER, "browser")
    tune(db, "browser.batch", "10")
    a, www = tracked(db, "www.youtube.com youtube.com")
    assign_tasks(db, browser, NOW)
    payload = [
        element("www.youtube.com", browser, "A", CLOUDFLARE_CNAME_A),
        element("not-canonical", browser, "A"),
        element("www.youtube.com", browser, "AAAA", body("www.youtube.com", AAAA)),
    ]
    outcome = accepted(accept_browser_results(db, payload, "browser:10.8.0.6", NOW))
    assert outcome.recorded == 1
    assert [(item.index, item.reason) for item in outcome.rejected] == [(1, "name is not in canonical form")]
    stored = name_resolves(db, [a])
    assert len(stored) == 1
    assert stored[0].status is ResolveStatus.OK
    assert stored[0].reporter == "browser:10.8.0.6"
    assert rows(db, "SELECT ip FROM addresses WHERE name_id = ? ORDER BY ip", a) == [("142.250.74.14",), ("142.250.74.46",)]
    assert stored[0].cnames == (DomainName("youtube-ui.l.google.com"),)
    assert [lease[0] for lease in leases(db)] == [www]
    assert runs(db)[0][0] == "browser"
    assert runs(db)[0][5] == 2


def test_a_bad_doh_body_is_an_error_with_no_addresses_and_the_name_waits_for_m(db: sqlite3.Connection) -> None:
    browser = external(db, ResolverKind.BROWSER, "browser")
    tune(db, "browser.batch", "10")
    tune(db, "browser.max_age", "1000s")
    a, www = tracked(db, "a.com www.a.com")
    assign_tasks(db, browser, NOW)
    payload = [element("a.com", browser, "A", body("a.com", A, [doh_record("a.com", A, "nope")])), element("a.com", browser, "AAAA", body("a.com", AAAA, status=3))]
    outcome = accepted(accept_browser_results(db, payload, "browser:10.8.0.6", NOW))
    assert outcome.recorded == 1
    assert outcome.rejected == ()
    stored = name_resolves(db, [a])[0]
    assert stored.status is ResolveStatus.ERROR
    assert stored.error is not None
    assert rows(db, "SELECT count(*) FROM addresses") == [(0,)]
    assert [lease[0] for lease in leases(db)] == [www]
    assert names_of(assign_tasks(db, browser, Timestamp(NOW + 10))) == []


def test_a_fetch_error_and_a_non_browser_resolver_are_not_stored_as_unreachable(db: sqlite3.Connection) -> None:
    browser = external(db, ResolverKind.BROWSER, "browser")
    dns = external(db, ResolverKind.DNS, "dns")
    tracked(db, "a.com www.a.com")
    payload = [
        element("a.com", browser, "A", http_status=None, error="Failed to fetch"),
        element("a.com", browser, "AAAA", http_status=404, payload=body("a.com", AAAA)),
        element("a.com", dns, "A", body("a.com", A)),
        element("a.com", dns, "AAAA", body("a.com", AAAA)),
        element("a.com", 999, "A", body("a.com", A)),
        element("a.com", 999, "AAAA", body("a.com", AAAA)),
        element("missing.example", browser, "A"),
    ]
    outcome = accepted(accept_browser_results(db, payload, "browser:10.8.0.6", NOW))
    assert [(item.index, item.reason) for item in outcome.rejected] == [
        (2, "resolver is not a browser resolver"),
        (3, "resolver is not a browser resolver"),
        (4, "unknown resolver"),
        (5, "unknown resolver"),
        (6, "missing the other record type"),
    ]
    stored = rows(db, "SELECT status, error FROM name_resolves")
    assert stored == [("error", "A: Failed to fetch; AAAA: HTTP 404")]
    assert rows(db, "SELECT count(*) FROM addresses") == [(0,)]


def test_over_500_browser_elements_or_a_non_array_records_nothing(db: sqlite3.Connection) -> None:
    browser = external(db, ResolverKind.BROWSER, "browser")
    tracked(db, "a.com www.a.com")
    good = [element("a.com", browser, "A", body("a.com", A)), element("a.com", browser, "AAAA", body("a.com", AAAA))]
    assert accept_browser_results(db, {"names": []}, "browser:10.8.0.6", NOW) == AcceptRefusal("not a JSON array")
    assert accept_browser_results(db, good * 251, "browser:10.8.0.6", NOW) == AcceptRefusal("more than 500 elements")
    assert rows(db, "SELECT count(*) FROM name_resolves") == [(0,)]
    assert runs(db) == []


def test_two_browser_resolvers_keep_separate_sessions(db: sqlite3.Connection) -> None:
    one, two = external(db, ResolverKind.BROWSER, "one"), external(db, ResolverKind.BROWSER, "two")
    tracked(db, "a.com www.a.com")
    payload = [
        element("a.com", one, "A", body("a.com", A, [doh_record("a.com", A, "192.0.2.20")])),
        element("a.com", one, "AAAA", body("a.com", AAAA)),
        element("a.com", two, "A", body("a.com", A, status=3)),
        element("a.com", two, "AAAA", body("a.com", AAAA, status=3)),
    ]
    outcome = accepted(accept_browser_results(db, payload, "browser:10.8.0.6", NOW))
    assert outcome.recorded == 2
    assert outcome.rejected == ()
    assert [row[1] for row in runs(db)] == [one, two]
    assert rows(db, "SELECT count(*) FROM requests") == [(1,)]


def test_browser_pair_status_matches_the_folded_query_results(db: sqlite3.Connection) -> None:
    browser = external(db, ResolverKind.BROWSER, "browser")
    a, _www = tracked(db, "a.com www.a.com")
    payload = [element("a.com", browser, "A", body("a.com", A, status=3)), element("a.com", browser, "AAAA", body("a.com", AAAA))]
    accepted(accept_browser_results(db, payload, "browser:10.8.0.6", NOW))
    assert name_resolves(db, [a])[0].status is ResolveStatus.NODATA
