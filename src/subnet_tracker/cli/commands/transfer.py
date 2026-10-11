import argparse
from pathlib import Path

from subnet_tracker.cli.command import Commands, Context
from subnet_tracker.cli.import_text import duplicate_warnings, format_summary, has_changes, import_data
from subnet_tracker.cli.output import Done, ExitCode, Failed, Outcome
from subnet_tracker.core.export_state import read_export
from subnet_tracker.core.groups import NO_GROUP
from subnet_tracker.core.services.importing import apply_import, preview_import
from subnet_tracker.textio.changes import ImportMode
from subnet_tracker.textio.export import LineBreaks, render_export
from subnet_tracker.types import FieldError, ParseError

STDIN = "-"


def _export(ctx: Context, args: argparse.Namespace) -> Outcome:
    rendered = render_export(read_export(ctx.conn, addresses=args.with_addresses))
    if isinstance(rendered, LineBreaks):
        return Failed(ExitCode.ERROR, tuple(f"line break in {field}" for field in rendered.fields))
    if args.output is None:
        return Done(rendered.removesuffix("\n"), {"text": rendered})
    ctx.write_output(Path(args.output), rendered)
    return Done(f"wrote {args.output}", {"path": args.output, "text": rendered})


def _read(path: str, ctx: Context) -> str:
    return ctx.stdin() if path == STDIN else Path(path).read_text(encoding="utf-8")


def _failed(errors: tuple[ParseError, ...], rejected: tuple[FieldError, ...]) -> Failed | None:
    messages = [f"{error.field}: {error.message}" for error in rejected]
    messages += [f"line {error.line}: {error.message}" for error in errors]
    return Failed(ExitCode.USAGE, tuple(messages)) if messages else None


def _import(ctx: Context, args: argparse.Namespace) -> Outcome:
    text = _read(args.file, ctx)
    groups, tags = tuple(args.group), tuple(args.tag)
    servers = ctx.config.routes.server_addresses
    preview = preview_import(ctx.conn, text, args.mode, groups, tags, servers)
    if (failed := _failed(preview.errors, preview.rejected)) is not None:
        return failed
    summary = format_summary(preview.diff)
    warnings = duplicate_warnings(preview.diff)
    if args.dry_run or not has_changes(preview.diff):
        return Done(summary, import_data(preview.diff, False), warnings=warnings)
    if not args.yes:
        match ctx.confirm(f"{summary}\nimport these changes?"):
            case None:
                return Failed(ExitCode.USAGE, ("stdin is not a terminal to confirm on, pass --yes to import without asking",))
            case False:
                return Failed(ExitCode.ERROR, ("not confirmed, nothing imported",))
    result = apply_import(ctx.conn, text, args.mode, groups, tags, servers, ctx.actor, ctx.now)
    if (failed := _failed(result.errors, result.rejected)) is not None:
        return failed
    return Done(format_summary(result.diff), import_data(result.diff, result.applied), notify=result.applied, warnings=duplicate_warnings(result.diff))


def register(commands: Commands) -> None:
    export = commands.command("export", "write domains, groups, routes and stored settings as text", _export)
    export.add_argument("--with-addresses", action="store_true", help="include resolver sightings")
    export.add_argument("-o", dest="output", metavar="FILE", help="write the text to FILE instead of stdout")
    importing = commands.command("import", "merge or replace domains, groups, routes and settings from a text file", _import)
    importing.add_argument("file", metavar=f"FILE|{STDIN}")
    importing.add_argument("--mode", type=ImportMode, choices=list(ImportMode), default=ImportMode.MERGE)
    importing.add_argument("--group", action="append", default=[], metavar=f"G|{NO_GROUP}", help=f"only domains of this group; '{NO_GROUP}' is domains without one; may be repeated")
    importing.add_argument("--tag", action="append", default=[], metavar="T", help="only domains with this tag; may be repeated")
    importing.add_argument("--dry-run", action="store_true", help="show what would change and write nothing")
    importing.add_argument("--yes", action="store_true", help="import without asking")
