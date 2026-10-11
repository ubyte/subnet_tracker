import re

from subnet_tracker.discovery.parse import Network, Rejected
from subnet_tracker.discovery.www import domain_names
from subnet_tracker.textio.lines import GroupHeader, KeyValue, Section, domain_line, group_property, header, key_value, route_line
from subnet_tracker.textio.model import DomainLine, DuplicateLine, GroupSection, ImportFile, RouteLine, SettingLine
from subnet_tracker.types import DomainName, ParseError, ParseResult

FORMAT_VERSION = "1"
_VERSION = re.compile(r"#\s*subnet-tracker export v(\S+)")


class _Parser:
    def __init__(self) -> None:
        # None while skipping the lines of an invalid section header
        self.section: Section | GroupHeader | None = Section.UNGROUPED
        self.properties: set[str] | None = None
        self.settings: dict[str, SettingLine] = {}
        self.groups: dict[str, GroupSection] = {}
        self.domains: list[DomainLine] = []
        self.first_lines: dict[DomainName, int] = {}
        self.routes: dict[Network, RouteLine] = {}
        self.duplicates: list[DuplicateLine] = []
        self.errors: list[ParseError] = []

    def result(self) -> ParseResult[ImportFile]:
        model = ImportFile(tuple(self.settings.values()), tuple(self.groups.values()), tuple(self.domains), tuple(self.routes.values()), tuple(self.duplicates))
        return ParseResult(model, tuple(self.errors))

    def feed(self, line: int, text: str) -> None:
        if text.startswith("#"):
            self._comment(line, text)
        elif text.startswith("["):
            self._header(line, text)
        elif text:
            match self.section:
                case Section.SETTINGS:
                    self._setting(line, text)
                case Section.ROUTES:
                    self._route(line, text)
                case Section.UNGROUPED:
                    self._domain(line, text, None)
                case GroupHeader(name):
                    self._domain(line, text, name)

    def _error(self, line: int, message: str) -> None:
        self.errors.append(ParseError(line, message))

    def _comment(self, line: int, text: str) -> None:
        if (m := _VERSION.fullmatch(text)) and m[1] != FORMAT_VERSION:
            self._error(line, f"unsupported export format version v{m[1]}, expected v{FORMAT_VERSION}")

    def _header(self, line: int, text: str) -> None:
        self.properties = None
        match header(text):
            case Rejected(reason):
                self._error(line, reason)
                self.section = None
            case GroupHeader(name) as group if name in self.groups:
                self._error(line, f"group {name!r} is already defined on line {self.groups[name].line}")
                self.section = group
            case GroupHeader(name) as group:
                self.groups[name] = GroupSection(line, name)
                self.section = group
                self.properties = set()
            case section:
                self.section = section

    def _setting(self, line: int, text: str) -> None:
        if (kv := key_value(text)) is None:
            self._error(line, "expected key = value")
        elif first := self.settings.get(kv.key):
            self._error(line, f"setting {kv.key!r} is already given on line {first.line}")
        else:
            self.settings[kv.key] = SettingLine(line, kv.key, kv.value)

    def _property(self, line: int, kv: KeyValue, group: str | None) -> None:
        if group is None or self.properties is None:
            self._error(line, "group properties must come right after the [group] header")
        elif kv.key in self.properties:
            self._error(line, f"group property {kv.key!r} is already given")
        else:
            self.properties.add(kv.key)
            match group_property(self.groups[group], kv):
                case Rejected(reason):
                    self._error(line, f"{kv.key}: {reason}")
                case updated:
                    self.groups[group] = updated

    def _domain(self, line: int, text: str, group: str | None) -> None:
        if (kv := key_value(text)) is not None:
            self._property(line, kv, group)
            return
        self.properties = None
        parsed = domain_line(line, text, group)
        self.errors.extend(parsed.errors)
        if (domain := parsed.value) is None:
            return
        names = domain_names(domain.name).all
        if first := next((self.first_lines[n] for n in names if n in self.first_lines), None):
            self.duplicates.append(DuplicateLine(line, domain.name, first))
        else:
            self.domains.append(domain)
            self.first_lines.update(dict.fromkeys(names, line))

    def _route(self, line: int, text: str) -> None:
        parsed = route_line(line, text)
        self.errors.extend(parsed.errors)
        if (route := parsed.value) is None:
            return
        if first := self.routes.get(route.network):
            self._error(line, f"{str(route.network)!r} is already listed on line {first.line}")
        else:
            self.routes[route.network] = route


def parse_import(text: str) -> ParseResult[ImportFile]:
    parser = _Parser()
    for line, content in enumerate(text.splitlines(), 1):
        parser.feed(line, content.strip())
    return parser.result()
