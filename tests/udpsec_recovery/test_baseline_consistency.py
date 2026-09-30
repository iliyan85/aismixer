"""Consistency of the recovery baseline artifacts with the code that enforces them.

Keeps SCENARIO_MATRIX.md, the harness catalogue and the MP1 acceptance tests
from drifting apart, keeps every cited existing test real, and keeps the
MP1 acceptance tests free of xfail markers: MP1 is implemented, so a marker
there could only hide a regression.
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
REQUIREMENT = re.compile(r"MP1-L\d+[a-z]?")
MP2_REQUIREMENT = re.compile(r"MP2-B(\d+)")
MP2_TESTS = REPO / "tests" / "test_udpsec_path_challenge_retry.py"
# Provenance tags meaning "executed by the repository lab".
LAB_PROVENANCE = {"MP0", "MP1", "MP2"}
# Matrix rows tested by the lab without a scenario of their own.
DERIVED_MP0_ROWS = {"A8": "A3", "C4": "C3", "D7": "C3", "D8": "B1", "D9": "all"}
# MP1 requirements guarded by ordinary tests outside the acceptance module.
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


def _provenance(row):
    return set(row["Provenance"].split())


def _acceptance_tests():
    return {
        name: function
        for name, function in inspect.getmembers(acceptance, inspect.isfunction)
        if name.startswith("test_mp1_")
    }


def _requirement_of(test_name):
    match = re.match(r"test_mp1_l(\d+)([a-z]?)_", test_name)
    assert match, test_name
    return f"MP1-L{match.group(1)}{match.group(2)}"


def test_every_harness_scenario_has_a_lab_backed_matrix_row():
    rows = _matrix_rows()
    for scenario_id, spec in SCENARIOS.items():
        assert scenario_id in rows, f"{scenario_id} missing from SCENARIO_MATRIX.md"
        row = rows[scenario_id]
        assert LAB_PROVENANCE & _provenance(row), scenario_id
        assert row["Fable"] == (spec.fable_id or "—")


def test_every_lab_backed_matrix_row_is_backed_by_the_lab():
    for scenario_id, row in _matrix_rows().items():
        if LAB_PROVENANCE & _provenance(row):
            assert scenario_id in SCENARIOS or scenario_id in DERIVED_MP0_ROWS, scenario_id


def test_no_row_claims_a_strict_xfail_any_more():
    for scenario_id, row in _matrix_rows().items():
        assert "XFAIL" not in _provenance(row), scenario_id


def test_matrix_requirements_match_the_acceptance_tests():
    rows = _matrix_rows()
    named = {}
    for scenario_id, row in rows.items():
        for requirement in REQUIREMENT.findall(row["Target"]):
            named.setdefault(requirement, []).append(scenario_id)
            if requirement not in GUARD_REQUIREMENTS:
                assert "MP1" in _provenance(row), (
                    f"{scenario_id} names {requirement} but has no MP1 provenance"
                )
    tests = _acceptance_tests()
    for requirement, scenario_ids in named.items():
        if requirement in GUARD_REQUIREMENTS:
            continue
        prefix = "test_" + requirement.lower().replace("-", "_") + "_"
        matching = [name for name in tests if name.startswith(prefix)]
        assert len(matching) == 1, f"{requirement}: acceptance tests {matching}"
        source = inspect.getsource(tests[matching[0]])
        assert any(f'run("{scenario_id}"' in source for scenario_id in scenario_ids), (
            f"{matching[0]} runs none of the scenarios naming {requirement}: {scenario_ids}"
        )
    for name in tests:
        assert _requirement_of(name) in named, f"{name} has no matrix row"
    for requirement in GUARD_REQUIREMENTS:
        assert requirement in named, requirement
        assert requirement in inspect.getsource(invariants), requirement


def test_mp2_requirements_named_in_the_matrix_have_tests():
    """Every MP2-B<N> scenario the matrix names is implemented by at least
    one `test_mp2_b<N>_` test in the MP2 test module, and a row naming one
    carries MP2 provenance."""
    source = MP2_TESTS.read_text(encoding="utf-8")
    implemented = set(re.findall(r"^def test_mp2_b(\d+)_", source, re.M))
    named = set()
    for scenario_id, row in _matrix_rows().items():
        found = set(MP2_REQUIREMENT.findall(row["Target"]))
        if found:
            assert "MP2" in _provenance(row), scenario_id
        named |= found
    assert named, "the matrix names no MP2 requirement"
    assert named <= implemented, sorted(named - implemented)


def test_mp1_acceptance_tests_carry_no_xfail_marker():
    for name, function in _acceptance_tests().items():
        marks = [mark.name for mark in getattr(function, "pytestmark", [])]
        assert "xfail" not in marks, name
        assert "skip" not in marks and "skipif" not in marks, name


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
