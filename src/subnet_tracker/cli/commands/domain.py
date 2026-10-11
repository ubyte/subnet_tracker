import argparse
import json
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path

from subnet_tracker.cli.add_summary import add_outcome, counted
from subnet_tracker.cli.command import Commands, Context
from subnet_tracker.cli.commands.add import STDIN
from subnet_tracker.cli.domain_text import domain_json, domain_text, domains_table, pair_text
from subnet_tracker.cli.output import Done, ExitCode, Failed, Outcome, rejected
from subnet_tracker.core.domain_query import DomainQuery, DomainSort, DomainState, query_domains
from subnet_tracker.core.domain_view import domain_views
from subnet_tracker.core.domains import Domain, DomainEdit, find_domain
from subnet_tracker.core.edits import KEEP
from subnet_tracker.core.services.domains import AddOptions, DomainChange, DomainChanged, add_domain_names, change_domains, remove_domains
from subnet_tracker.core.settings_store import load_settings
from subnet_tracker.discovery.parse import Rejected, normalize_name, parse_names
from subnet_tracker.types import BatchKind, DomainId, FieldError, ReconnectMode

NO_GROUP = "none"
INHERIT = "inherit"


def prefix(text: str) -> int | None:
    return None if text == INHERIT else int(text)


# either name of a pair stands for its domain; every name must be tracked
def named_domains(conn: sqlite3.Connection, texts: Sequence[str]) -> tuple[Domain, ...] | Failed:
    found: dict[DomainId, Domain] = {}
    errors: list[str] = []
    for text in texts:
        match normalize_name(text):
            case Rejected(reason):
                errors.append(f"{text!r}: {reason}")
            case name:
                if (domain := find_domain(conn, name)) is None:
                    errors.append(f"{name} is not tracked")
                else:
                    found.setdefault(domain.id, domain)
    return Failed(ExitCode.USAGE, tuple(errors)) if errors else tuple(found.values())


def changes_text(changed: Sequence[DomainChanged]) -> str:
    def change(name: str, old: object, new: object) -> str:
        return f"{name} {json.dumps(old, ensure_ascii=False)} -> {json.dumps(new, ensure_ascii=False)}"

    return "\n".join(f"{c.domain.canonical.name}: " + ", ".join(change(name, old, new) for name, (old, new) in c.changes.items()) for c in changed) or "nothing changed"


def _list(ctx: Context, args: argparse.Namespace) -> Outcome:
    no_group = args.group == NO_GROUP
    views = query_domains(ctx.conn, DomainQuery(None if no_group else args.group, no_group, args.tag, args.batch, args.search, args.state, args.sort))
    if isinstance(views, FieldError):
        return rejected(views)
    return Done(domains_table(views), [domain_json(v) for v in views])


def _show(ctx: Context, args: argparse.Namespace) -> Outcome:
    found = named_domains(ctx.conn, [args.name])
    if isinstance(found, Failed):
        return found
    retention = load_settings(ctx.conn).routes.retention
    views = domain_views(ctx.conn, [d.id for d in found])
    if not views:
        return Failed(ExitCode.USAGE, (f"{args.name} is not tracked",))
    return Done(domain_text(views[0], retention), domain_json(views[0], retention))


def _read_names(path: str, stdin: Callable[[], str]) -> str:
    return stdin() if path == STDIN else Path(path).read_text(encoding="utf-8")


def _add(ctx: Context, args: argparse.Namespace) -> Outcome:
    listing = None if args.from_file is None else _read_names(args.from_file, ctx.stdin)
    given, listed = parse_names("\n".join(args.names)), parse_names(listing or "")
    source = "stdin" if args.from_file == STDIN else args.from_file
    if errors := [e.message for e in given.errors] + [f"{source}:{e.line}: {e.message}" for e in listed.errors]:
        return Failed(ExitCode.USAGE, tuple(errors))
    if not (names := given.value + listed.value):
        return Failed(ExitCode.USAGE, ("no names to add",))
    text = "\n".join([*args.names, *([listing] if listing is not None else [])])
    change = DomainChange(DomainEdit(prefix_v4=args.prefix4, prefix_v6=args.prefix6, comment=args.comment), add_groups=tuple(args.group), add_tags=tuple(args.tag))
    return add_outcome(add_domain_names(ctx.conn, BatchKind.CLI, ctx.actor, text, names, AddOptions(ReconnectMode.AUTO, not args.no_resolve, change), ctx.now))


