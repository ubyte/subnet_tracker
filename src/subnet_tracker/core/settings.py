import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, replace
from enum import StrEnum, auto
from typing import Any

from subnet_tracker.config import IPNetwork
from subnet_tracker.core.db import transaction
from subnet_tracker.core.setting_codecs import DECIMAL, DOMAINS, DURATION, FLAG, INTEGER, NETWORKS, SIZE, TEXT, Codec, Validate, accept, at_least, between, choice_codec, printable_ascii, ratio
from subnet_tracker.types import Bytes, ReconnectMode, Seconds, Timestamp, format_duration, format_size, parse_duration, parse_size

_SPEC = "setting"
_CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"


class BrowserRefreshMode(StrEnum):
    AUTO = auto()
    BUTTON = auto()
    OFF = auto()


class ApplyStrategy(StrEnum):
    RECONNECT = auto()
    NONE = auto()


class SettingsError(Exception):
    pass


def _setting[T](codec: Codec[T], default: T, validate: Validate[T] = accept) -> T:
    return field(default=default, metadata={_SPEC: (codec, validate)})


def _duration(default: str, minimum: int = 0) -> Seconds:
    return _setting(DURATION, parse_duration(default), at_least(Seconds(minimum), format_duration))


def _integer(default: int, low: int = 1, high: int | None = None) -> int:
    return _setting(INTEGER, default, at_least(low) if high is None else between(low, high))


def _concurrency(default: int) -> int:
    return _integer(default, 1, 1024)


def _choice[E: StrEnum](default: E, *allowed: E) -> E:
    return _setting(choice_codec(allowed or tuple(type(default))), default)


@dataclass(frozen=True, slots=True)
class ResolveSettings:
    interval: Seconds = _duration("1h", minimum=1)
    timeout: Seconds = _duration("3s", minimum=1)
    concurrency: int = _concurrency(32)
    unhealthy_ratio: float = _setting(DECIMAL, 0.5, ratio)


@dataclass(frozen=True, slots=True)
class ResolverSettings:
    stale_warning: Seconds = _duration("2d")


@dataclass(frozen=True, slots=True)
class BrowserSettings:
    refresh_mode: BrowserRefreshMode = _choice(BrowserRefreshMode.AUTO)
    max_age: Seconds = _duration("1d")
    batch: int = _integer(300)
    concurrency: int = _concurrency(6)


@dataclass(frozen=True, slots=True)
class AgentSettings:
    max_age: Seconds = _duration("1h")
    batch: int = _integer(200)
    poll_interval: Seconds = _duration("1m", minimum=1)
    concurrency: int = _concurrency(8)
    timeout: Seconds = _duration("5s", minimum=1)
    offline_after: Seconds = _duration("30m")


@dataclass(frozen=True, slots=True)
class RoutesSettings:
    retention: Seconds = _duration("4d")
    default_prefix_v4: int = _integer(23, 1, 32)
    default_prefix_v6: int = _integer(48, 1, 128)
    push_ipv4: bool = _setting(FLAG, True)
    push_ipv6: bool = _setting(FLAG, True)
    exclude: tuple[IPNetwork, ...] = _setting(NETWORKS, ())
    max_v4: int = _integer(1000)
    max_v6: int = _integer(300)
    min_aggregate_prefix_v4: int = _integer(12, 1, 32)
    min_aggregate_prefix_v6: int = _integer(32, 1, 128)


@dataclass(frozen=True, slots=True)
class ApplySettings:
    strategy: ApplyStrategy = _choice(ApplyStrategy.RECONNECT)
    after_add: ReconnectMode = _choice(ReconnectMode.ON_LEAVE, ReconnectMode.ON_LEAVE, ReconnectMode.AUTO, ReconnectMode.NOW)
    min_interval: Seconds = _duration("1h")
    idle_delay: Seconds = _duration("30s")
    leave_grace: Seconds = _duration("5s")
    reconnect_spread: Seconds = _duration("2s")


@dataclass(frozen=True, slots=True)
class DiscoverySettings:
    timeout: Seconds = _duration("10s", minimum=1)
    max_bytes: Bytes = _setting(SIZE, parse_size("5MB"), at_least(Bytes(1), format_size))
    user_agent: str = _setting(TEXT, _CHROME_UA, printable_ascii)
    ignore: tuple[str, ...] = _setting(DOMAINS, ("w3.org", "schema.org", "ogp.me"))


@dataclass(frozen=True, slots=True)
class HistorySettings:
    retention: Seconds = _duration("90d")


@dataclass(frozen=True, slots=True)
class Settings:
    resolve: ResolveSettings = field(default_factory=ResolveSettings)
    resolvers: ResolverSettings = field(default_factory=ResolverSettings)
    browser: BrowserSettings = field(default_factory=BrowserSettings)
    agent: AgentSettings = field(default_factory=AgentSettings)
    routes: RoutesSettings = field(default_factory=RoutesSettings)
    apply: ApplySettings = field(default_factory=ApplySettings)
    discovery: DiscoverySettings = field(default_factory=DiscoverySettings)
    history: HistorySettings = field(default_factory=HistorySettings)


