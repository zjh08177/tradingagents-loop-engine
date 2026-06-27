#!/usr/bin/env python3
"""loop-engine — the phase-driver for the tradingagents v1 build.

A stateless step-function over ``status.json``: given the ledger, compute the
single next actionable step across the W0-W4 wave/gate graph, halting at
human-ratify gates and external-prerequisite blockers. It is RE-INVOKED to make
progress (no long-running process); a cron/Workflow wrapper drives cadence later.

Design invariants
  - A wave closes GREEN only when EVERY gate_test has a recorded ``pass`` verdict
    (negative control: one failing/unproven test cannot close a wave).
  - Dispatch (running builds / pytest / the live-acceptance harness) is an honest
    stub until the v1 product exists — the engine never fakes a green verdict.

Usage
  python3 engine.py next                 # print the next action (dry run)
  python3 engine.py status               # one-line-per-wave summary
  python3 engine.py validate             # schema/consistency check
  python3 engine.py record-test W0 test_edgar_vendor pass
  python3 engine.py record-task T0.2 done
  python3 engine.py prereq P2 done
  python3 engine.py ratify RG1
  python3 engine.py advance              # dispatch the next action (stubbed)
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

VERDICTS = {"unknown", "pass", "fail"}
TASK_STATES = {"pending", "in_progress", "done"}
PREREQ_STATES = {"pending", "done"}
RATIFY_STATES = {"pending", "ratified"}

DEFAULT_LEDGER = pathlib.Path(__file__).resolve().parent / "status.json"


# --------------------------------------------------------------------------- io
def load_ledger(path=DEFAULT_LEDGER) -> dict:
    return json.loads(pathlib.Path(path).read_text())


def save_ledger(led: dict, path=DEFAULT_LEDGER) -> None:
    led["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    pathlib.Path(path).write_text(json.dumps(led, indent=2) + "\n")


# ------------------------------------------------------------------- predicates
def wave_is_green(wave: dict) -> bool:
    """A wave is green iff it has gate tests AND every one passed."""
    gates = wave.get("gate_tests", [])
    return bool(gates) and all(g["verdict"] == "pass" for g in gates)


def _prereq(led: dict, pid: str) -> dict | None:
    for p in led["prerequisites"]:
        if p["id"] == pid:
            return p
    return None


def _prereq_done(led: dict, pid: str) -> bool:
    p = _prereq(led, pid)
    # absent prereq is treated as NOT done (fail-loud; validate_ledger flags it)
    return p is not None and p["status"] == "done"


def _ratify_after(led: dict, after: str) -> dict | None:
    for g in led["ratify_gates"]:
        if g["after"] == after:
            return g
    return None


# ----------------------------------------------------------------- action build
def _act(action, *, halt=False, **extra) -> dict:
    out = {
        "action": action,
        "halt": halt,
        "reason": None,
        "wave": None,
        "task": None,
        "gate": None,
        "prereqs": None,
        "tests": None,
        "detail": "",
    }
    out.update(extra)
    return out


def compute_next_action(led: dict) -> dict:
    """The phase graph. Returns the single next step (or a HALT/SHIP terminal)."""
    for wave in led["waves"]:
        if wave_is_green(wave):
            rg = _ratify_after(led, wave["id"])
            if rg and rg["status"] != "ratified":
                return _act(
                    "HALT", halt=True, reason="ratify", gate=rg["id"],
                    detail=f"{rg['id']}: {rg.get('desc', '')}",
                )
            continue

        # frontier wave (first non-green) ---------------------------------
        blocking = [p for p in wave.get("blocked_by_prereqs", []) if not _prereq_done(led, p)]
        if blocking:
            return _act(
                "HALT", halt=True, reason="prereq", wave=wave["id"], prereqs=blocking,
                detail=f"{wave['id']} blocked by prerequisites {blocking}",
            )

        pending = [t for t in wave["tasks"] if t["status"] != "done"]
        if pending:
            t = pending[0]
            return _act(
                "BUILD", wave=wave["id"], task=t["id"],
                detail=f"build {t['id']}: {t.get('desc', '')}",
            )

        unproven = [g["id"] for g in wave["gate_tests"] if g["verdict"] != "pass"]
        return _act(
            "VERIFY", wave=wave["id"], tests=unproven,
            detail=f"{wave['id']} tasks done; run gate tests {unproven}",
        )

    # all waves green ------------------------------------------------------
    tb = led["tier_b"]
    blocking = [p for p in tb.get("blocked_by_prereqs", []) if not _prereq_done(led, p)]
    if blocking:
        return _act(
            "HALT", halt=True, reason="prereq", prereqs=blocking,
            detail=f"Tier-B (live acceptance) blocked by prerequisites {blocking}",
        )

    failing = [c["id"] for c in tb["criteria"] if c["verdict"] != "pass"]
    if failing:
        return _act(
            "LIVE_ACCEPT", tests=failing,
            detail=f"run the live-acceptance harness for {failing}",
        )

    rg = _ratify_after(led, "tier_b")
    if rg and rg["status"] != "ratified":
        return _act(
            "HALT", halt=True, reason="ratify", gate=rg["id"],
            detail=f"{rg['id']}: {rg.get('desc', '')}",
        )

    return _act("SHIP", detail="all Tier-A + Tier-B gates green and ratified")


# ------------------------------------------------------------------- mutations
def record_test(led: dict, wave_id: str, test_id: str, verdict: str) -> None:
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {sorted(VERDICTS)}, got {verdict!r}")
    for wave in led["waves"]:
        if wave["id"] != wave_id:
            continue
        for g in wave["gate_tests"]:
            if g["id"] == test_id:
                g["verdict"] = verdict
                return
    raise KeyError(f"no gate test {test_id!r} in wave {wave_id!r}")


def record_accept(led: dict, criterion_id: str, verdict: str) -> None:
    """Record a Tier-B live-acceptance criterion (EC-A1..A9) verdict."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {sorted(VERDICTS)}, got {verdict!r}")
    for c in led["tier_b"]["criteria"]:
        if c["id"] == criterion_id:
            c["verdict"] = verdict
            return
    raise KeyError(f"no Tier-B criterion {criterion_id!r}")


