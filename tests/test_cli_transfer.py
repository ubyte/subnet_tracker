import sqlite3
from dataclasses import replace
from pathlib import Path

from cli_world import ACTOR, NOW, Cli, Result, answering, reading, trigger_lines
from domain_view_world import add_resolver, seen
from domains_world import count, paste
from groups_world import domain, join, make_group
from subnet_tracker.core.events import EventFilter, list_events
from subnet_tracker.core.groups import find_group

FILE = "example.com\n"


def write(tmp_path: Path, text: str = FILE) -> Path:
    path = tmp_path / "import.txt"
    path.write_text(text, encoding="utf-8")
    return path


def test_export_prints_stored_state_and_writes_a_file(cli: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    make_group(db, "Video")
    paste(db, "example.com")
    shown = cli("export")
    assert shown.code == 0
    assert shown.err == ""
    assert "example.com" in shown.out
    assert '[group "Video"]' in shown.out
    assert "routes.default_prefix_v4" not in shown.out
    assert "[addresses]" not in shown.out
    assert cli.json("export") == {"text": shown.out}
    dest = tmp_path / "out.txt"
    wrote = cli("export", "-o", str(dest))
    assert wrote == Result(0, f"wrote {dest}\n", "")
    assert dest.read_text(encoding="utf-8") == shown.out


def test_export_with_addresses_and_a_line_break(cli: Cli, db: sqlite3.Connection) -> None:
    paste(db, "example.com")
    resolver = add_resolver(db, "dns", 1)
    seen(db, "example.com", "9.0.0.1", resolver, 10, 20)
    assert "9.0.0.1" in cli("export", "--with-addresses").out
    assert "[addresses]" not in cli("export").out
    db.execute("UPDATE domains SET comment = ? WHERE id = ?", ("a\nb", domain(db, "example.com").id))
    broken = cli("export")
    assert broken.code == 1
    assert broken.out == ""
    assert "line break" in broken.err


def test_import_confirms_applies_and_uses_the_cli_actor(cli: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    path = write(tmp_path)
    assert cli("import", str(path)).code == 2
    assert "--yes" in cli("import", str(path)).err
    prompts: list[str] = []
    refused = answering(cli, False, prompts)("import", str(path))
    assert refused.code == 1
    assert "not confirmed" in refused.err
    assert "import these changes?" in prompts[0]
    assert count(db, "domains") == 0
    assert trigger_lines(tmp_path) == []
    dry = cli("import", str(path), "--dry-run")
    assert dry.code == 0
    assert "1 added" in dry.out
    assert count(db, "domains") == 0
    assert trigger_lines(tmp_path) == []
    sudo = replace(cli, runtime=replace(cli.runtime, user=lambda: "bob"))
    added = sudo("import", str(path), "--yes")
    assert added.code == 0
    assert "1 added" in added.out
    assert trigger_lines(tmp_path) == [f"{NOW} cli:bob"]
    assert list_events(db, EventFilter(5))[0].actor == "cli:bob"
    assert reading(cli, "other.com\n")("import", "-", "--yes").code == 0
    assert domain(db, "other.com").id


def test_a_canonical_flip_is_not_counted_twice(cli: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    paste(db, "example.com")
    path = write(tmp_path, "www.example.com\n")
    result = cli("import", str(path), "--mode", "replace", "--yes")
    assert result.code == 0
    assert result.err == ""
    assert "1 updated" in result.out
    assert "(1 canonical)" in result.out
    assert "2 updated" not in result.out
    assert domain(db, "www.example.com").id == domain(db, "example.com").id
    assert trigger_lines(tmp_path) == [f"{NOW} {ACTOR}"]


def test_import_reports_errors_with_line_numbers_and_filters_domains(cli: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    video = make_group(db, "Video")
    paste(db, "a.com b.com")
    join(db, domain(db, "a.com").id, video)
    bad = tmp_path / "bad.txt"
    bad.write_text("# subnet-tracker export v9\n", encoding="utf-8")
    rejected = cli("import", str(bad), "--yes")
    assert rejected.code == 2
    assert "line 1:" in rejected.err
    assert count(db, "domains") == 2
    missing = cli("import", str(write(tmp_path)), "--group", "NONE", "--yes")
    assert missing.code == 2
    assert "NONE" in missing.err
    scoped = cli("import", str(write(tmp_path, "c.com\n")), "--group", "none", "--mode", "replace", "--yes")
    assert scoped.code == 0
    assert domain(db, "c.com").id
    assert domain(db, "a.com").id
    assert db.execute("SELECT 1 FROM names WHERE name = 'b.com'").fetchone() is None
    assert find_group(db, "Video") is not None
    repeat = cli("import", str(write(tmp_path, "c.com\nwww.c.com\n")), "--yes")
    assert repeat.code == 0
    assert "repeats" in repeat.err
    assert trigger_lines(tmp_path) == [f"{NOW} {ACTOR}"]


def test_a_group_description_notifies_nobody(cli: Cli, db: sqlite3.Connection, tmp_path: Path) -> None:
    make_group(db, "Video")
    path = write(tmp_path, '[group "Video"]\ndescription = hosting\n')
    result = cli("import", str(path), "--yes")
    assert result.code == 0
    assert trigger_lines(tmp_path) == []
