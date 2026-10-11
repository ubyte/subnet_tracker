import argparse
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Literal

from subnet_tracker.cli.add_summary import summary_json, summary_text
from subnet_tracker.cli.command import Commands, Context
from subnet_tracker.cli.output import Done, ExitCode, Failed, Outcome, rejected, table
from subnet_tracker.core.batches import AddResult
from subnet_tracker.core.groups import GroupEdit, NewGroup, checked_group_edit, find_group, list_groups
from subnet_tracker.core.names import tracked_names
from subnet_tracker.core.services.domains import AddOptions, DomainChange, add_domain_names
from subnet_tracker.core.services.groups import add_group, lookup_group
from subnet_tracker.core.settings_store import load_settings
from subnet_tracker.discovery.extract import Candidate, ScanRole
from subnet_tracker.discovery.fetch import MAX_URLS, Dns, Exchange, HttpxExchange, system_dns
from subnet_tracker.discovery.scan import ScanResult, scan
from subnet_tracker.types import BatchKind, DomainName, FieldError, IPAddress, JsonValue, ReconnectMode


# Tests replace this with the DNS and exchange doubles used by discovery.scan.
def page_fetch() -> tuple[Dns, Exchange]:
    return _SystemDns(), HttpxExchange()


class _SystemDns:
    def resolve(self, host: str) -> tuple[IPAddress, ...]:
        return system_dns(host)


type GroupAction = Literal["default", "new", "existing", "none"]


@dataclass(frozen=True, slots=True)
class GroupPlan:
    action: GroupAction
    name: str | None = None


def chosen(candidates: Sequence[Candidate], include_third_party: bool) -> tuple[Candidate, ...]:
    return tuple(item for item in candidates if item.selected or (include_third_party and item.role == ScanRole.THIRD and not item.exists))


def _rows(candidates: Sequence[Candidate], picked: Collection[DomainName]) -> list[tuple[str, str, str, str, str]]:
    return [("yes" if item.name in picked else "no", item.name, str(item.role), "exists" if item.exists else "new", ", ".join(item.sources)) for item in candidates]


def listing(result: ScanResult, picked: Collection[DomainName]) -> str:
    lines = [table(("add", "name", "role", "state", "sources"), _rows(result.candidates, picked)) if result.candidates else "no domains"]
    if result.ignored:
        lines.append("ignored: " + ", ".join(result.ignored))
    lines.extend(f"error: {error.url}: {error.message}" if error.url else f"error: {error.message}" for error in result.errors)
    return "\n".join(lines)


def planned_line(plan: GroupPlan) -> str:
    if plan.action == "existing" and plan.name:
        return f"group {plan.name}"
    if plan.action == "new" and plan.name:
        return f"new group {plan.name}"
    return "no group"


def result_line(plan: GroupPlan, created: bool) -> str:
    if plan.action == "new" and plan.name and not created:
        return f"new group {plan.name} not created"
    return planned_line(plan)


def _exit(urls: int, errors: int, added: int) -> ExitCode:
    if not errors:
        return ExitCode.OK
    if errors < urls or added:
        return ExitCode.PARTIAL
    return ExitCode.ERROR


def _candidate(item: Candidate, add: bool) -> dict[str, JsonValue]:
    return {"name": item.name, "role": str(item.role), "exists": item.exists, "add": add, "sources": list(item.sources)}


def _data(result: ScanResult, picked: Collection[DomainName], plan: GroupPlan, created: bool, added: AddResult) -> dict[str, JsonValue]:
    summary = summary_json(added)
    data: dict[str, JsonValue] = {
        "candidates": [_candidate(item, item.name in picked) for item in result.candidates],
        "ignored": list(result.ignored),
        "errors": [{"url": error.url, "message": error.message} for error in result.errors],
        "group": {"name": None if plan.action == "none" else plan.name, "created": created},
    }
    if isinstance(summary, dict):
        data.update(summary)
    return data


def _new_group_name(ctx: Context, text: str) -> str | Failed:
    checked = checked_group_edit(GroupEdit(text))
    if isinstance(checked, FieldError):
        return rejected(checked)
    if not isinstance(checked.name, str):
        return Failed(ExitCode.USAGE, ("name: must be 1 to 100 characters long",))
    if find_group(ctx.conn, checked.name) is not None:
        return rejected(FieldError("name", f"group {checked.name!r} already exists"))
    return checked.name


