import os
import stat
from datetime import UTC, datetime
from ipaddress import ip_network
from pathlib import Path

import pytest

from subnet_tracker.openvpn.apply import write_routes_file
from subnet_tracker.routes.model import PlanStats, Route, RoutePlan
from subnet_tracker.routes.render import render_routes, routes_hash
from subnet_tracker.types import RouteKind, Timestamp

GENERATED = Timestamp(int(datetime(2026, 10, 11, 3, 20, tzinfo=UTC).timestamp()))


def _rendered(*networks: str, at: Timestamp = GENERATED) -> str:
    routes = tuple(Route(ip_network(network), RouteKind.DNS) for network in networks)
    v4 = sum(route.network.version == 4 for route in routes)
    return render_routes(RoutePlan(routes, PlanStats(v4, len(routes) - v4, v4, len(routes) - v4), ()), at).text


def test_creates_the_file_without_a_previous_copy(tmp_path: Path) -> None:
    path = tmp_path / "routes.conf"
    text = 'push "route 149.154.160.0 255.255.240.0"\n'
    assert write_routes_file(path, text) is True
    assert path.read_bytes() == text.encode()
    assert write_routes_file(path, text) is False
    assert list(tmp_path.iterdir()) == [path]


def test_rendered_text_is_stored_and_a_new_header_is_not_a_change(tmp_path: Path) -> None:
    path = tmp_path / "routes.conf"
    text = _rendered("149.154.160.0/20", "2001:67c:4e8::/48")
    assert write_routes_file(path, text) is True
    assert path.read_bytes() == text.encode()
    later = _rendered("149.154.160.0/20", "2001:67c:4e8::/48", at=Timestamp(GENERATED + 3600))
    assert later != text
    assert routes_hash(later) == routes_hash(text)
    assert write_routes_file(path, later) is False
    assert path.read_bytes() == text.encode()
    assert not (tmp_path / "routes.conf.prev").exists()


def test_manual_directive_is_overwritten_and_kept_in_prev(tmp_path: Path) -> None:
    text = 'push "route 1.0.0.0 255.0.0.0"\n'
    edited = f"{text}push-reset\n"
    path = tmp_path / "routes.conf"
    path.write_bytes(edited.encode())
    assert write_routes_file(path, text) is True
    assert path.read_bytes() == text.encode()
    assert (tmp_path / "routes.conf.prev").read_bytes() == edited.encode()


def test_undecodable_file_is_replaced_and_the_raw_bytes_are_kept(tmp_path: Path) -> None:
    path = tmp_path / "routes.conf"
    raw = b"\xff\xfe\n"
    path.write_bytes(raw)
    text = 'push "route 1.0.0.0 255.0.0.0"\n'
    assert write_routes_file(path, text) is True
    assert path.read_bytes() == text.encode()
    assert (tmp_path / "routes.conf.prev").read_bytes() == raw


def test_prev_is_the_immediate_previous_version(tmp_path: Path) -> None:
    path = tmp_path / "routes.conf"
    versions = [f'push "route {i}.0.0.0 255.0.0.0"\n' for i in (1, 2, 3)]
    for text in versions:
        assert write_routes_file(path, text) is True
    assert path.read_text() == versions[2]
    assert (tmp_path / "routes.conf.prev").read_text() == versions[1]


def test_previous_copy_follows_the_given_file_name(tmp_path: Path) -> None:
    path = tmp_path / "pushed.txt"
    assert write_routes_file(path, "old\n") is True
    assert write_routes_file(path, 'push "route 1.0.0.0 255.0.0.0"\n') is True
    assert path.read_text() == 'push "route 1.0.0.0 255.0.0.0"\n'
    assert (tmp_path / "pushed.txt.prev").read_text() == "old\n"


