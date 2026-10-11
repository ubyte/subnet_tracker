import re
import tomllib
from ipaddress import ip_address, ip_network
from pathlib import Path

import pytest

from subnet_tracker.config import Config, ConfigError, Endpoint, load_config, parse_config
from subnet_tracker.types import parse_ip_range

EXAMPLE = """
db = "/var/lib/subnet-tracker/state.db"
pki_dir = "/var/lib/subnet-tracker/pki"

[web]
bind = ["10.8.0.1:8080", "127.0.0.1:8080", "[::1]:8080"]
allowed_hosts = ["vpn.example.net"]
trusted_networks = ["127.0.0.0/8", "::1", "10.8.0.0/24", "10.8.0.2-10.8.0.9"]
admin_password_hash = "pbkdf2:sha256:600000$salt$hash"
remember_for = "365d"
secret_key_file = "/var/lib/subnet-tracker/secret_key"

[agent_api]
listen = ["10.8.0.1:8443"]
endpoints = ["https://10.8.0.1:8443"]

[routes]
file = "/etc/openvpn/server/subnet-tracker/routes.conf"
server_addresses = ["203.0.113.10", "2001:db8::10"]
tunnel_networks = ["10.8.0.0/24"]

[[openvpn]]
name = "server"
management = "unix:/run/subnet-tracker/ovpn-server.sock"

[[openvpn]]
name = "tcp"
management = "unix:/run/subnet-tracker/ovpn-tcp.sock"
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def parse_text(text: str) -> Config:
    return parse_config(tomllib.loads(text))


def test_example(tmp_path: Path) -> None:
    c = load_config(write(tmp_path, EXAMPLE))
    assert c.db == Path("/var/lib/subnet-tracker/state.db")
    assert c.web.bind == (Endpoint(ip_address("10.8.0.1"), 8080), Endpoint(ip_address("127.0.0.1"), 8080), Endpoint(ip_address("::1"), 8080))
    assert c.web.allowed_hosts == ("vpn.example.net",)
    assert c.web.trusted_networks[-1] == parse_ip_range("10.8.0.2-10.8.0.9")
    assert c.web.remember_for == 365 * 86400
    assert c.agent_api.endpoints == ("https://10.8.0.1:8443",)
    assert c.routes.server_addresses == (ip_address("203.0.113.10"), ip_address("2001:db8::10"))
    assert c.routes.tunnel_networks == (ip_network("10.8.0.0/24"),)
    assert [(o.name, o.management) for o in c.openvpn] == [("server", Path("/run/subnet-tracker/ovpn-server.sock")), ("tcp", Path("/run/subnet-tracker/ovpn-tcp.sock"))]


def test_empty_file_gives_defaults() -> None:
    c = parse_config({})
    assert c == Config()
    assert c.web.trusted_networks == (parse_ip_range("127.0.0.0/8"), parse_ip_range("::1"), parse_ip_range("10.8.0.0/24"))
    assert c.web.admin_password_hash == ""
    assert c.openvpn == ()


def test_endpoint_str() -> None:
    assert str(Endpoint(ip_address("10.8.0.1"), 8080)) == "10.8.0.1:8080"
    assert str(Endpoint(ip_address("::1"), 8080)) == "[::1]:8080"


@pytest.mark.parametrize(
    ("text", "where"),
    [
        ('dbx = "x"', "dbx"),
        ('[web]\nbindx = ["10.8.0.1:80"]', "web.bindx"),
        ("[[openvpn]]\nname = 'a'\nmanagement = 'unix:/a'\nextra = 1", "openvpn[0].extra"),
        ("[surprise]", "surprise"),
    ],
)
def test_unknown_key_is_error(text: str, where: str) -> None:
    with pytest.raises(ConfigError, match=rf"^{re.escape(where)}: unknown"):
        parse_text(text)


@pytest.mark.parametrize(
    ("text", "where"),
    [
        ("db = 5", "db"),
        ('db = ""', "db"),
        ("[web]\nbind = '10.8.0.1:80'", "web.bind"),
        ("[web]\nbind = ['10.8.0.1']", "web.bind[0]"),
        ("[web]\nbind = ['10.8.0.1:0']", "web.bind[0]"),
        ("[web]\nbind = ['::1:80']", "web.bind[0]"),
        ("[web]\nbind = ['host:80']", "web.bind[0]"),
        ("[web]\ntrusted_networks = ['10.8.0.9-10.8.0.2']", "web.trusted_networks[0]"),
        ("[web]\ntrusted_networks = ['10.8.0.1-::1']", "web.trusted_networks[0]"),
        ("[web]\nremember_for = 365", "web.remember_for"),
        ("[web]\nremember_for = '1y'", "web.remember_for"),
        ("[agent_api]\nendpoints = ['http://10.8.0.1:8443']", "agent_api.endpoints[0]"),
        ("[agent_api]\nendpoints = ['https://10.8.0.1:99999']", "agent_api.endpoints[0]"),
        ("[routes]\nserver_addresses = ['10.0.0.0/8']", "routes.server_addresses[0]"),
        ("[routes]\ntunnel_networks = ['10.8.0.1/24']", "routes.tunnel_networks[0]"),
        ("web = 1", "web"),
        ("[[openvpn]]\nname = 'a'", "openvpn[0].management"),
        ("[[openvpn]]\nname = 'a'\nmanagement = '/run/a.sock'", "openvpn[0].management"),
        ("[[openvpn]]\nname = 'a'\nmanagement = 'unix:a.sock'", "openvpn[0].management"),
        ("[[openvpn]]\nname = 'a'\nmanagement = 'unix:/a'\n[[openvpn]]\nname = 'a'\nmanagement = 'unix:/b'", "openvpn[1].name"),
        ("[[openvpn]]\nname = ''\nmanagement = 'unix:/a'", "openvpn[0].name"),
    ],
)
def test_bad_value_is_error(text: str, where: str) -> None:
    with pytest.raises(ConfigError, match=rf"^{re.escape(where)}: "):
        parse_text(text)


def test_toml_syntax_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"config\.toml"):
        load_config(write(tmp_path, "db = "))


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"config\.toml"):
        load_config(tmp_path / "config.toml")
