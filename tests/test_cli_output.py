import pytest

from subnet_tracker.cli.output import Done, ExitCode, Failed, Rendered, render, table, warning


def test_exit_codes_follow_the_design() -> None:
    assert {c.name: int(c) for c in ExitCode} == {"OK": 0, "ERROR": 1, "USAGE": 2, "PARTIAL": 3}


def test_table_pads_columns_and_strips_line_ends() -> None:
    assert table(("key", "value", "default"), [("a.b", "1", "1"), ("long.key", "", "x"), ("c", "22", "")]) == "key       value  default\na.b       1      1\nlong.key         x\nc         22"


def test_table_without_rows_is_its_header() -> None:
    assert table(("key", "value"), []) == "key  value"


def test_text_gets_a_line_end() -> None:
    assert render(Done("one\ntwo", {"x": 1}), as_json=False) == Rendered("one\ntwo\n", "")


def test_empty_text_prints_nothing() -> None:
    assert render(Done("", None), as_json=False) == Rendered("", "")


def test_json_is_indented_and_keeps_non_ascii() -> None:
    assert render(Done("ignored", {"name": "пример.рф", "ids": [1]}), as_json=True) == Rendered('{\n  "name": "пример.рф",\n  "ids": [\n    1\n  ]\n}\n', "")


@pytest.mark.parametrize("as_json", [False, True])
def test_failure_is_text_on_stderr(as_json: bool) -> None:
    assert render(Failed(ExitCode.USAGE, ("first", "second")), as_json) == Rendered("", "subnet-tracker: error: first\nsubnet-tracker: error: second\n")


def test_warning_line() -> None:
    assert warning("tick not notified") == "subnet-tracker: warning: tick not notified\n"