def _edit(ctx: Context, args: argparse.Namespace) -> Outcome:
    edit = DomainEdit(prefix_v4=args.prefix4, prefix_v6=args.prefix6, enabled=args.enabled, comment=args.comment)
    change = DomainChange(edit, tuple(args.add_group), tuple(args.rm_group), tuple(args.add_tag), tuple(args.rm_tag))
    if change == DomainChange():
        return Failed(ExitCode.USAGE, ("nothing to change, give at least one option",))
    found = named_domains(ctx.conn, args.names)
    if isinstance(found, Failed):
        return found
    changed = change_domains(ctx.conn, [d.id for d in found], change, ctx.actor, ctx.now)
    if isinstance(changed, FieldError):
        return rejected(changed)
    return Done(changes_text(changed), [{"id": c.domain.id, "name": c.domain.canonical.name, "changes": c.details} for c in changed], notify=bool(changed))


def _rm(ctx: Context, args: argparse.Namespace) -> Outcome:
    found = named_domains(ctx.conn, args.names)
    if isinstance(found, Failed):
        return found
    if not args.yes:
        match ctx.confirm(f"delete {', '.join(map(pair_text, found))}?"):
            case None:
                return Failed(ExitCode.USAGE, ("stdin is not a terminal to confirm on, pass --yes to delete without asking",))
            case False:
                return Failed(ExitCode.ERROR, ("not confirmed, nothing deleted",))
    removed = remove_domains(ctx.conn, [d.id for d in found], ctx.actor, ctx.now)
    return Done(counted(f"deleted {len(removed)}", [pair_text(d) for d in removed]), [{"id": d.id, "name": d.canonical.name, "pair": d.pair.name} for d in removed], notify=bool(removed))


def _settings_arguments(parser: argparse.ArgumentParser, prefix_type: Callable[[str], int | None], prefix_metavar: str) -> None:
    parser.add_argument("--prefix4", type=prefix_type, default=KEEP, metavar=prefix_metavar, help="IPv4 prefix of the domain's networks")
    parser.add_argument("--prefix6", type=prefix_type, default=KEEP, metavar=prefix_metavar, help="IPv6 prefix of the domain's networks")
    parser.add_argument("--comment", default=KEEP, metavar="C")


def register(commands: Commands) -> None:
    domain = commands.group("domain", "tracked domains; either name of a domain stands for the whole domain")
    listing = domain.command("list", "list domains, newest first, a row per name", _list)
    listing.add_argument("--group", metavar=f"G|{NO_GROUP}", help=f"domains of a group, '{NO_GROUP}' for domains without one")
    listing.add_argument("--tag", metavar="T")
    listing.add_argument("--batch", type=int, metavar="ID")
    listing.add_argument("--search", metavar="S", help="a part of a name, group or tag, in any case")
    listing.add_argument("--state", type=DomainState, choices=list(DomainState))
    listing.add_argument("--sort", type=DomainSort, choices=list(DomainSort), help="oldest resolve first for the times")
    domain.command("show", "show a domain with resolver results and addresses of both names", _show).add_argument("name", metavar="NAME")
    add = domain.command("add", "add domains by name with their settings; names already tracked are skipped and keep theirs", _add)
    add.add_argument("names", nargs="*", metavar="NAME")
    add.add_argument("--from-file", metavar=f"FILE|{STDIN}", help="names separated by spaces, commas and newlines")
    add.add_argument("--group", action="append", default=[], metavar="G", help="an existing group")
    add.add_argument("--tag", action="append", default=[], metavar="T")
    _settings_arguments(add, int, "N")
    add.add_argument("--no-resolve", action="store_true", help="leave the new domains to the scheduled resolve")
    edit = domain.command("edit", "change domains", _edit)
    edit.add_argument("names", nargs="+", metavar="NAME")
    for option, metavar in (("--add-group", "G"), ("--rm-group", "G"), ("--add-tag", "T"), ("--rm-tag", "T")):
        edit.add_argument(option, action="append", default=[], metavar=metavar)
    _settings_arguments(edit, prefix, f"N|{INHERIT}")
    switch = edit.add_mutually_exclusive_group()
    switch.add_argument("--enable", dest="enabled", action="store_const", const=True)
    switch.add_argument("--disable", dest="enabled", action="store_const", const=False)
    edit.set_defaults(enabled=KEEP)
    rm = domain.command("rm", "delete domains with both of their names", _rm)
    rm.add_argument("names", nargs="+", metavar="NAME")
    rm.add_argument("--yes", action="store_true", help="delete without asking")
