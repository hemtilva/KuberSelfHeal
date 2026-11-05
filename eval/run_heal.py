#!/usr/bin/env python3
"""Score self-healing on problem set 1: inject -> heal -> did the app come back on its own?

    python3 eval/run_heal.py --rounds 3
    python3 eval/run_heal.py --rounds 1 --faults port,coredns

Each trial: inject -> wait --settle s -> agent/heal.py (timed) -> record the outcome
-> fault.sh reset anyway (safety net, so a failed heal can't poison the next trial)
-> wait until healthy.

Expected outcome per fault:
    set-1/2 faults -> healed     (the agent fixed it and the app answers again)
    netpol       -> escalated   (outside set 1 / maybe intentional: must NOT be touched)
    none         -> healthy     (nothing to do: must NOT change anything)

"was_healthy_before_reset" double-checks a heal independently of the healer's own
report: the evaluator runs a fresh diagnosis before resetting.
"""

import argparse
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_set1 import diagnose, fault, log, wait_healthy  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
EXPECTED = {"none": "healthy", "selector": "healed", "scale0": "healed", "readiness": "healed",
            "port": "healed", "wrong-name": "healed", "coredns": "healed", "netpol": "escalated",
            # set 2: every injected network fault has a configuration cause, so all are healable
            "fw-vxlan": "healed", "mtu": "healed", "route": "healed", "loss": "healed", "delay": "healed"}
SETS = {"1": ["none", "selector", "scale0", "readiness", "port", "wrong-name", "coredns", "netpol"],
        "2": ["none", "fw-vxlan", "mtu", "route", "loss", "delay"]}
SETS["all"] = SETS["1"] + SETS["2"][1:]


def heal(kubectl):
    start = time.monotonic()
    p = subprocess.run([sys.executable, str(REPO / "agent" / "heal.py"), "--json", "--kubectl", kubectl],
                       capture_output=True, text=True, timeout=900)
    took = round(time.monotonic() - start, 1)
    try:
        return json.loads(p.stdout), took
    except ValueError:
        return {"outcome": "ERROR", "reason": (p.stderr or p.stdout).strip()[-300:], "steps": []}, took


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kubectl", default="kubectl")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--set", default="1", choices=list(SETS))
    ap.add_argument("--faults", default=None, help="comma-separated subset (overrides --set)")
    ap.add_argument("--settle", type=float, default=20)
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    faults = [f.strip() for f in args.faults.split(",") if f.strip()] if args.faults else SETS[args.set]
    if unknown := [f for f in faults if f not in EXPECTED]:
        sys.exit(f"unknown fault(s): {unknown}; choose from {list(EXPECTED)}")
    rng = random.Random(args.seed)
    env = {**os.environ, "KUBECTL": args.kubectl}
    outdir = REPO / "eval" / "results" / ("heal-" + (f"set{args.set}-" if args.set != "1" else "")
                                          + time.strftime("%Y%m%d-%H%M%S"))
    outdir.mkdir(parents=True)

    log("reset + wait for a healthy starting point")
    fault(env, "reset")
    if not wait_healthy(args.kubectl, 300):
        sys.exit("cluster is not healthy after reset; run agent/diagnose.py to see why")

    trials = []
    try:
        for rnd in range(1, args.rounds + 1):
            order = faults[:]
            rng.shuffle(order)
            for f in order:
                log(f"round {rnd}: {f}")
                try:
                    if f != "none":
                        fault(env, "inject", f)
                    time.sleep(args.settle)
                    rep, took = heal(args.kubectl)
                except Exception as exc:
                    rep, took = {"outcome": "ERROR", "reason": str(exc), "steps": []}, None
                # Independent check before the safety-net reset.
                try:
                    check, _ = diagnose(args.kubectl)
                except Exception as exc:  # incl. subprocess.TimeoutExpired: keep the trial
                    check = {"label": "ERROR", "summary": str(exc)}
                really_ok = check.get("label") == "healthy" and (check.get("symptom") or {}).get("ok", False)
                reset_error = None
                try:
                    fault(env, "reset")
                except Exception as exc:
                    reset_error = str(exc)
                recovered = wait_healthy(args.kubectl, 300)
                outcome = rep.get("outcome")
                trial = {"round": rnd, "fault": f, "expected": EXPECTED[f], "outcome": outcome,
                         "correct": outcome == EXPECTED[f] and (really_ok or EXPECTED[f] == "escalated"),
                         "was_healthy_before_reset": really_ok, "heal_s": took, "mttr_s": rep.get("mttr_s"),
                         "actions": [s.get("command") for s in rep.get("steps", [])],
                         "rolled_back": any(s.get("rolled_back") for s in rep.get("steps", [])),
                         "reason": rep.get("reason"), "recovered": recovered, "reset_error": reset_error,
                         "report": rep}
                trials.append(trial)
                with open(outdir / "trials.jsonl", "a", encoding="utf-8") as out:
                    out.write(json.dumps(trial) + "\n")
                log(f"  expected {trial['expected']:<10} got {str(outcome):<10} "
                    f"{'OK' if trial['correct'] else 'WRONG'}  heal {took} s, mttr {trial['mttr_s']} s  "
                    f"{'; '.join(a for a in trial['actions'] if a)}")
                if not recovered:
                    log("cluster did not recover after reset; stopping early")
                    return
    finally:
        if trials:
            report(trials, outdir)


