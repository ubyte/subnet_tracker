from dataclasses import fields
from enum import Enum, auto
from typing import TYPE_CHECKING

from subnet_tracker.types import PREFIX_V4, PREFIX_V6, FieldError, check_prefix

if TYPE_CHECKING:
    from _typeshed import DataclassInstance


class Keep(Enum):
    KEEP = auto()


KEEP = Keep.KEEP


def prefixes_error(prefix_v4: int | Keep | None, prefix_v6: int | Keep | None) -> FieldError | None:
    for field, prefix, limits in (("prefix_v4", prefix_v4, PREFIX_V4), ("prefix_v6", prefix_v6, PREFIX_V6)):
        if not isinstance(prefix, Keep) and (error := check_prefix(field, prefix, limits)):
            return error
    return None


def edit_columns(edit: "DataclassInstance") -> dict[str, object]:
    values = {f.name: getattr(edit, f.name) for f in fields(edit)}
    return {column: int(v) if isinstance(v, bool) else v for column, v in values.items() if not isinstance(v, Keep)}
