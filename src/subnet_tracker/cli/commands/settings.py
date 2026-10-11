import argparse
from collections.abc import Sequence

from subnet_tracker.cli.command import Commands, Context
from subnet_tracker.cli.output import Done, ExitCode, Failed, Outcome, table
from subnet_tracker.core.events import JsonValue
from subnet_tracker.core.services.settings import change_setting
from subnet_tracker.core.settings import SettingError, SettingState, SettingUpdate
from subnet_tracker.core.settings_store import get_setting, list_settings


def settings_table(states: Sequence[SettingState]) -> str:
    return table(("key", "value", "default"), [(s.key, s.value, s.default) for s in states])


def state_json(state: SettingState) -> JsonValue:
    return {"key": state.key, "value": state.value, "default": state.default, "updated_at": state.updated_at}


def update_text(update: SettingUpdate) -> str:
    return f"{update.new.key} = {update.new.text}" + ("" if update.changed else " (unchanged)")


def _rejected(error: SettingError) -> Failed:
    return Failed(ExitCode.USAGE, (str(error),))


def _list(ctx: Context, args: argparse.Namespace) -> Outcome:
    states = list_settings(ctx.conn)
    return Done(settings_table(states), [state_json(s) for s in states])


def _get(ctx: Context, args: argparse.Namespace) -> Outcome:
    state = get_setting(ctx.conn, args.key)
    return _rejected(state) if isinstance(state, SettingError) else Done(state.value, state_json(state))


def _change(ctx: Context, key: str, text: str | None) -> Outcome:
    update = change_setting(ctx.conn, key, text, ctx.actor, ctx.now)
    if isinstance(update, SettingError):
        return _rejected(update)
    return Done(update_text(update), {"key": update.new.key, "value": update.new.text, "old": update.old, "changed": update.changed}, notify=update.changed)


def _set(ctx: Context, args: argparse.Namespace) -> Outcome:
    return _change(ctx, args.key, args.value)


def _reset(ctx: Context, args: argparse.Namespace) -> Outcome:
    return _change(ctx, args.key, None)


def register(commands: Commands) -> None:
    settings = commands.group("settings", "global settings stored in the database")
    settings.command("list", "show every setting with its value and default", _list)
    settings.command("get", "print the value of a setting", _get).add_argument("key", metavar="KEY")
    set_command = settings.command("set", "change a setting; its default value resets it", _set)
    set_command.add_argument("key", metavar="KEY")
    set_command.add_argument("value", metavar="VALUE")
    settings.command("reset", "return a setting to its default", _reset).add_argument("key", metavar="KEY")