def set_task(led: dict, task_id: str, status: str) -> None:
    if status not in TASK_STATES:
        raise ValueError(f"status must be one of {sorted(TASK_STATES)}, got {status!r}")
    for wave in led["waves"]:
        for t in wave["tasks"]:
            if t["id"] == task_id:
                t["status"] = status
                return
    raise KeyError(f"no task {task_id!r}")


def set_prereq(led: dict, pid: str, status: str) -> None:
    if status not in PREREQ_STATES:
        raise ValueError(f"status must be one of {sorted(PREREQ_STATES)}, got {status!r}")
    p = _prereq(led, pid)
    if p is None:
        raise KeyError(f"no prerequisite {pid!r}")
    p["status"] = status


def ratify(led: dict, gate_id: str) -> None:
    for g in led["ratify_gates"]:
        if g["id"] == gate_id:
            g["status"] = "ratified"
            return
    raise KeyError(f"no ratify gate {gate_id!r}")


# -------------------------------------------------------------------- dispatch
def run_verify(led: dict, wave_id: str, test_ids: list[str], suite_dir: str) -> dict:
    """Run the named gate tests via real pytest and record per-test verdicts.

    Selects the tests with ``-k`` in ``suite_dir``, parses pytest's JUnit XML
    (stdlib, no plugin), and records pass/fail into the ledger. A requested test
    with no matching testcase is reported as ``missing`` and left ``unknown`` —
    a missing test is never silently marked pass.
    """
    with tempfile.TemporaryDirectory() as d:
        xml_path = os.path.join(d, "report.xml")
        kexpr = " or ".join(test_ids)
        subprocess.run(
            [sys.executable, "-m", "pytest", suite_dir, "-q", "-p", "no:cacheprovider",
             "-k", kexpr, "--junit-xml", xml_path],
            capture_output=True,
        )
        if not os.path.exists(xml_path):
            raise RuntimeError(f"pytest produced no JUnit XML for {suite_dir!r}/{kexpr!r}")
        root = ET.parse(xml_path).getroot()

    passed_by_name: dict[str, bool] = {}
    for case in root.iter("testcase"):
        ok = not any(child.tag in ("failure", "error", "skipped") for child in case)
        passed_by_name[case.get("name")] = ok

    recorded: dict[str, str] = {}
    missing: list[str] = []
    for tid in test_ids:
        matches = [ok for name, ok in passed_by_name.items() if name == tid or tid in name]
        if not matches:
            missing.append(tid)
            continue
        verdict = "pass" if all(matches) else "fail"
        record_test(led, wave_id, tid, verdict)
        recorded[tid] = verdict
    return {"recorded": recorded, "missing": missing}