def test_matching_hash_keeps_bytes_mtime_and_prev(tmp_path: Path) -> None:
    path = tmp_path / "routes.conf"
    write_routes_file(path, 'push "route 9.0.0.0 255.0.0.0"\n')
    write_routes_file(path, 'push "route 8.0.0.0 255.0.0.0"\n')
    previous = (tmp_path / "routes.conf.prev").read_bytes()
    os.utime(path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    stamped = '# generated 2026-10-11T04:20:00Z, 1 IPv4 + 0 IPv6 routes\npush "route 8.0.0.0 255.0.0.0"\n'
    assert routes_hash(stamped) == routes_hash(path.read_text())
    assert stamped != path.read_text()
    assert write_routes_file(path, stamped) is False
    assert path.read_text() == 'push "route 8.0.0.0 255.0.0.0"\n'
    assert path.stat().st_mtime_ns == 1_700_000_000_000_000_000
    assert (tmp_path / "routes.conf.prev").read_bytes() == previous


def test_comparison_uses_routes_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "routes.conf"
    text = 'push "route 1.0.0.0 255.0.0.0"\n'
    write_routes_file(path, text)
    seen: list[str] = []

    def spy(value: str) -> str:
        seen.append(value)
        return routes_hash(value)

    monkeypatch.setattr("subnet_tracker.openvpn.apply.routes_hash", spy)
    assert write_routes_file(path, text) is False
    assert seen == [text, text]


def test_written_files_are_world_readable(tmp_path: Path) -> None:
    path = tmp_path / "routes.conf"
    saved = os.umask(0o077)
    try:
        write_routes_file(path, 'push "route 1.0.0.0 255.0.0.0"\n')
        write_routes_file(path, 'push "route 2.0.0.0 255.0.0.0"\n')
        assert stat.S_IMODE(path.stat().st_mode) == 0o644
        assert stat.S_IMODE((tmp_path / "routes.conf.prev").stat().st_mode) == 0o644
    finally:
        os.umask(saved)


def test_replace_fsyncs_a_same_directory_temporary_before_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[str, Path]] = []
    real_fsync, real_replace = os.fsync, os.replace

    def spy_fsync(fd: int) -> None:
        events.append(("fsync", Path(os.readlink(f"/proc/self/fd/{fd}")).resolve()))
        real_fsync(fd)

    def spy_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        source, dest = Path(src).resolve(), Path(dst).resolve()
        assert source.parent == dest.parent == tmp_path.resolve()
        assert ("fsync", source) in events
        events.append(("replace", dest))
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(os, "replace", spy_replace)
    path = tmp_path / "routes.conf"
    path.write_text('push "route 1.0.0.0 255.0.0.0"\n')
    assert write_routes_file(path, 'push "route 2.0.0.0 255.0.0.0"\n') is True
    replaced = [index for index, (kind, _) in enumerate(events) if kind == "replace"]
    assert [events[index][1].name for index in replaced] == ["routes.conf.prev", "routes.conf"]
    directory = tmp_path.resolve()
    assert all(("fsync", directory) in events[index + 1 :] for index in replaced)
    assert all(not name.startswith(".") for name in (entry.name for entry in tmp_path.iterdir()))


def test_failed_rename_leaves_the_original_and_no_temporary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "routes.conf"
    original = b'push "route 1.0.0.0 255.0.0.0"\n'
    path.write_bytes(original)
    real_replace = os.replace

    def fail_routes(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if Path(dst) == path:
            raise OSError("replace failed")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", fail_routes)
    with pytest.raises(OSError, match="replace failed"):
        write_routes_file(path, 'push "route 2.0.0.0 255.0.0.0"\n')
    assert path.read_bytes() == original
    prev = tmp_path / "routes.conf.prev"
    assert not prev.exists() or prev.read_bytes() == original
    assert all(not entry.name.startswith(".") for entry in tmp_path.iterdir())


def test_missing_directory_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "absent" / "routes.conf"
    with pytest.raises(FileNotFoundError):
        write_routes_file(path, 'push "route 1.0.0.0 255.0.0.0"\n')
    assert not path.parent.exists()
