import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address, ip_network
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn
from urllib.parse import urlsplit

from subnet_tracker.types import IPAddress, IpRange, Seconds, parse_duration, parse_ip_range

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

type IPNetwork = IPv4Network | IPv6Network
type Parse[T] = Callable[[object, str], T]

_YEAR = Seconds(365 * 86400)


class ConfigError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Endpoint:
    ip: IPAddress
    port: int

    def __str__(self) -> str:
        return f"[{self.ip}]:{self.port}" if self.ip.version == 6 else f"{self.ip}:{self.port}"


@dataclass(frozen=True, slots=True)
class WebConfig:
    bind: tuple[Endpoint, ...] = (Endpoint(IPv4Address("10.8.0.1"), 8080), Endpoint(IPv4Address("127.0.0.1"), 8080))
    allowed_hosts: tuple[str, ...] = ()
    trusted_networks: tuple[IpRange, ...] = tuple(map(parse_ip_range, ("127.0.0.0/8", "::1", "10.8.0.0/24")))
    admin_password_hash: str = ""
    remember_for: Seconds = _YEAR
    secret_key_file: Path = Path("/var/lib/subnet-tracker/secret_key")


@dataclass(frozen=True, slots=True)
class AgentApiConfig:
    listen: tuple[Endpoint, ...] = (Endpoint(IPv4Address("10.8.0.1"), 8443),)
    endpoints: tuple[str, ...] = ("https://10.8.0.1:8443",)


@dataclass(frozen=True, slots=True)
class RoutesConfig:
    file: Path = Path("/etc/openvpn/server/subnet-tracker/routes.conf")
    server_addresses: tuple[IPAddress, ...] = ()
    tunnel_networks: tuple[IPNetwork, ...] = (IPv4Network("10.8.0.0/24"),)


@dataclass(frozen=True, slots=True)
class OpenVpnInstance:
    name: str
    management: Path


@dataclass(frozen=True, slots=True)
class Config:
    db: Path = Path("/var/lib/subnet-tracker/state.db")
    pki_dir: Path = Path("/var/lib/subnet-tracker/pki")
    web: WebConfig = field(default_factory=WebConfig)
    agent_api: AgentApiConfig = field(default_factory=AgentApiConfig)
    routes: RoutesConfig = field(default_factory=RoutesConfig)
    openvpn: tuple[OpenVpnInstance, ...] = ()


def _fail(where: str, message: str) -> NoReturn:
    raise ConfigError(f"{where}: {message}")


@dataclass(frozen=True, slots=True)
class _Table:
    data: Mapping[str, object]
    where: str

    def at(self, key: str) -> str:
        return f"{self.where}.{key}" if self.where else key

    def get[T](self, key: str, parse: Parse[T], default: T) -> T:
        return parse(self.data[key], self.at(key)) if key in self.data else default

    def require[T](self, key: str, parse: Parse[T]) -> T:
        if key not in self.data:
            _fail(self.at(key), "required key is missing")
        return parse(self.data[key], self.at(key))


def _table(value: object, where: str, shape: "type[DataclassInstance]") -> _Table:
    if not isinstance(value, dict):
        _fail(where, "expected a table")
    table = _Table(value, where)
    known = {f.name for f in fields(shape)}
    for key in value:
        if key not in known:
            _fail(table.at(key), "unknown key")
    return table


def _text(value: object, where: str) -> str:
    if not isinstance(value, str):
        _fail(where, "expected a string")
    return value


def _nonempty(value: object, where: str) -> str:
    if not (text := _text(value, where)):
        _fail(where, "must not be empty")
    return text


def _path(value: object, where: str) -> Path:
    return Path(_nonempty(value, where))


def _array[T](item: Parse[T]) -> Parse[tuple[T, ...]]:
    def parse(value: object, where: str) -> tuple[T, ...]:
        if not isinstance(value, list):
            _fail(where, "expected an array")
        return tuple(item(v, f"{where}[{i}]") for i, v in enumerate(value))

    return parse