def dispatch(action: dict, led: dict | None = None, suite_dir: str | None = None):
    """Execute the next action.

    VERIFY      -> run the wave's gate_tests via pytest + record each (run_verify).
    BUILD       -> hand the task to auto-debug / the feature-dev workflow (TODO).
    LIVE_ACCEPT -> drive the AI live-acceptance harness, eval/acceptance/ (TODO).

    VERIFY needs a v1 test suite to run against (``suite_dir`` or the ledger's
    ``v1_suite_dir``). BUILD/LIVE_ACCEPT have no engine yet, so we refuse loudly
    rather than fake a verdict.
    """
    a = action["action"]
    if a == "VERIFY":
        sdir = suite_dir or (led or {}).get("v1_suite_dir")
        if not sdir:
            raise NotImplementedError(
                "VERIFY needs v1_suite_dir set in status.json "
                "(point it at the v1 test suite once W0 stands it up)."
            )
        return run_verify(led, action["wave"], action["tests"], sdir)
    if a in {"BUILD", "LIVE_ACCEPT"}:
        raise NotImplementedError(
            f"dispatch({a}) not wired yet — connect to "
            "auto-debug (BUILD) / eval.acceptance (LIVE_ACCEPT). "
            "Drive the ledger via record-* commands meanwhile."
        )
    # HALT / SHIP are terminal — nothing to dispatch.


# -------------------------------------------------------------------- validate
def validate_ledger(led: dict) -> list[str]:
    errs: list[str] = []
    if led.get("schema_version") != 1:
        errs.append("schema_version must be 1")
    for key in ("project", "waves", "prerequisites", "ratify_gates", "tier_b"):
        if key not in led:
            errs.append(f"missing top-level key: {key}")
    if errs:
        return errs

    prereq_ids = {p["id"] for p in led["prerequisites"]}
    for p in led["prerequisites"]:
        if p["status"] not in PREREQ_STATES:
            errs.append(f"prereq {p['id']}: bad status {p['status']!r}")
    for wave in led["waves"]:
        for t in wave["tasks"]:
            if t["status"] not in TASK_STATES:
                errs.append(f"task {t['id']}: bad status {t['status']!r}")
        for g in wave["gate_tests"]:
            if g["verdict"] not in VERDICTS:
                errs.append(f"{wave['id']}/{g['id']}: bad verdict {g['verdict']!r}")
        for pid in wave.get("blocked_by_prereqs", []):
            if pid not in prereq_ids:
                errs.append(f"{wave['id']}: references undefined prereq {pid!r}")
    for g in led["ratify_gates"]:
        if g["status"] not in RATIFY_STATES:
            errs.append(f"ratify {g['id']}: bad status {g['status']!r}")
    tb = led["tier_b"]
    for pid in tb.get("blocked_by_prereqs", []):
        if pid not in prereq_ids:
            errs.append(f"tier_b: references undefined prereq {pid!r}")
    for c in tb.get("criteria", []):
        if c["verdict"] not in VERDICTS:
            errs.append(f"tier_b/{c['id']}: bad verdict {c['verdict']!r}")
    return errs


