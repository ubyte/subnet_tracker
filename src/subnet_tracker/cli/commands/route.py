import argparse

from subnet_tracker.cli.command import Commands, Context
from subnet_tracker.cli.output import Done, ExitCode, Failed, Outcome, changed_line, listed, rejected, table
from subnet_tracker.core.edits import KEEP, Keep
from subnet_tracker.core.global_routes import GlobalRoute, GlobalRouteEdit, list_global_routes
from subnet_tracker.core.groups import get_group
from subnet_tracker.core.services.routes import RouteChange, add_route, change_route, lookup_route, remove_route
from subnet_tracker.types import FieldError, JsonValue


def yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _names(ctx: Context, route: GlobalRoute) -> list[str]:
    return sorted(group.name for i in route.groups if (group := get_group(ctx.conn, i)) is not None)


def route_json(ctx: Context, route: GlobalRoute) -> JsonValue:
    return {
        "id": route.id,
        "cidr": str(route.network),
        "family": route.network.version,
        "comment": route.comment,
        "enabled": route.enabled,
        "groups": _names(ctx, route),
        "created_at": route.created_at,
        "updated_at": route.updated_at,
    }


def _list(ctx: Context, args: argparse.Namespace) -> Outcome:
    routes = list_global_routes(ctx.conn)
    rows = [(str(route.network), listed(_names(ctx, route)), route.comment, yes_no(route.enabled)) for route in routes]
    return Done(table(("cidr", "groups", "comment", "enabled"), rows), [route_json(ctx, route) for route in routes])


def _add(ctx: Context, args: argparse.Namespace) -> Outcome:
    created = add_route(ctx.conn, args.cidr, args.comment, args.group, ctx.config.routes.server_addresses, ctx.actor, ctx.now)
    if isinstance(created, FieldError):
        return rejected(created)
    return Done(f"added route {created.network}", route_json(ctx, created), notify=True)


def _edit(ctx: Context, args: argparse.Namespace) -> Outcome:
    if all(isinstance(value, Keep) for value in (args.new_cidr, args.comment, args.enabled)) and not args.add_group and not args.rm_group:
        return Failed(ExitCode.USAGE, ("nothing to change, give at least one option",))
    found = lookup_route(ctx.conn, args.cidr)
    if isinstance(found, FieldError):
        return rejected(found)
    updated = change_route(
        ctx.conn, found.id, RouteChange(GlobalRouteEdit(args.new_cidr, args.comment, args.enabled), tuple(args.add_group), tuple(args.rm_group)), ctx.config.routes.server_addresses, ctx.actor, ctx.now
    )
    if isinstance(updated, FieldError):
        return rejected(updated)
    changes = {name: {"old": old, "new": new} for name, (old, new) in updated.changes.items()}
    return Done(changed_line(str(updated.route.network), updated.changes), {"id": updated.route.id, "cidr": str(updated.route.network), "changes": changes}, notify=bool(updated.changes))


def _rm(ctx: Context, args: argparse.Namespace) -> Outcome:
    found = lookup_route(ctx.conn, args.cidr)
    if isinstance(found, FieldError):
        return rejected(found)
    removed = remove_route(ctx.conn, found.id, ctx.actor, ctx.now)
    if isinstance(removed, FieldError):
        return rejected(removed)
    return Done(f"deleted route {removed.network}", {"id": removed.id, "cidr": str(removed.network)}, notify=True)


def register(commands: Commands) -> None:
    route = commands.group("route", "global routes")
    route.command("list", "list global routes", _list)
    add = route.command("add", "add a global route", _add)
    add.add_argument("cidr", metavar="CIDR")
    add.add_argument("--group", action="append", default=[], metavar="G")
    add.add_argument("--comment", default="", metavar="C")
    edit = route.command("edit", "change a global route", _edit)
    edit.add_argument("cidr", metavar="CIDR")
    edit.add_argument("--cidr", dest="new_cidr", default=KEEP, metavar="NEW")
    edit.add_argument("--comment", default=KEEP, metavar="C")
    edit.add_argument("--add-group", action="append", default=[], metavar="G")
    edit.add_argument("--rm-group", action="append", default=[], metavar="G")
    switch = edit.add_mutually_exclusive_group()
    switch.add_argument("--enable", dest="enabled", action="store_const", const=True)
    switch.add_argument("--disable", dest="enabled", action="store_const", const=False)
    edit.set_defaults(enabled=KEEP)
    route.command("rm", "delete a global route", _rm).add_argument("cidr", metavar="CIDR")
