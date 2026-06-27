"""TDD for the VERIFY dispatch: run a wave's gate tests via real pytest and
record per-test verdicts into the ledger. Proves the engine→build binding with
a fixture suite (one pass, one fail) driven in a subprocess.
"""
import pathlib

import engine

HERE = pathlib.Path(__file__).resolve().parent
SELFTEST = str(HERE.parent / "dispatch_selftest")


def _ledger():
    return {
        "schema_version": 1,
        "project": "x",
        "updated_at": None,
        "v1_suite_dir": None,
        "prerequisites": [],
        "ratify_gates": [],
        "waves": [
            {
                "id": "WX",
                "title": "WX",
                "tier": "A",
                "blocked_by_prereqs": [],
                "tasks": [{"id": "WX.T0", "desc": "", "status": "done"}],
                "gate_tests": [
                    {"id": "test_sample_pass", "verdict": "unknown"},
                    {"id": "test_sample_fail", "verdict": "unknown"},
                ],
            }
        ],
        "tier_b": {"status": "pending", "blocked_by_prereqs": [], "criteria": []},
    }


def test_run_verify_records_real_pytest_outcomes():
    led = _ledger()
    summary = engine.run_verify(
        led, "WX", ["test_sample_pass", "test_sample_fail"], SELFTEST
    )
    assert summary["recorded"]["test_sample_pass"] == "pass"
    assert summary["recorded"]["test_sample_fail"] == "fail"
    verdicts = {g["id"]: g["verdict"] for g in led["waves"][0]["gate_tests"]}
    assert verdicts == {"test_sample_pass": "pass", "test_sample_fail": "fail"}


def test_run_verify_flags_missing_tests():
    led = _ledger()
    led["waves"][0]["gate_tests"].append({"id": "test_absent", "verdict": "unknown"})
    summary = engine.run_verify(led, "WX", ["test_absent"], SELFTEST)
    assert "test_absent" in summary["missing"]
    # a test that does not exist is never silently marked pass
    verdicts = {g["id"]: g["verdict"] for g in led["waves"][0]["gate_tests"]}
    assert verdicts["test_absent"] == "unknown"


def test_dispatch_verify_refuses_without_suite_dir():
    import pytest

    led = {"v1_suite_dir": None}
    with pytest.raises(NotImplementedError):
        engine.dispatch(
            {"action": "VERIFY", "wave": "WX", "tests": ["test_sample_pass"]}, led=led
        )


def test_dispatch_verify_runs_when_suite_dir_set():
    led = _ledger()
    led["v1_suite_dir"] = SELFTEST
    summary = engine.dispatch(
        {"action": "VERIFY", "wave": "WX", "tests": ["test_sample_pass"]}, led=led
    )
    assert summary["recorded"]["test_sample_pass"] == "pass"
