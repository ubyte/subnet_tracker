import tomllib
from ipaddress import IPv4Address, IPv4Network, IPv6Address
from pathlib import Path

from subnet_tracker.config import Endpoint, OpenVpnInstance, parse_config
from subnet_tracker.types import parse_ip_range

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "deploy" / "install.sh"
NFTABLES = ROOT / "deploy" / "nftables" / "subnet-tracker.nft"
SYSTEMD = ROOT / "deploy" / "systemd"
UNIT_NAMES = ("subnet-tracker-tick.path", "subnet-tracker-tick.service", "subnet-tracker-tick.timer")


def install_script() -> str:
    return INSTALL.read_text(encoding="utf-8")


def design_fence(language: str) -> str:
    design = (ROOT / "docs" / "design.md").read_text(encoding="utf-8")
    marker = f"```{language}\n"
    start = design.index(marker) + len(marker)
    return design[start : design.index("\n```", start)] + "\n"


def test_install_script_creates_the_user_and_directories() -> None:
    script = install_script()
    assert script.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail\n" in script
    assert 'if [ "$(id -u)" -ne 0 ]; then\n' in script
    lines = (
        "groupadd --system subnet-tracker\n",
        "useradd --system --gid subnet-tracker --home-dir /var/lib/subnet-tracker --no-create-home subnet-tracker\n",
        "chown root:subnet-tracker /etc/subnet-tracker\n",
        "chmod 0750 /etc/subnet-tracker\n",
        "chown subnet-tracker:subnet-tracker /var/lib/subnet-tracker\n",
        "chmod 2775 /var/lib/subnet-tracker\n",
        "chown subnet-tracker:subnet-tracker /var/lib/subnet-tracker/pki\n",
        "chmod 0700 /var/lib/subnet-tracker/pki\n",
        "chown root:root /opt/subnet-tracker\n",
        "chmod 0755 /opt/subnet-tracker\n",
        "chown root:root /opt/subnet-tracker/agent\n",
        "chmod 0755 /opt/subnet-tracker/agent\n",
        "chown subnet-tracker:subnet-tracker /etc/openvpn/server/subnet-tracker\n",
        "chmod 0755 /etc/openvpn/server/subnet-tracker\n",
        "chown root:root /usr/lib/subnet-tracker\n",
        "chmod 0755 /usr/lib/subnet-tracker\n",
    )
    for line in lines:
        assert line in script
    assert "apt-get" not in script
    assert "apt install" not in script


def test_install_script_builds_the_venv_and_writes_server_addresses_once() -> None:
    script = install_script()
    assert "python3.12 -m venv /opt/subnet-tracker\n" in script
    assert '/opt/subnet-tracker/bin/pip install "$ROOT"\n' in script
    assert "ip -o addr show scope global" in script
    assert "trusted=(127.0.0.0/8 ::1)\n" in script
    assert "tunnel_networks=(10.8.0.0/24)\n" in script
    assert 'trusted+=("$net")\n' in script
    assert "server_addresses = ${server_addresses}\n" in script
    assert "trusted_networks = ${trusted_toml}\n" in script
    assert "tunnel_networks = ${tunnel_toml}\n" in script
    assert "if [ ! -e /etc/subnet-tracker/config.toml ]; then\n" in script
    assert 'chown root:subnet-tracker "$config_tmp"\n' in script
    assert 'chmod 0640 "$config_tmp"\n' in script


