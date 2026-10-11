import threading
from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import ip_address

from subnet_tracker.discovery.fetch import HttpResponse
from subnet_tracker.types import IPAddress


@dataclass(frozen=True, slots=True)
class Sent:
    url: str
    pin: IPAddress
    timeout: float
    user_agent: str
    max_bytes: int


class FakeDns:
    def __init__(self, records: Mapping[str, tuple[str, ...]]) -> None:
        self.records = records
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def resolve(self, host: str) -> tuple[IPAddress, ...]:
        with self._lock:
            self.calls.append(host)
        return tuple(ip_address(item) for item in self.records.get(host, ()))


class FakeExchange:
    def __init__(self, routes: Mapping[str, HttpResponse | Exception]) -> None:
        self.routes = routes
        self.sent: list[Sent] = []
        self._lock = threading.Lock()

    def send(self, url: str, headers: Mapping[str, str], pin: IPAddress, timeout: float, max_bytes: int) -> HttpResponse:
        with self._lock:
            self.sent.append(Sent(url, pin, timeout, headers.get("user-agent", ""), max_bytes))
        route = self.routes[url]
        if isinstance(route, Exception):
            raise route
        return route
