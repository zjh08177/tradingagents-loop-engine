"""TDD tests for the loop-engine phase-driver.

The engine is a stateless step-function over status.json: given the ledger,
compute the single next actionable step across the W0-W4 wave/gate graph,
halting at ratify gates and external-prereq blockers. It never fakes green.
"""
import json
import pathlib

import engine

HERE = pathlib.Path(__file__).resolve().parent
LEDGER_PATH = HERE.parent / "status.json"


# ---- synthetic ledger builders -------------------------------------------
def mk_wave(wid, task_status, gate_verdicts, blocked=None, tier="A"):
    return {
        "id": wid,
        "title": wid,
        "tier": tier,
        "blocked_by_prereqs": blocked or [],
        "tasks": [
            {"id": f"{wid}.T{i}", "desc": "", "status": s}
            for i, s in enumerate(task_status)
        ],
        "gate_tests": [
            {"id": f"{wid}_g{i}", "verdict": v} for i, v in enumerate(gate_verdicts)
        ],
    }


def mk_ledger(waves, prereqs=None, ratify=None, tier_b=None):
    return {
        "schema_version": 1,
        "project": "x",
        "updated_at": None,
        "prerequisites": prereqs or [],
        "waves": waves,
        "ratify_gates": ratify or [],
        "tier_b": tier_b
        or {"status": "pending", "blocked_by_prereqs": [], "criteria": []},
    }


def prereq(pid, status, blocks=None):
    return {"id": pid, "desc": "", "owner": "x", "status": status, "blocks": blocks or []}


# ---- wave_is_green: the negative-control core (EC3) -----------------------
def test_wave_green_requires_every_gate_test_pass():
    w = mk_wave("W0", ["done"], ["pass", "pass"])
    assert engine.wave_is_green(w) is True


def test_wave_not_green_if_any_gate_test_unknown():
    w = mk_wave("W0", ["done"], ["pass", "unknown"])
    assert engine.wave_is_green(w) is False


def test_wave_not_green_if_any_gate_test_fails():
    # negative control teeth: a single failing gate_test cannot close the wave
    w = mk_wave("W0", ["done"], ["pass", "fail"])
    assert engine.wave_is_green(w) is False


def test_empty_gate_tests_is_not_green():
    # a wave with no recorded gate tests has nothing proving it -> not green
    w = mk_wave("W0", ["done"], [])
    assert engine.wave_is_green(w) is False


# ---- compute_next_action: the phase graph (EC2) ---------------------------
def test_prereq_blocks_build_and_halts():
    led = mk_ledger(
        [mk_wave("W0", ["pending"], ["unknown"], blocked=["P2"])],
        prereqs=[prereq("P2", "pending")],
    )
    act = engine.compute_next_action(led)
    assert act["action"] == "HALT"
    assert act["reason"] == "prereq"
    assert "P2" in act["prereqs"]


def test_build_first_pending_task_when_prereqs_done():
    led = mk_ledger(
        [mk_wave("W0", ["pending", "pending"], ["unknown"], blocked=["P2"])],
        prereqs=[prereq("P2", "done")],
    )
    act = engine.compute_next_action(led)
    assert act["action"] == "BUILD"
    assert act["wave"] == "W0"
    assert act["task"] == "W0.T0"
    assert act["halt"] is False


def test_verify_when_tasks_done_but_gate_tests_unproven():
    led = mk_ledger([mk_wave("W0", ["done", "done"], ["unknown", "pass"])])
    act = engine.compute_next_action(led)
    assert act["action"] == "VERIFY"
    assert act["wave"] == "W0"
    assert act["tests"] == ["W0_g0"]  # only the unproven one


def test_advances_to_next_wave_once_prior_is_green():
    led = mk_ledger(
        [
            mk_wave("W0", ["done"], ["pass"]),
            mk_wave("W1", ["pending"], ["unknown"]),
        ]
    )
    act = engine.compute_next_action(led)
    assert act["action"] == "BUILD"
    assert act["wave"] == "W1"


