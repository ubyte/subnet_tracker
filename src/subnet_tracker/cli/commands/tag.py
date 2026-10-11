import argparse

from subnet_tracker.cli.command import Commands, Context
from subnet_tracker.cli.output import Done, Outcome, rejected, table
from subnet_tracker.core.services.tags import lookup_tag, remove_tag, rename_named_tag
from subnet_tracker.core.tags import TagRow, list_tags
from subnet_tracker.types import FieldError, JsonValue


def tag_json(row: TagRow) -> JsonValue:
    return {"id": row.tag.id, "name": row.tag.name, "domains": row.domains}


def _list(ctx: Context, args: argparse.Namespace) -> Outcome:
    rows = list_tags(ctx.conn)
    return Done(table(("name", "domains"), [(row.tag.name, str(row.domains)) for row in rows]), [tag_json(row) for row in rows])


def _rename(ctx: Context, args: argparse.Namespace) -> Outcome:
    renamed = rename_named_tag(ctx.conn, args.old, args.new, ctx.actor, ctx.now)
    if isinstance(renamed, FieldError):
        return rejected(renamed)
    if not renamed.changed:
        text = "nothing changed"
    elif renamed.merged:
        text = f"merged tag {renamed.old} into {renamed.tag.name}"
    else:
        text = f"renamed tag {renamed.old} to {renamed.tag.name}"
    data: JsonValue = {"id": renamed.tag.id, "name": renamed.tag.name, "old": renamed.old, "merged": renamed.merged, "changed": renamed.changed}
    return Done(text, data, notify=renamed.applied)


def _rm(ctx: Context, args: argparse.Namespace) -> Outcome:
    found = lookup_tag(ctx.conn, args.name)
    if isinstance(found, FieldError):
        return rejected(found)
    removed = remove_tag(ctx.conn, found.id, ctx.actor, ctx.now)
    if isinstance(removed, FieldError):
        return rejected(removed)
    return Done(f"deleted tag {removed.value.name}", {"id": removed.value.id, "name": removed.value.name}, notify=removed.applied)


def register(commands: Commands) -> None:
    tag = commands.group("tag", "tags")
    tag.command("list", "list tags and how many domains have them", _list)
    rename = tag.command("rename", "rename a tag; a name that is taken merges the two", _rename)
    rename.add_argument("old", metavar="OLD")
    rename.add_argument("new", metavar="NEW")
    tag.command("rm", "delete a tag", _rm).add_argument("name", metavar="NAME")