# -------------------------------------------------------------------- summary
def status_lines(led: dict) -> list[str]:
    lines = []
    for w in led["waves"]:
        done = sum(t["status"] == "done" for t in w["tasks"])
        passed = sum(g["verdict"] == "pass" for g in w["gate_tests"])
        flag = "GREEN" if wave_is_green(w) else "...."
        lines.append(
            f"{w['id']} [{flag:5}] tasks {done}/{len(w['tasks'])}  "
            f"gates {passed}/{len(w['gate_tests'])}"
        )
    pend = [p["id"] for p in led["prerequisites"] if p["status"] != "done"]
    lines.append(f"prereqs pending: {pend or 'none'}")
    lines.append(
        "ratify: " + ", ".join(f"{g['id']}={g['status']}" for g in led["ratify_gates"])
    )
    return lines


# ------------------------------------------------------------------------- cli
def _human(act: dict) -> str:
    tag = "HALT" if act["halt"] else act["action"]
    return f"[{tag}] {act['detail']}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="engine", description=__doc__)
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("next")
    sub.add_parser("status")
    sub.add_parser("validate")
    sub.add_parser("advance")
    p = sub.add_parser("verify")
    p.add_argument("wave")
    p = sub.add_parser("record-test")
    p.add_argument("wave"); p.add_argument("test_id"); p.add_argument("verdict")
    p = sub.add_parser("record-task")
    p.add_argument("task_id"); p.add_argument("status")
    p = sub.add_parser("record-accept")
    p.add_argument("criterion_id"); p.add_argument("verdict")
    p = sub.add_parser("prereq")
    p.add_argument("pid"); p.add_argument("status")
    p = sub.add_parser("ratify")
    p.add_argument("gate_id")
    args = ap.parse_args(argv)

    led = load_ledger(args.ledger)

    if args.cmd == "validate":
        errs = validate_ledger(led)
        print("\n".join(errs) if errs else "ledger valid")
        return 1 if errs else 0
    if args.cmd == "status":
        print("\n".join(status_lines(led)))
        return 0
    if args.cmd == "next":
        print(_human(compute_next_action(led)))
        return 0
    if args.cmd == "advance":
        act = compute_next_action(led)
        print(_human(act))
        if act["halt"] or act["action"] == "SHIP":
            return 0
        result = dispatch(act, led=led)  # VERIFY runs+records; BUILD/LIVE_ACCEPT raise
        if act["action"] == "VERIFY":
            save_ledger(led, args.ledger)
            print(f"recorded: {result['recorded']}  missing: {result['missing']}")
            print(_human(compute_next_action(led)))
        return 0
    if args.cmd == "verify":
        sdir = led.get("v1_suite_dir")
        if not sdir:
            print("v1_suite_dir not set in status.json — no v1 suite to run yet")
            return 1
        wave = next((w for w in led["waves"] if w["id"] == args.wave), None)
        if wave is None:
            print(f"no wave {args.wave!r}")
            return 1
        unproven = [g["id"] for g in wave["gate_tests"] if g["verdict"] != "pass"]
        result = run_verify(led, args.wave, unproven, sdir)
        save_ledger(led, args.ledger)
        print(f"recorded: {result['recorded']}  missing: {result['missing']}")
        print(_human(compute_next_action(led)))
        return 0

    # mutations
    if args.cmd == "record-test":
        record_test(led, args.wave, args.test_id, args.verdict)
    elif args.cmd == "record-task":
        set_task(led, args.task_id, args.status)
    elif args.cmd == "record-accept":
        record_accept(led, args.criterion_id, args.verdict)
    elif args.cmd == "prereq":
        set_prereq(led, args.pid, args.status)
    elif args.cmd == "ratify":
        ratify(led, args.gate_id)
    save_ledger(led, args.ledger)
    print(_human(compute_next_action(led)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