def test_failing_gate_test_blocks_advance_negative_control():
    # W0 tasks all done, but one gate_test FAILS -> must VERIFY W0, never reach W1
    led = mk_ledger(
        [
            mk_wave("W0", ["done"], ["fail"]),
            mk_wave("W1", ["pending"], ["unknown"]),
        ]
    )
    act = engine.compute_next_action(led)
    assert act["action"] == "VERIFY"
    assert act["wave"] == "W0"


def test_ratify_gate_halts_before_tier_b():
    led = mk_ledger(
        [mk_wave("W0", ["done"], ["pass"])],
        ratify=[{"id": "RG1", "desc": "", "after": "W0", "status": "pending"}],
    )
    act = engine.compute_next_action(led)
    assert act["action"] == "HALT"
    assert act["reason"] == "ratify"
    assert act["gate"] == "RG1"


def test_tier_b_live_accept_after_rg1_ratified():
    led = mk_ledger(
        [mk_wave("W0", ["done"], ["pass"])],
        prereqs=[prereq("P1", "done")],
        ratify=[{"id": "RG1", "desc": "", "after": "W0", "status": "ratified"}],
        tier_b={
            "status": "pending",
            "blocked_by_prereqs": ["P1"],
            "criteria": [{"id": "EC-A1", "verdict": "unknown"}],
        },
    )
    act = engine.compute_next_action(led)
    assert act["action"] == "LIVE_ACCEPT"
    assert "EC-A1" in act["tests"]


def test_tier_b_blocked_by_external_prereq_p1():
    led = mk_ledger(
        [mk_wave("W0", ["done"], ["pass"])],
        prereqs=[prereq("P1", "pending")],
        ratify=[{"id": "RG1", "desc": "", "after": "W0", "status": "ratified"}],
        tier_b={
            "status": "pending",
            "blocked_by_prereqs": ["P1"],
            "criteria": [{"id": "EC-A1", "verdict": "unknown"}],
        },
    )
    act = engine.compute_next_action(led)
    assert act["action"] == "HALT"
    assert act["reason"] == "prereq"
    assert "P1" in act["prereqs"]


def test_ship_when_everything_green_and_ratified():
    led = mk_ledger(
        [mk_wave("W0", ["done"], ["pass"])],
        prereqs=[prereq("P1", "done")],
        ratify=[
            {"id": "RG1", "desc": "", "after": "W0", "status": "ratified"},
            {"id": "RG2", "desc": "", "after": "tier_b", "status": "ratified"},
        ],
        tier_b={
            "status": "pending",
            "blocked_by_prereqs": ["P1"],
            "criteria": [{"id": "EC-A1", "verdict": "pass"}],
        },
    )
    act = engine.compute_next_action(led)
    assert act["action"] == "SHIP"


# ---- validate_ledger (EC1) ------------------------------------------------
def test_validate_flags_missing_schema_version():
    bad = mk_ledger([mk_wave("W0", ["done"], ["pass"])])
    del bad["schema_version"]
    errs = engine.validate_ledger(bad)
    assert any("schema_version" in e for e in errs)


def test_validate_flags_unknown_verdict_value():
    bad = mk_ledger([mk_wave("W0", ["done"], ["MAYBE"])])
    errs = engine.validate_ledger(bad)
    assert any("verdict" in e.lower() for e in errs)


# ---- the real seeded ledger (EC1 + EC2) -----------------------------------
def test_real_status_json_validates_and_is_complete():
    led = engine.load_ledger(LEDGER_PATH)
    assert engine.validate_ledger(led) == []
    wave_ids = [w["id"] for w in led["waves"]]
    assert wave_ids == ["W0", "W1", "W2", "W3", "W4"]
    assert {p["id"] for p in led["prerequisites"]} == {"P1", "P2", "P3", "P4"}
    assert {g["id"] for g in led["ratify_gates"]} == {"RG1", "RG2"}
    assert len(led["tier_b"]["criteria"]) == 9  # EC-A1..A9