def test_config_template_parses_as_section_13() -> None:
    script = install_script()
    marker = 'cat > "$config_tmp" <<EOF\n'
    start = script.index(marker) + len(marker)
    body = script[start : script.index("\nEOF\n", start)]
    rendered = body.replace("${trusted_toml}", '["127.0.0.0/8", "::1", "10.8.0.0/24"]')
    rendered = rendered.replace("${server_addresses}", '["203.0.113.10", "2001:db8::10"]')
    rendered = rendered.replace("${tunnel_toml}", '["10.8.0.0/24"]')
    config = parse_config(tomllib.loads(rendered))
    assert config.db == Path("/var/lib/subnet-tracker/state.db")
    assert config.pki_dir == Path("/var/lib/subnet-tracker/pki")
    assert config.web.bind == (Endpoint(IPv4Address("10.8.0.1"), 8080), Endpoint(IPv4Address("127.0.0.1"), 8080))
    assert config.web.trusted_networks == (parse_ip_range("127.0.0.0/8"), parse_ip_range("::1"), parse_ip_range("10.8.0.0/24"))
    assert config.web.secret_key_file == Path("/var/lib/subnet-tracker/secret_key")
    assert config.agent_api.listen == (Endpoint(IPv4Address("10.8.0.1"), 8443),)
    assert config.agent_api.endpoints == ("https://10.8.0.1:8443",)
    assert config.routes.file == Path("/etc/openvpn/server/subnet-tracker/routes.conf")
    assert config.routes.server_addresses == (IPv4Address("203.0.113.10"), IPv6Address("2001:db8::10"))
    assert config.routes.tunnel_networks == (IPv4Network("10.8.0.0/24"),)
    assert config.openvpn == (OpenVpnInstance(name="server", management=Path("/run/subnet-tracker/ovpn-server.sock")),)


def test_install_script_copies_the_existing_units_and_tmpfiles() -> None:
    script = install_script()
    present = tuple(sorted(path.name for path in SYSTEMD.iterdir() if path.is_file()))
    assert present == UNIT_NAMES
    for name in present:
        assert f'"$ROOT/deploy/systemd/{name}" /etc/systemd/system/{name}\n' in script
    assert '"$ROOT/deploy/tmpfiles.d/subnet-tracker.conf" /etc/tmpfiles.d/subnet-tracker.conf\n' in script
    assert "systemd-tmpfiles --create /etc/tmpfiles.d/subnet-tracker.conf\n" in script
    assert "f /run/subnet-tracker/trigger" not in script
    assert "systemctl daemon-reload\n" in script
    assert "systemctl enable subnet-tracker-tick.timer subnet-tracker-tick.path\n" in script
    assert "systemctl start subnet-tracker-tick.timer subnet-tracker-tick.path\n" in script
    assert "systemctl enable subnet-tracker-tick.service" not in script
    assert "subnet-tracker-web.service" not in script
    assert "subnet-tracker-agent-api.service" not in script
    assert "RuntimeDirectory" not in script
    assert "[Service]" not in script
    assert "[Install]" not in script
    assert "ovpn-client-connect" not in script


def test_nftables_snippet_drops_the_ui_port_off_the_tunnel_and_lo() -> None:
    text = NFTABLES.read_text(encoding="utf-8")
    script = install_script()
    assert text == design_fence("nft")
    assert text.count("dport") == 1
    assert "tcp dport 8080" in text
    assert "8443" not in text
    assert 'tcp dport 8080 iifname != "lo" iifname != "tun0" drop\n' in text
    assert "policy accept" in text
    assert "iifname" not in script
    assert "dport" not in script
    assert '"$ROOT/deploy/nftables/subnet-tracker.nft" /etc/subnet-tracker/nftables.conf\n' in script
    assert "nft -f /etc/subnet-tracker/nftables.conf\n" in script


def test_sysctl_sets_rp_filter_on_external_interfaces() -> None:
    design = (ROOT / "docs" / "design.md").read_text(encoding="utf-8")
    script = install_script()
    assert "net.ipv4.conf.<iface>.rp_filter = 1" in design
    assert "net.ipv4.conf.%s.rp_filter = 1" in script
    assert "lo|tun0" in script
    assert "net.ipv4.conf.all.rp_filter" not in script
    assert "sysctl -p /etc/sysctl.d/subnet-tracker.conf\n" in script