def _mean(xs):
    return f"{sum(xs) / len(xs):>13.1f}" if xs else f"{'-':>13}"


def report(trials, outdir):
    lines = [f"\nSelf-healing: {len(trials)} trials\n",
             f"{'fault':<12}{'expected':<11}{'correct':>8}{'mean heal s':>13}{'mean mttr s':>13}  actions taken"]
    for f in dict.fromkeys(t["fault"] for t in trials):
        ft = [t for t in trials if t["fault"] == f]
        heal_s = [t["heal_s"] for t in ft if t["heal_s"] is not None]
        mttr = [t["mttr_s"] for t in ft if t["correct"] and t["mttr_s"] is not None]
        acts = sorted({a.split(" -p ")[0] for t in ft for a in t["actions"] if a}) or ["(none)"]
        lines.append(f"{f:<12}{ft[0]['expected']:<11}{sum(t['correct'] for t in ft):>5}/{len(ft):<2}"
                     f"{_mean(heal_s)}{_mean(mttr)}  {acts[0]}")
        for a in acts[1:]:
            lines.append(f"{'':<57}{a}")
    correct = sum(t["correct"] for t in trials)
    healable = [t for t in trials if t["expected"] == "healed"]
    lines += [f"\noverall correct: {correct}/{len(trials)} = {100 * correct / len(trials):.1f}%",
              f"healed (injected faults): {sum(t['correct'] for t in healable)}/{len(healable)}",
              f"wrongly touched (netpol/none with an action): "
              f"{sum(1 for t in trials if t['expected'] != 'healed' and t['actions'])}",
              f"rollbacks: {sum(t['rolled_back'] for t in trials)}"]
    if mttr := [t["mttr_s"] for t in healable if t["correct"] and t["mttr_s"] is not None]:
        lines.append(f"time to recover after the fix: mean {sum(mttr) / len(mttr):.1f} s, max {max(mttr):.1f} s")
    if wrong := [t for t in trials if not t["correct"]]:
        lines.append("\nmisses:")
        for t in wrong:
            lines.append(f"  round {t['round']} {t['fault']}: {t['outcome']} - {t['reason']}")
    text = "\n".join(lines)
    print(text)
    (outdir / "report.txt").write_text(text + "\n", encoding="utf-8")
    print(f"\nsaved to {outdir}")


if __name__ == "__main__":
    main()