def test_real_fresh_ledger_next_action_is_do_prereqs():
    # seeded fresh: P2/P3/P4 pending block W0 -> the engine demands prereqs first
    led = engine.load_ledger(LEDGER_PATH)
    act = engine.compute_next_action(led)
    assert act["action"] == "HALT"
    assert act["reason"] == "prereq"
    assert set(act["prereqs"]) >= {"P2", "P3", "P4"}


# ---- state-mutation helpers (the human/CI write interface) ----------------
def test_record_test_then_recompute_closes_the_wave():
    led = mk_ledger(
        [
            mk_wave("W0", ["done"], ["unknown"]),
            mk_wave("W1", ["pending"], ["unknown"]),
        ]
    )
    engine.record_test(led, "W0", "W0_g0", "pass")
    act = engine.compute_next_action(led)
    assert act["action"] == "BUILD" and act["wave"] == "W1"


def test_record_test_rejects_unknown_verdict():
    import pytest

    led = mk_ledger([mk_wave("W0", ["done"], ["unknown"])])
    with pytest.raises(ValueError):
        engine.record_test(led, "W0", "W0_g0", "MAYBE")


def test_ratify_flips_gate_and_unblocks():
    led = mk_ledger(
        [mk_wave("W0", ["done"], ["pass"])],
        ratify=[{"id": "RG1", "desc": "", "after": "W0", "status": "pending"}],
        prereqs=[prereq("P1", "done")],
        tier_b={"status": "pending", "blocked_by_prereqs": ["P1"], "criteria": []},
    )
    assert engine.compute_next_action(led)["action"] == "HALT"
    engine.ratify(led, "RG1")
    # tier_b has no criteria -> straight to ship-ratify HALT on RG2-absent => SHIP
    assert engine.compute_next_action(led)["action"] == "SHIP"


def test_set_prereq_done_unblocks_build():
    led = mk_ledger(
        [mk_wave("W0", ["pending"], ["unknown"], blocked=["P2"])],
        prereqs=[prereq("P2", "pending")],
    )
    assert engine.compute_next_action(led)["action"] == "HALT"
    engine.set_prereq(led, "P2", "done")
    assert engine.compute_next_action(led)["action"] == "BUILD"


# ---- Tier-B criteria mutation (record-accept) -----------------------------
def test_record_accept_drives_tier_b_to_ship():
    led = mk_ledger(
        [mk_wave("W0", ["done"], ["pass"])],
        prereqs=[prereq("P1", "done")],
        ratify=[
            {"id": "RG1", "desc": "", "after": "W0", "status": "ratified"},
            {"id": "RG2", "desc": "", "after": "tier_b", "status": "ratified"},
        ],
        tier_b={
            "status": "pending",
            "blocked_by_prereqs": ["P1"],
            "criteria": [
                {"id": "EC-A1", "verdict": "unknown"},
                {"id": "EC-A2", "verdict": "unknown"},
            ],
        },
    )
    assert engine.compute_next_action(led)["action"] == "LIVE_ACCEPT"
    engine.record_accept(led, "EC-A1", "pass")
    engine.record_accept(led, "EC-A2", "pass")
    assert engine.compute_next_action(led)["action"] == "SHIP"


def test_record_accept_rejects_bad_verdict():
    import pytest

    led = mk_ledger(
        [mk_wave("W0", ["done"], ["pass"])],
        tier_b={"status": "pending", "blocked_by_prereqs": [],
                "criteria": [{"id": "EC-A1", "verdict": "unknown"}]},
    )
    with pytest.raises(ValueError):
        engine.record_accept(led, "EC-A1", "green")


def test_record_accept_unknown_criterion_raises():
    import pytest

    led = mk_ledger(
        [mk_wave("W0", ["done"], ["pass"])],
        tier_b={"status": "pending", "blocked_by_prereqs": [],
                "criteria": [{"id": "EC-A1", "verdict": "unknown"}]},
    )
    with pytest.raises(KeyError):
        engine.record_accept(led, "EC-A99", "pass")


# ---- dispatch is an honest stub (EC4) -------------------------------------
def test_dispatch_build_is_not_yet_wired():
    import pytest

    with pytest.raises(NotImplementedError):
        engine.dispatch({"action": "BUILD", "wave": "W0", "task": "W0.T0"})
