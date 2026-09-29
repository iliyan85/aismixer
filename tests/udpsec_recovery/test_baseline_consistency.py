"""Consistency of the MP0 baseline artifacts with the code that enforces them.

Keeps SCENARIO_MATRIX.md, the harness catalogue and the MP1 acceptance tests
from drifting apart, keeps every cited existing test real, and keeps every
MP1 xfail strict (an XPASS must fail the run).
"""

import inspect
import re
from pathlib import Path

from . import test_mp1_liveness_acceptance as acceptance
from . import test_recovery_invariants as invariants
from .harness.scenarios import SCENARIOS

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
MATRIX = (HERE / "SCENARIO_MATRIX.md").read_text(encoding="utf-8")
ID_PATTERN = re.compile(r"^[ABCD]\d+[a-z]?$")
# Matrix rows tested by the MP0 lab without a scenario of their own.
DERIVED_MP0_ROWS = {"A8": "A3", "C4": "C3", "D7": "C3", "D8": "B1", "D9": "all"}
# MP1 requirements that already hold and are guarded by ordinary tests.
GUARD_REQUIREMENTS = {"MP1-L9", "MP1-L10"}


def _matrix_rows():
    body = MATRIX.split("<!-- matrix:start -->", 1)[1].split("<!-- matrix:end -->", 1)[0]
    lines = [line for line in body.splitlines() if line.startswith("| ")]
    header = [cell.strip() for cell in lines[0].strip("|").split("|")]
    rows = {}
    for line in lines[1:]:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        assert len(cells) == len(header), line
        row = dict(zip(header, cells))
        assert ID_PATTERN.match(row["ID"]), row["ID"]
        assert row["ID"] not in rows, f"duplicate matrix row {row['ID']}"
        rows[row["ID"]] = row
    return rows


def _acceptance_tests():
    return {
        name: function
        for name, function in inspect.getmembers(acceptance, inspect.isfunction)
        if name.startswith("test_mp1_")
    }


def _xfail_marks(function):
    return [mark for mark in getattr(function, "pytestmark", []) if mark.name == "xfail"]


def test_every_harness_scenario_has_an_mp0_matrix_row():
    rows = _matrix_rows()
    for scenario_id, spec in SCENARIOS.items():
        assert scenario_id in rows, f"{scenario_id} missing from SCENARIO_MATRIX.md"
        row = rows[scenario_id]
        assert "MP0" in row["Provenance"].split()
        assert row["Fable"] == (spec.fable_id or "—")


def test_every_mp0_matrix_row_is_backed_by_the_lab():
    for scenario_id, row in _matrix_rows().items():
        if "MP0" in row["Provenance"].split():
            assert scenario_id in SCENARIOS or scenario_id in DERIVED_MP0_ROWS, scenario_id


def test_matrix_requirements_match_the_acceptance_tests():
    rows = _matrix_rows()
    named = set()
    for scenario_id, row in rows.items():
        found = set(re.findall(r"MP1-L\d+[a-z]?", row["Future target"]))
        if "XFAIL" in row["Provenance"].split():
            assert found - GUARD_REQUIREMENTS, f"{scenario_id} is XFAIL but names no MP1 requirement"
        named |= found
    tests = _acceptance_tests()
    for requirement in named - GUARD_REQUIREMENTS:
        prefix = "test_" + requirement.lower().replace("-", "_") + "_"
        matching = [name for name in tests if name.startswith(prefix)]
        assert len(matching) == 1, f"{requirement}: acceptance tests {matching}"
    for name in tests:
        match = re.match(r"test_mp1_l(\d+)([a-z]?)_", name)
        assert match, name
        requirement = f"MP1-L{match.group(1)}{match.group(2)}"
        assert requirement in named, f"{name} has no matrix row"
    for requirement in GUARD_REQUIREMENTS:
        assert requirement in inspect.getsource(invariants), requirement


def test_every_mp1_marker_is_strict_and_names_the_scenario_it_runs():
    """While a marker exists it must be strict (an XPASS fails the run) and
    only an AssertionError may satisfy it. MP1 removes markers as each
    requirement is met."""
    for name, function in _acceptance_tests().items():
        for mark in _xfail_marks(function):
            assert mark.kwargs.get("strict") is True, name
            assert mark.kwargs.get("raises") is AssertionError, name
            reason = mark.kwargs.get("reason", "")
            match = re.match(r"MP1-L\d+[a-z]? \[([A-D]\d+[a-z]?)", reason)
            assert match, f"{name}: reason must start 'MP1-Lx [<scenario>'"
            assert f'run("{match.group(1)}"' in inspect.getsource(function), name


def test_every_cited_existing_test_exists():
    citations = MATRIX.split("## Existing project tests cited", 1)[1]
    checked = 0
    current_file = None
    for line in citations.splitlines():
        for token in re.findall(r"`([^`]+)`", line):
            if token.startswith("tests/"):
                path, _, name = token.partition("::")
                current_file = REPO / path
                assert current_file.is_file(), token
            elif token.startswith("::"):
                name = token[2:]
            else:
                continue
            if name:
                assert current_file is not None, token
                source = current_file.read_text(encoding="utf-8")
                assert re.search(rf"^def {re.escape(name)}\(", source, re.M), f"{current_file.name}::{name}"
                checked += 1
    assert checked >= 50
