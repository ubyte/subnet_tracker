import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OPENVPN = ROOT / "deploy" / "openvpn"
SCRIPT = OPENVPN / "ovpn-client-connect"
SNIPPET = OPENVPN / "server.conf.snippet"
ROUTES_FILE = "/etc/openvpn/server/subnet-tracker/routes.conf"
CONNECT = "/usr/lib/subnet-tracker/ovpn-client-connect"
SOCKET = "/run/subnet-tracker/ovpn-server.sock"

NAMED = [
    ("management", f"{SOCKET} unix"),
    ("management-client-group", "subnet-tracker"),
    ("script-security", "2"),
    ("client-connect", CONNECT),
]


def section_31() -> str:
    design = (ROOT / "docs" / "design.md").read_text(encoding="utf-8")
    return design[design.index("### 3.1 ") : design.index("### 3.2 ")]


def fenced(kind: str) -> str:
    blocks = re.findall(rf"^```{kind}\n(.*?)^```$", section_31(), re.DOTALL | re.MULTILINE)
    assert len(blocks) == 1
    block = blocks[0]
    assert isinstance(block, str)
    return block


def directives(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith(";"):
            continue
        key, _, value = line.partition(" ")
        found.append((key, value.strip()))
    return found


def runner(tmp_path: Path, routes: Path) -> Path:
    body = SCRIPT.read_text(encoding="utf-8")
    assert body.count(ROUTES_FILE) == 1
    path = tmp_path / "ovpn-client-connect"
    path.write_text(body.replace(ROUTES_FILE, str(routes), 1), encoding="utf-8")
    path.chmod(0o755)
    return path


def run(tmp_path: Path, routes: Path, dest: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(runner(tmp_path, routes)), str(dest)], check=False, capture_output=True, text=True)


def test_directive_parser_skips_comments_and_blank_lines() -> None:
    text = "# comment\n\n; also a comment\n  script-security 2  \nclient-connect /usr/lib/subnet-tracker/ovpn-client-connect\n"
    assert directives(text) == [("script-security", "2"), ("client-connect", CONNECT)]


def test_snippet_is_the_section_3_1_fragment_and_names_no_other_directive() -> None:
    text = SNIPPET.read_text(encoding="utf-8")
    assert text == fenced("")
    assert directives(text) == NAMED
    assert "push-reset" not in text
    assert "client-config-dir" not in text
    assert "client-disconnect" not in text
    assert "\nconfig " not in text
    assert "iroute" not in text
    assert "learn-address" not in text


def test_script_matches_design_and_only_appends_routes() -> None:
    script = fenced("sh")
    assert re.search(rf"^routes={re.escape(ROUTES_FILE)}$", script, re.M)
    assert re.search(r'^if \[ ! -e "\$routes" \]; then$', script, re.M)
    assert re.search(r"^    exit 0$", script, re.M)
    assert re.search(r'^cat "\$routes" >> "\$1"$', script, re.M)
    assert "push-reset" not in script
    assert SOCKET not in script
    assert "client-kill" not in script
    assert SCRIPT.read_text(encoding="utf-8") == script
    assert stat.S_IMODE(SCRIPT.stat().st_mode) == 0o755
    assert subprocess.run(["sh", "-n", str(SCRIPT)], check=False).returncode == 0


def test_missing_routes_file_exits_zero_and_writes_nothing(tmp_path: Path) -> None:
    dest = tmp_path / "client.conf"
    dest.write_text('push "route 10.9.0.0 255.255.0.0"\n', encoding="utf-8")
    routes = tmp_path / "routes.conf"
    result = run(tmp_path, routes, dest)
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert dest.read_text(encoding="utf-8") == 'push "route 10.9.0.0 255.255.0.0"\n'
    assert not routes.exists()


def test_script_appends_the_whole_routes_file_including_comments(tmp_path: Path) -> None:
    routes = tmp_path / "routes.conf"
    body = '# managed by subnet-tracker, do not edit\n# generated 2026-10-11T03:20:00Z, 1 IPv4 + 0 IPv6 routes\npush "route 149.154.160.0 255.255.240.0"\npush "route-ipv6 2001:67c:4e8::/48"\n'
    routes.write_text(body, encoding="utf-8")
    dest = tmp_path / "client.conf"
    dest.write_text('push "route 10.9.0.0 255.255.0.0"\n', encoding="utf-8")
    result = run(tmp_path, routes, dest)
    assert result.returncode == 0
    assert dest.read_text(encoding="utf-8") == 'push "route 10.9.0.0 255.255.0.0"\n' + body
    assert routes.read_text(encoding="utf-8") == body


def test_unreadable_routes_file_fails_the_connection(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can read a mode 000 file")
    routes = tmp_path / "routes.conf"
    body = 'push "route 9.0.0.0 255.0.0.0"\n'
    routes.write_text(body, encoding="utf-8")
    routes.chmod(0)
    dest = tmp_path / "client.conf"
    prior = 'push "route 10.9.0.0 255.255.0.0"\n'
    dest.write_text(prior, encoding="utf-8")
    try:
        result = run(tmp_path, routes, dest)
        assert result.returncode != 0
        assert dest.read_text(encoding="utf-8") == prior
    finally:
        routes.chmod(0o644)
    assert routes.read_text(encoding="utf-8") == body