def _group_plan(ctx: Context, args: argparse.Namespace) -> GroupPlan | Failed:
    if args.group is not None:
        found = lookup_group(ctx.conn, args.group)
        return rejected(found) if isinstance(found, FieldError) else GroupPlan("existing", found.name)
    if args.no_group:
        return GroupPlan("none")
    if args.new_group is not None:
        name = _new_group_name(ctx, args.new_group)
        return name if isinstance(name, Failed) else GroupPlan("new", name)
    return GroupPlan("default")


def _resolved(plan: GroupPlan, group_name: str) -> GroupPlan:
    return GroupPlan("new", group_name or None) if plan.action == "default" else plan


def _confirm(ctx: Context, text: str, count: int, destination: str) -> Failed | None:
    noun = "domain" if count == 1 else "domains"
    match ctx.confirm(f"{text}\nadd {count} {noun} into {destination}?"):
        case None:
            return Failed(ExitCode.USAGE, ("stdin is not a terminal to confirm on, pass --yes to add without asking",))
        case False:
            return Failed(ExitCode.ERROR, ("not confirmed, nothing added",))
    return None


def _save(ctx: Context, urls: Sequence[str], names: Sequence[DomainName], plan: GroupPlan) -> tuple[AddResult, bool] | Failed:
    created = False
    group = plan.name if plan.action == "existing" else None
    if plan.action == "new" and plan.name and names:
        made = add_group(ctx.conn, NewGroup(plan.name), ctx.actor, ctx.now)
        if isinstance(made, FieldError):
            return rejected(made)
        created = True
        group = made.value.name
    change = DomainChange(add_groups=(group,)) if group else DomainChange()
    added = add_domain_names(ctx.conn, BatchKind.SCAN, ctx.actor, "\n".join(urls), names, AddOptions(ReconnectMode.AUTO, change=change), ctx.now)
    return rejected(added) if isinstance(added, FieldError) else (added, created)


def _scan(ctx: Context, args: argparse.Namespace) -> Outcome:
    plan = _group_plan(ctx, args)
    if isinstance(plan, Failed):
        return plan
    if len(args.urls) > MAX_URLS:
        return Failed(ExitCode.USAGE, (f"at most {MAX_URLS} URLs",))
    dns, exchange = page_fetch()
    discovery = load_settings(ctx.conn).discovery
    taken = [row.group.name for row in list_groups(ctx.conn) if row.group is not None]
    result = scan(tuple(args.urls), existing=tracked_names(ctx.conn), groups=taken, settings=discovery, dns=dns, exchange=exchange)
    plan = _resolved(plan, result.group_name)
    picked = chosen(result.candidates, args.include_third_party)
    names = tuple(item.name for item in picked)
    if plan.action == "new" and not plan.name and names:
        return Failed(ExitCode.USAGE, ("no group name for these URLs, pass --new-group NAME or --no-group",))
    body = listing(result, names)
    if not args.yes and (refused := _confirm(ctx, body, len(names), planned_line(plan))) is not None:
        return refused
    saved = _save(ctx, args.urls, names, plan)
    if isinstance(saved, Failed):
        return saved
    added, created = saved
    text = "\n".join((body, result_line(plan, created), summary_text(added)))
    return Done(text, _data(result, names, plan, created, added), _exit(len(args.urls), len(result.errors), len(added.added)), notify=bool(added.added))


def register(commands: Commands) -> None:
    command = commands.command("scan", "scan pages and add the selected domains after confirmation, creating a new group by default", _scan)
    command.add_argument("urls", nargs="+", metavar="URL")
    groups = command.add_mutually_exclusive_group()
    groups.add_argument("--new-group", metavar="NAME", help="create this group instead of the default name")
    groups.add_argument("--group", metavar="G", help="add into an existing group")
    groups.add_argument("--no-group", action="store_true", help="add without a group")
    command.add_argument("--include-third-party", action="store_true", help="also add third-party domains that are not already tracked")
    command.add_argument("--yes", action="store_true", help="add without asking")
