# loop-engine

The **phase-driver** for the tradingagents v1 build (and future iterations). It
turns the converged `impl-plan` into a self-driving loop: read the ledger →
compute the single next step → halt at human-ratify gates → never fake green.

This is the **engine** component of the loop-engineering plan
(`vault: Projects/personal/tradingagents/research/research-loop-engineering-engine.md`).
Rails (OAuth/vault/`--emit-trace`), the ratchet (live-bug → fixture), and the
outer loop are separate, later builds.

## Model

- **`status.json`** — the ledger. A machine-readable mirror of the impl-plan §0
  wave-close table + Tier-A/Tier-B exit criteria. SSOT of build *position* (the
  impl-plan stays SSOT of the *spec*).
- **`engine.py`** — a stateless step-function. Re-invoke it to advance; there is
  no long-running process (the two human-ratify gates can sit for days).

### The phase graph (`compute_next_action`)

```
W0 → W1 → W2 → W3 → W4 ──RG1(ratify)──→ Tier-B live-accept ──RG2(ratify)──→ SHIP
per wave:  prereq-block? → HALT     ·  a wave is GREEN only when EVERY gate_test == pass
           tasks pending? → BUILD   ·  one failing/unproven test ⇒ never advance
           tasks done?    → VERIFY     (negative-control teeth)
```

Actions: `BUILD` (dispatch to a fix engine), `VERIFY` (run a wave's gate tests),
`LIVE_ACCEPT` (drive the live harness), `HALT` (ratify gate or external prereq —
engine stops, surfaces to human), `SHIP`.

## Drive it

```bash
python3 engine.py next        # what's the next step?
python3 engine.py status      # per-wave task/gate counts
python3 engine.py validate    # schema + consistency check

# update the ledger as work lands / CI reports:
python3 engine.py record-test W0 test_edgar_vendor pass
python3 engine.py record-task T0.2 done
python3 engine.py record-accept EC-A1 pass   # Tier-B live-acceptance criterion
python3 engine.py prereq P2 done
python3 engine.py ratify RG1
python3 engine.py advance     # dispatch next action (stubbed until wired)
```

## Honest stubs (what's NOT wired yet)

`dispatch()` refuses `BUILD`/`VERIFY`/`LIVE_ACCEPT` with `NotImplementedError`
because the v1 product does not exist yet — there is nothing to run, so the
engine will not invent a verdict. Drive the ledger with the `record-*` commands
meanwhile. Wiring order:

1. **`VERIFY` → pytest** — run a wave's `gate_tests`, `record-test` each verdict.
2. **`BUILD` → auto-debug / feature-dev** — hand the task off, capture done.
3. **`LIVE_ACCEPT` → `eval/acceptance/`** — the AI live-acceptance harness.
4. **Cadence wrapper** — a cron routine for OAuth refresh + market-calendar live
   runs; a thin Workflow/ralph-loop wrapper to auto-advance between
   auto-passable phases (still halting at RG1/RG2).

## Tests

`python3 -m pytest -q` (in this dir) — 22 tests cover the phase graph, the
negative-control close rule, schema validation, and the seeded ledger.
