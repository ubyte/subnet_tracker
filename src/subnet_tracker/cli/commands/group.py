import argparse
from collections.abc import Callable

from subnet_tracker.cli.command import Commands, Context
from subnet_tracker.cli.output import Done, ExitCode, Failed, Outcome, changed_line, rejected, table
from subnet_tracker.core.edits import KEEP, Keep
from subnet_tracker.core.groups import NO_GROUP, Group, GroupEdit, GroupRow, NewGroup, list_groups
from subnet_tracker.core.services.groups import GroupRemoval, add_group, change_group, lookup_group, remove_group
from subnet_tracker.types import FieldError, JsonValue

INHERIT = "inherit"


def prefix(text: str) -> int | None:
    return None if text == INHERIT else int(text)


def yes_no(value: bool) -> str:
    return "yes" if value else "no"


def prefix_cell(value: int | None) -> str:
    return "-" if value is None else f"/{value}"


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def group_json(row: GroupRow) -> JsonValue:
    group = row.group
    if group is None:
        return {"id": None, "name": None, "description": None, "domains": row.domains, "routes": row.routes, "prefix_v4": None, "prefix_v6": None, "enabled": None}
    return {
        "id": group.id,
        "name": group.name,
        "description": group.description,
        "domains": row.domains,
        "routes": row.routes,
        "prefix_v4": group.prefix_v4,
        "prefix_v6": group.prefix_v6,
        "enabled": group.enabled,
        "created_at": group.created_at,
        "updated_at": group.updated_at,
    }


def _row(row: GroupRow) -> tuple[str, ...]:
    group = row.group
    if group is None:
        return (NO_GROUP, "-", str(row.domains), str(row.routes), "-", "-", "-")
    return (group.name, group.description, str(row.domains), str(row.routes), prefix_cell(group.prefix_v4), prefix_cell(group.prefix_v6), yes_no(group.enabled))


def stored_json(group: Group) -> JsonValue:
    return {
        "id": group.id,
        "name": group.name,
        "description": group.description,
        "prefix_v4": group.prefix_v4,
        "prefix_v6": group.prefix_v6,
        "enabled": group.enabled,
        "created_at": group.created_at,
        "updated_at": group.updated_at,
    }


def removal_text(result: GroupRemoval) -> str:
    lines = [f"deleted group {result.group.name}"]
    if result.domains:
        lines.append(f"deleted {_count(len(result.domains), 'domain', 'domains')} that belonged only to it: {', '.join(d.canonical.name for d in result.domains)}")
    if result.routes:
        lines.append(f"deleted {_count(len(result.routes), 'route', 'routes')} that belonged only to it: {', '.join(str(r.network) for r in result.routes)}")
    return "\n".join(lines)


def removal_json(result: GroupRemoval) -> JsonValue:
    return {"id": result.group.id, "name": result.group.name, "domains": [d.canonical.name for d in result.domains], "routes": [str(r.network) for r in result.routes]}


def _list(ctx: Context, args: argparse.Namespace) -> Outcome:
    rows = list_groups(ctx.conn)
    return Done(table(("name", "description", "domains", "routes", "prefix4", "prefix6", "enabled"), map(_row, rows)), [group_json(row) for row in rows])


def _add(ctx: Context, args: argparse.Namespace) -> Outcome:
    created = add_group(ctx.conn, NewGroup(args.name, args.description, args.prefix4, args.prefix6), ctx.actor, ctx.now)
    if isinstance(created, FieldError):
        return rejected(created)
    return Done(f"added group {created.name}", stored_json(created), notify=True)


def _edit(ctx: Context, args: argparse.Namespace) -> Outcome:
    if all(isinstance(value, Keep) for value in (args.name, args.description, args.prefix4, args.prefix6, args.enabled)):
        return Failed(ExitCode.USAGE, ("nothing to change, give at least one option",))
    found = lookup_group(ctx.conn, args.group_name)
    if isinstance(found, FieldError):
        return rejected(found)
    updated = change_group(ctx.conn, found.id, GroupEdit(args.name, args.description, args.prefix4, args.prefix6, args.enabled), ctx.actor, ctx.now)
    if isinstance(updated, FieldError):
        return rejected(updated)
    changes = {name: {"old": old, "new": new} for name, (old, new) in updated.changes.items()}
    return Done(changed_line(updated.group.name, updated.changes), {"id": updated.group.id, "name": updated.group.name, "changes": changes}, notify=bool(updated.changes))


def _confirm(ctx: Context, group: Group, with_members: bool) -> Outcome | None:
    prompt = f"delete group {group.name} and the domains and routes that belong only to it?" if with_members else f"delete group {group.name}?"
    match ctx.confirm(prompt):
        case None:
            return Failed(ExitCode.USAGE, ("stdin is not a terminal to confirm on, pass --yes to delete without asking",))
        case False:
            return Failed(ExitCode.ERROR, ("not confirmed, nothing deleted",))
        case True:
            return None


def _rm(ctx: Context, args: argparse.Namespace) -> Outcome:
    found = lookup_group(ctx.conn, args.name)
    if isinstance(found, FieldError):
        return rejected(found)
    if not args.yes and (refused := _confirm(ctx, found, args.with_members)) is not None:
        return refused
    removed = remove_group(ctx.conn, found.id, args.with_members, ctx.actor, ctx.now)
    if isinstance(removed, FieldError):
        return rejected(removed)
    return Done(removal_text(removed), removal_json(removed), notify=True)


def _prefixes(parser: argparse.ArgumentParser, prefix_type: Callable[[str], int | None], metavar: str) -> None:
    parser.add_argument("--prefix4", type=prefix_type, default=KEEP, metavar=metavar)
    parser.add_argument("--prefix6", type=prefix_type, default=KEEP, metavar=metavar)


def register(commands: Commands) -> None:
    group = commands.group("group", "groups of domains and global routes")
    group.command("list", "list groups, the row without a group first", _list)
    add = group.command("add", "create a group", _add)
    add.add_argument("name", metavar="NAME")
    add.add_argument("--prefix4", type=int, default=None, metavar="N")
    add.add_argument("--prefix6", type=int, default=None, metavar="N")
    add.add_argument("--description", default="", metavar="D")
    edit = group.command("edit", "change a group", _edit)
    edit.add_argument("group_name", metavar="NAME")
    edit.add_argument("--name", default=KEEP, metavar="NEW")
    edit.add_argument("--description", default=KEEP, metavar="D")
    _prefixes(edit, prefix, f"N|{INHERIT}")
    switch = edit.add_mutually_exclusive_group()
    switch.add_argument("--enable", dest="enabled", action="store_const", const=True)
    switch.add_argument("--disable", dest="enabled", action="store_const", const=False)
    edit.set_defaults(enabled=KEEP)
    rm = group.command("rm", "delete a group", _rm)
    rm.add_argument("name", metavar="NAME")
    rm.add_argument("--with-members", action="store_true", help="also delete domains and routes that belong only to this group")
    rm.add_argument("--yes", action="store_true", help="delete without asking")
