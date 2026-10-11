from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "deploy" / "systemd"
TMPFILES = ROOT / "deploy" / "tmpfiles.d" / "subnet-tracker.conf"
TRIGGER_LINE = "f /run/subnet-tracker/trigger 0660 subnet-tracker subnet-tracker -"

TICK_SERVICE = """\
# deploy/systemd/subnet-tracker-tick.service
[Unit]
Description=subnet-tracker tick
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=subnet-tracker
Group=subnet-tracker
ExecStart=/opt/subnet-tracker/bin/subnet-tracker tick
"""

TICK_TIMER = """\
# deploy/systemd/subnet-tracker-tick.timer
[Unit]
Description=Run subnet-tracker tick every minute

[Timer]
OnCalendar=minutely
AccuracySec=1s
Unit=subnet-tracker-tick.service

[Install]
WantedBy=timers.target
"""

TICK_PATH = """\
# deploy/systemd/subnet-tracker-tick.path
[Unit]
Description=Wake subnet-tracker tick when the trigger file changes
After=systemd-tmpfiles-setup.service

[Path]
PathModified=/run/subnet-tracker/trigger
Unit=subnet-tracker-tick.service

[Install]
WantedBy=multi-user.target
"""

TMPFILES_CONF = """\
d /run/subnet-tracker 0770 subnet-tracker subnet-tracker -
f /run/subnet-tracker/trigger 0660 subnet-tracker subnet-tracker -
"""


def directives(text: str) -> dict[tuple[str, str], str]:
    section = ""
    found: dict[tuple[str, str], str] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        key, _, value = line.partition("=")
        found[section, key.strip()] = value.strip()
    return found


def tmpfiles_rows(text: str) -> list[list[str]]:
    return [line.split() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def test_tick_service_is_oneshot_as_subnet_tracker() -> None:
    text = (SYSTEMD / "subnet-tracker-tick.service").read_text()
    fields = directives(text)
    assert fields["Service", "Type"] == "oneshot"
    assert fields["Service", "User"] == "subnet-tracker"
    assert fields["Service", "Group"] == "subnet-tracker"
    assert fields["Service", "ExecStart"] == "/opt/subnet-tracker/bin/subnet-tracker tick"
    assert fields["Service", "ExecStart"].endswith("subnet-tracker tick")
    assert ("Service", "RuntimeDirectory") not in fields
    assert "Install" not in {section for section, _key in fields}
    assert text == TICK_SERVICE


def test_tick_timer_fires_every_minute() -> None:
    text = (SYSTEMD / "subnet-tracker-tick.timer").read_text()
    fields = directives(text)
    assert fields["Timer", "OnCalendar"] == "minutely"
    assert fields["Timer", "AccuracySec"] == "1s"
    assert fields["Timer", "Unit"] == "subnet-tracker-tick.service"
    assert ("Timer", "Persistent") not in fields
    assert fields["Install", "WantedBy"] == "timers.target"
    assert text == TICK_TIMER


def test_tick_path_wakes_on_the_trigger_file() -> None:
    text = (SYSTEMD / "subnet-tracker-tick.path").read_text()
    fields = directives(text)
    assert fields["Path", "PathModified"] == "/run/subnet-tracker/trigger"
    assert fields["Path", "Unit"] == "subnet-tracker-tick.service"
    assert fields["Unit", "After"] == "systemd-tmpfiles-setup.service"
    assert fields["Install", "WantedBy"] == "multi-user.target"
    assert text == TICK_PATH


def test_tmpfiles_creates_runtime_directory_and_trigger() -> None:
    text = TMPFILES.read_text()
    rows = tmpfiles_rows(text)
    assert rows[0] == ["d", "/run/subnet-tracker", "0770", "subnet-tracker", "subnet-tracker", "-"]
    assert rows[1] == ["f", "/run/subnet-tracker/trigger", "0660", "subnet-tracker", "subnet-tracker", "-"]
    assert int(rows[0][2], 8) == 0o770
    assert int(rows[1][2], 8) == 0o660
    assert TRIGGER_LINE in text.splitlines()
    paths = {row[1] for row in rows}
    assert paths == {"/run/subnet-tracker", "/run/subnet-tracker/trigger"}
    assert text == TMPFILES_CONF
