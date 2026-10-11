import argparse

from subnet_tracker.cli.command import Commands, Context
from subnet_tracker.cli.output import Done, ExitCode, Failed, Outcome, changed_line, rejected, table
from subnet_tracker.core.edits import KEEP, Keep
from subnet_tracker.core.resolvers import NewResolver, Resolver, ResolverEdit, list_resolvers
from subnet_tracker.core.services.resolvers import add_resolver, change_resolver, lookup_resolver, remove_resolver
from subnet_tracker.types import FieldError, JsonValue, ResolverKind

_KINDS = [kind for kind in ResolverKind if kind is not ResolverKind.AGENT]


def yes_no(value: bool) -> str:
    return "yes" if value else "no"


def resolver_json(resolver: Resolver) -> JsonValue:
    return {
        "id": resolver.id,
        "name": resolver.name,
        "kind": resolver.kind,
        "endpoint": resolver.endpoint,
        "use_for_routes": resolver.use_for_routes,
        "max_qps": resolver.max_qps,
        "position": resolver.position,
        "created_at": resolver.created_at,
        "updated_at": resolver.updated_at,
    }


def _row(resolver: Resolver) -> tuple[str, ...]:
    return (yes_no(resolver.use_for_routes), resolver.name, resolver.kind, resolver.endpoint or "-", str(resolver.max_qps))


def _list(ctx: Context, args: argparse.Namespace) -> Outcome:
    resolvers = list_resolvers(ctx.conn)
    return Done(table(("routes", "name", "kind", "endpoint", "qps"), map(_row, resolvers)), [resolver_json(resolver) for resolver in resolvers])


def _add(ctx: Context, args: argparse.Namespace) -> Outcome:
    created = add_resolver(ctx.conn, NewResolver(args.name, args.kind, args.endpoint, args.use_for_routes, args.max_qps), ctx.actor, ctx.now)
    if isinstance(created, FieldError):
        return rejected(created)
    return Done(f"added resolver {created.value.name}", resolver_json(created.value), notify=created.applied)


def _edit(ctx: Context, args: argparse.Namespace) -> Outcome:
    if all(isinstance(value, Keep) for value in (args.new_name, args.endpoint, args.use_for_routes, args.max_qps)):
        return Failed(ExitCode.USAGE, ("nothing to change, give at least one option",))
    found = lookup_resolver(ctx.conn, args.name)
    if isinstance(found, FieldError):
        return rejected(found)
    updated = change_resolver(ctx.conn, found.id, ResolverEdit(args.new_name, args.endpoint, args.use_for_routes, args.max_qps), ctx.actor, ctx.now)
    if isinstance(updated, FieldError):
        return rejected(updated)
    changes = {name: {"old": old, "new": new} for name, (old, new) in updated.changes.items()}
    return Done(changed_line(updated.resolver.name, updated.changes), {"id": updated.resolver.id, "name": updated.resolver.name, "changes": changes}, notify=updated.applied)


def _removed(name: str, addresses: int) -> str:
    if addresses == 0:
        return f"deleted resolver {name}"
    noun = "address" if addresses == 1 else "addresses"
    return f"deleted resolver {name}, {addresses} {noun} removed"


def _rm(ctx: Context, args: argparse.Namespace) -> Outcome:
    found = lookup_resolver(ctx.conn, args.name)
    if isinstance(found, FieldError):
        return rejected(found)
    removed = remove_resolver(ctx.conn, found.id, ctx.actor, ctx.now)
    if isinstance(removed, FieldError):
        return rejected(removed)
    return Done(_removed(removed.resolver.name, removed.addresses), {"id": removed.resolver.id, "name": removed.resolver.name, "addresses": removed.addresses}, notify=removed.applied)


def _routes(parser: argparse.ArgumentParser) -> None:
    switch = parser.add_mutually_exclusive_group()
    switch.add_argument("--routes", dest="use_for_routes", action="store_const", const=True, help="use addresses from this resolver in routes")
    switch.add_argument("--no-routes", dest="use_for_routes", action="store_const", const=False, help="keep the addresses out of routes")


def register(commands: Commands) -> None:
    resolver = commands.group("resolver", "resolvers")
    resolver.command("list", "list resolvers in checklist order", _list)
    add = resolver.command("add", "add a resolver; an agent is created with its agent, not here", _add)
    add.add_argument("name", metavar="NAME")
    add.add_argument("--kind", required=True, choices=_KINDS)
    add.add_argument("--endpoint", default=None, metavar="E")
    add.add_argument("--max-qps", type=int, default=50, metavar="N")
    _routes(add)
    add.set_defaults(use_for_routes=True)
    edit = resolver.command("edit", "change a resolver; the kind stays as it was created", _edit)
    edit.add_argument("name", metavar="NAME")
    edit.add_argument("--name", dest="new_name", default=KEEP, metavar="NEW")
    edit.add_argument("--endpoint", default=KEEP, metavar="E")
    edit.add_argument("--max-qps", type=int, default=KEEP, metavar="N")
    _routes(edit)
    edit.set_defaults(use_for_routes=KEEP)
    resolver.command("rm", "delete a resolver and the addresses only it had seen", _rm).add_argument("name", metavar="NAME")
