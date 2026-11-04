#!/usr/bin/env python3
"""Score the agent on problem set 1: inject each fault, diagnose, compare, reset.

Run on the control-plane Pi with the app and k8s/diag.yaml deployed:

    python3 eval/run_set1.py --rounds 3
    python3 eval/run_set1.py --rounds 1 --faults selector,port     # quick check
    python3 eval/run_set1.py --kubectl "sudo k3s kubectl"

Each trial:  inject -> wait --settle s -> diagnose (timed) -> reset -> wait until healthy.
"none" trials (no fault) measure false alarms; "netpol" is a fault OUTSIDE set 1
that the agent should call "other".

Output: eval/results/<timestamp>/trials.jsonl and a confusion matrix.
"""

import argparse
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXPECTED = {
    "none": "healthy",
    "selector": "selector_mismatch",
    "scale0": "no_ready_endpoints",
    "readiness": "no_ready_endpoints",
    "port": "port_mismatch",
    "wrong-name": "wrong_service_name",
    "coredns": "dns_coredns_down",
    "netpol": "other",
}
LABEL_ORDER = ["healthy", "dns_coredns_down", "wrong_service_name", "selector_mismatch",
               "no_ready_endpoints", "port_mismatch", "other", "ERROR"]


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def fault(env, *args):
    p = subprocess.run(["bash", str(REPO / "faults" / "fault.sh"), *args], env=env,
                       capture_output=True, text=True, timeout=900)
    if p.returncode != 0:
        raise RuntimeError(f"fault.sh {' '.join(args)} failed: {p.stderr.strip() or p.stdout.strip()}")


def diagnose(kubectl):
    start = time.monotonic()
    p = subprocess.run([sys.executable, str(REPO / "agent" / "diagnose.py"), "--json", "--kubectl", kubectl],
                       capture_output=True, text=True, timeout=300)
    elapsed = round(time.monotonic() - start, 2)
    try:
        return json.loads(p.stdout), elapsed
    except ValueError:
        return {"label": "ERROR", "summary": (p.stderr or p.stdout).strip()[-300:]}, elapsed


def wait_healthy(kubectl, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r, _ = diagnose(kubectl)
        if r.get("label") == "healthy":
            return True
        time.sleep(5)
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kubectl", default="kubectl")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--faults", default=",".join(EXPECTED), help="comma-separated subset")
    ap.add_argument("--settle", type=float, default=20, help="seconds between inject and diagnose")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    faults = [f.strip() for f in args.faults.split(",") if f.strip()]
    unknown = [f for f in faults if f not in EXPECTED]
    if unknown:
        sys.exit(f"unknown fault(s): {unknown}; choose from {list(EXPECTED)}")
    rng = random.Random(args.seed)
    env = {**os.environ, "KUBECTL": args.kubectl}

    outdir = REPO / "eval" / "results" / time.strftime("%Y%m%d-%H%M%S")
    outdir.mkdir(parents=True)
    trials_file = outdir / "trials.jsonl"

    log("reset + wait for a healthy starting point")
    fault(env, "reset")
    if not wait_healthy(args.kubectl, 300):
        sys.exit("cluster is not healthy after reset; run agent/diagnose.py to see why")

    trials = []
    for rnd in range(1, args.rounds + 1):
        order = faults[:]
        rng.shuffle(order)
        for f in order:
            log(f"round {rnd}: {f}")
            injected_at = time.time()
            try:
                if f != "none":
                    fault(env, "inject", f)
                time.sleep(args.settle)
                result, took = diagnose(args.kubectl)
            except Exception as exc:  # keep going; record the failure
                result, took = {"label": "ERROR", "summary": str(exc)}, None
            finally:
                fault(env, "reset")
            recovered = wait_healthy(args.kubectl, 300)
            trial = {"round": rnd, "fault": f, "expected": EXPECTED[f], "predicted": result.get("label"),
                     "correct": result.get("label") == EXPECTED[f], "confidence": result.get("confidence"),
                     "diagnosis_s": took, "injected_at": injected_at, "recovered": recovered,
                     "summary": result.get("summary"), "result": result}
            trials.append(trial)
            with open(trials_file, "a", encoding="utf-8") as out:
                out.write(json.dumps(trial) + "\n")
            log(f"  expected {trial['expected']:<20} got {trial['predicted']:<20} "
                f"{'OK' if trial['correct'] else 'WRONG'}  ({took} s)")
            if not recovered:
                sys.exit("cluster did not recover after reset; stopping (see trials.jsonl)")

    report(trials, outdir)


def report(trials, outdir):
    labels = [l for l in LABEL_ORDER if any(t["expected"] == l or t["predicted"] == l for t in trials)]
    short = {l: l[:10] for l in labels}
    lines = [f"\nConfusion matrix (rows = expected, columns = predicted), {len(trials)} trials\n",
             f"{'':<20}" + "".join(f"{short[l]:>11}" for l in labels)]
    for exp in labels:
        row = [sum(1 for t in trials if t["expected"] == exp and t["predicted"] == pred) for pred in labels]
        if any(row) or any(t["expected"] == exp for t in trials):
            lines.append(f"{exp:<20}" + "".join(f"{(n or '.'):>11}" for n in row))
    correct = sum(t["correct"] for t in trials)
    times = [t["diagnosis_s"] for t in trials if t["diagnosis_s"] is not None]
    lines.append(f"\naccuracy: {correct}/{len(trials)} = {100 * correct / len(trials):.1f}%")
    if times:
        lines.append(f"diagnosis time: mean {sum(times) / len(times):.1f} s, max {max(times):.1f} s")
    lines.append("\nper fault:")
    for f in dict.fromkeys(t["fault"] for t in trials):
        ft = [t for t in trials if t["fault"] == f]
        lines.append(f"  {f:<12} {sum(t['correct'] for t in ft)}/{len(ft)}")
    wrong = [t for t in trials if not t["correct"]]
    if wrong:
        lines.append("\nmisses:")
        for t in wrong:
            lines.append(f"  round {t['round']} {t['fault']}: got {t['predicted']} - {t['summary']}")
    text = "\n".join(lines)
    print(text)
    (outdir / "report.txt").write_text(text + "\n", encoding="utf-8")
    print(f"\nsaved to {outdir}")


if __name__ == "__main__":
    main()