def _converted[T](convert: Callable[[str], T]) -> Parse[T]:
    def parse(value: object, where: str) -> T:
        text = _text(value, where)
        try:
            return convert(text)
        except ValueError as e:
            _fail(where, str(e))

    return parse


_ENDPOINT = re.compile(r"(?:\[(?P<v6>[^\]]+)\]|(?P<v4>[^:\[\]]+)):(?P<port>[0-9]{1,5})")


def parse_endpoint(text: str) -> Endpoint:
    if not (m := _ENDPOINT.fullmatch(text)) or not 1 <= (port := int(m["port"])) <= 65535:
        raise ValueError(f"invalid endpoint {text!r}, expected ipv4:port or [ipv6]:port")
    ip: IPAddress = IPv6Address(m["v6"]) if m["v6"] else IPv4Address(m["v4"])
    return Endpoint(ip, port)


def parse_https_url(text: str) -> str:
    url = urlsplit(text)
    if url.scheme != "https" or not url.hostname:
        raise ValueError(f"invalid endpoint {text!r}, expected an https:// URL")
    return text


def parse_unix_socket(text: str) -> Path:
    path = Path(text.removeprefix("unix:"))
    if not text.startswith("unix:") or not path.is_absolute():
        raise ValueError(f"invalid management socket {text!r}, expected unix:/absolute/path")
    return path


_endpoints = _array(_converted(parse_endpoint))


def _web(value: object, where: str) -> WebConfig:
    t, d = _table(value, where, WebConfig), WebConfig()
    return WebConfig(
        bind=t.get("bind", _endpoints, d.bind),
        allowed_hosts=t.get("allowed_hosts", _array(_nonempty), d.allowed_hosts),
        trusted_networks=t.get("trusted_networks", _array(_converted(parse_ip_range)), d.trusted_networks),
        admin_password_hash=t.get("admin_password_hash", _text, d.admin_password_hash),
        remember_for=t.get("remember_for", _converted(parse_duration), d.remember_for),
        secret_key_file=t.get("secret_key_file", _path, d.secret_key_file),
    )


def _agent_api(value: object, where: str) -> AgentApiConfig:
    t, d = _table(value, where, AgentApiConfig), AgentApiConfig()
    return AgentApiConfig(listen=t.get("listen", _endpoints, d.listen), endpoints=t.get("endpoints", _array(_converted(parse_https_url)), d.endpoints))


def _routes(value: object, where: str) -> RoutesConfig:
    t, d = _table(value, where, RoutesConfig), RoutesConfig()
    return RoutesConfig(
        file=t.get("file", _path, d.file),
        server_addresses=t.get("server_addresses", _array(_converted(ip_address)), d.server_addresses),
        tunnel_networks=t.get("tunnel_networks", _array(_converted(ip_network)), d.tunnel_networks),
    )


def _openvpn_instance(value: object, where: str) -> OpenVpnInstance:
    t = _table(value, where, OpenVpnInstance)
    return OpenVpnInstance(name=t.require("name", _nonempty), management=t.require("management", _converted(parse_unix_socket)))


def _openvpn(value: object, where: str) -> tuple[OpenVpnInstance, ...]:
    instances = _array(_openvpn_instance)(value, where)
    seen: set[str] = set()
    for i, instance in enumerate(instances):
        if instance.name in seen:
            _fail(f"{where}[{i}].name", f"duplicate instance name {instance.name!r}")
        seen.add(instance.name)
    return instances


def parse_config(data: Mapping[str, object]) -> Config:
    t, d = _table(dict(data), "", Config), Config()
    return Config(
        db=t.get("db", _path, d.db),
        pki_dir=t.get("pki_dir", _path, d.pki_dir),
        web=t.get("web", _web, d.web),
        agent_api=t.get("agent_api", _agent_api, d.agent_api),
        routes=t.get("routes", _routes, d.routes),
        openvpn=t.get("openvpn", _openvpn, d.openvpn),
    )


def load_config(path: Path) -> Config:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ConfigError(f"{path}: {e}") from e
    try:
        return parse_config(data)
    except ConfigError as e:
        raise ConfigError(f"{path}: {e}") from None