@dataclass(frozen=True, slots=True)
class SettingError:
    key: str
    message: str


@dataclass(frozen=True, slots=True)
class SettingValue:
    key: str
    text: str
    json: str
    is_default: bool


@dataclass(frozen=True, slots=True)
class SettingState:
    key: str
    value: str
    default: str
    updated_at: Timestamp | None

    @property
    def changed(self) -> bool:
        return self.value != self.default


@dataclass(frozen=True, slots=True)
class Setting[T]:
    section: str
    name: str
    default: T
    codec: Codec[T]
    validate: Validate[T]

    @property
    def key(self) -> str:
        return f"{self.section}.{self.name}"

    def _checked[R](self, convert: Callable[[R], T], raw: R) -> T | SettingError:
        try:
            value = convert(raw)
            self.validate(value)
        except ValueError as e:
            return SettingError(self.key, str(e))
        return value

    def parse(self, text: str) -> T | SettingError:
        return self._checked(self.codec.parse, text)

    def decode(self, raw: str) -> T | SettingError:
        return self._checked(lambda r: self.codec.decode(json.loads(r)), raw)

    def format(self, value: T) -> str:
        return self.codec.format(value)

    def encode(self, value: T) -> str:
        return json.dumps(self.codec.encode(value))


_DEFAULTS = Settings()


def _section_settings(name: str) -> list[Setting[Any]]:
    section = getattr(_DEFAULTS, name)
    return [Setting(name, f.name, getattr(section, f.name), *f.metadata[_SPEC]) for f in fields(section)]


_REGISTRY = {s.key: s for section in fields(_DEFAULTS) for s in _section_settings(section.name)}
SETTING_KEYS = tuple(_REGISTRY)


def _unknown(key: str) -> SettingError:
    return SettingError(key, f"unknown setting {key!r}")


def _value[T](setting: Setting[T], value: T) -> SettingValue:
    text = setting.format(value)
    return SettingValue(setting.key, text, setting.encode(value), text == setting.format(setting.default))


def parse_setting(key: str, text: str) -> SettingValue | SettingError:
    if (setting := _REGISTRY.get(key)) is None:
        return _unknown(key)
    value = setting.parse(text)
    return value if isinstance(value, SettingError) else _value(setting, value)


def _decoded[T](setting: Setting[T], raw: str) -> T:
    value = setting.decode(raw)
    if isinstance(value, SettingError):
        raise SettingsError(f"stored value {raw} of setting {setting.key} is invalid: {value.message}; reset it to the default with 'subnet-tracker settings reset {setting.key}'")
    return value


def _settings(values: Mapping[str, object]) -> Settings:
    def section(name: str) -> Any:
        return replace(getattr(_DEFAULTS, name), **{s.name: v for key, v in values.items() if (s := _REGISTRY[key]).section == name})

    return Settings(**{f.name: section(f.name) for f in fields(_DEFAULTS)})


def _state[T](setting: Setting[T], row: sqlite3.Row | None) -> SettingState:
    default = setting.format(setting.default)
    if row is None:
        return SettingState(setting.key, default, default, None)
    return SettingState(setting.key, setting.format(_decoded(setting, row["value"])), default, Timestamp(row["updated_at"]))


def _stored(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {row["key"]: row for row in conn.execute("SELECT key, value, updated_at FROM settings") if row["key"] in _REGISTRY}


def load_settings(conn: sqlite3.Connection) -> Settings:
    return _settings({key: _decoded(_REGISTRY[key], row["value"]) for key, row in _stored(conn).items()})


def list_settings(conn: sqlite3.Connection) -> tuple[SettingState, ...]:
    stored = _stored(conn)
    return tuple(_state(setting, stored.get(key)) for key, setting in _REGISTRY.items())


def get_setting(conn: sqlite3.Connection, key: str) -> SettingState | SettingError:
    if (setting := _REGISTRY.get(key)) is None:
        return _unknown(key)
    return _state(setting, conn.execute("SELECT value, updated_at FROM settings WHERE key = ?", (key,)).fetchone())


_UPSERT = "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at"


def save_setting(conn: sqlite3.Connection, value: SettingValue, now: Timestamp) -> None:
    if value.is_default:
        conn.execute("DELETE FROM settings WHERE key = ?", (value.key,))
    else:
        conn.execute(_UPSERT, (value.key, value.json, now))


def set_setting(conn: sqlite3.Connection, key: str, text: str, now: Timestamp) -> SettingValue | SettingError:
    value = parse_setting(key, text)
    if isinstance(value, SettingValue):
        with transaction(conn):
            save_setting(conn, value, now)
    return value


def reset_setting(conn: sqlite3.Connection, key: str) -> SettingError | None:
    if key not in _REGISTRY:
        return _unknown(key)
    conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    return None
