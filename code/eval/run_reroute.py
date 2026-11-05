#!/usr/bin/env python3
"""Score agent/reroute.py: inject each fault, time how long the app is down, check it fails back.

Start the agent first, in another terminal:   python3 agent/reroute.py
Then:   python3 eval/run_reroute.py --url http://<DB-node-ip>:30080/ --rounds 3

Run it a second time with the agent STOPPED (--label no-agent) to get the "Kubernetes alone"
numbers to compare against.

Per trial:
  1. wait until the cluster is in its normal state: app answers, no reroute taints,
     backend on its role=backend node
  2. inject the fault (faults/fault.sh) and poll the app URL every second
  3. outcome: rerouted (agent moved the backend, app OK again) | escalated (agent alerted, did
     nothing) | no_impact (app never failed, no action) | not_recovered | false_action ('none' trials)
  4. reset the fault, then wait for the failback (taints gone, backend back on its node)

`none` trials inject nothing: any eviction there is a false alarm.
Results: eval/results/reroute-<label>-<timestamp>/ (trials.jsonl, report.txt)
"""

import argparse
import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agent"))
from diagnose import Kube  # noqa: E402
from reroute import TAINT_KEY  # noqa: E402

DEFAULT_FAULTS = ["route", "mtu", "delay", "loss", "fw-vxlan", "none"]


def ok(url):
    try:
        with urllib.request.urlopen(url, timeout=4) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


def fault_sh(kubectl, *args):
    env = dict(os.environ, KUBECTL=kubectl)
    p = subprocess.run(["bash", str(REPO / "faults" / "fault.sh"), *args], env=env,
                       capture_output=True, text=True, timeout=600)
    return p.returncode, (p.stdout + p.stderr).strip()


def normal_state(k, ns):
    """(is_normal, backend_node)"""
    nodes = k.items("get", "nodes")
    if any(t.get("key") == TAINT_KEY for n in nodes for t in n["spec"].get("taints") or []):
        return False, None
    preferred = {n["metadata"]["name"] for n in nodes if n["metadata"].get("labels", {}).get("role") == "backend"}
    pods = [p for p in k.items("-n", ns, "get", "pods", "-l", "app=backend")
            if not p["metadata"].get("deletionTimestamp")]
    where = {p["spec"].get("nodeName") for p in pods}
    return bool(pods) and where <= preferred, ",".join(sorted(w for w in where if w))


def backend_nodes(k, ns):
    pods = [p for p in k.items("-n", ns, "get", "pods", "-l", "app=backend")
            if not p["metadata"].get("deletionTimestamp")]
    return ",".join(sorted({p["spec"].get("nodeName") or "?" for p in pods}))


def agent_events(log_path, since):
    out = []
    try:
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("ts", 0) >= since:
                    out.append(e)
    except FileNotFoundError:
        pass
    return out


def wait_until(cond, timeout, step=3):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return False


def trial(a, k, fault):
    rec = {"fault": fault}
    if not wait_until(lambda: normal_state(k, a.namespace)[0] and ok(a.url), a.settle):
        rec["warning"] = "cluster not back to its normal state before the trial"
    rec["backend_before"] = backend_nodes(k, a.namespace)
    t0 = time.time()
    if fault != "none":
        rc, out = fault_sh(a.kubectl, "inject", fault)
        if rc != 0:
            rec.update(outcome="inject_failed", error=out[-300:])
            return rec
    rec["inject_s"] = round(time.time() - t0, 1)

    first_fail = last_fail = None
    streak, recovered_at = 0, None
    end = t0 + (a.observe if fault == "none" else a.timeout)
    while time.time() < end:
        t = time.time()
        if ok(a.url):
            streak += 1
            if first_fail and streak >= 3 and recovered_at is None:
                recovered_at = last_fail
                if fault != "none":
                    break
        else:
            streak = 0
            first_fail = first_fail or t
            last_fail = t
            recovered_at = None
        time.sleep(max(0.0, 1 - (time.time() - t)))

    ev = agent_events(a.log, t0)
    kinds = [e["event"] for e in ev]
    rec.update(app_failed=first_fail is not None,
               downtime_s=round((recovered_at or time.time()) - first_fail, 1) if first_fail else 0.0,
               recovered=bool(recovered_at) if first_fail else True,
               evictions=kinds.count("evict"), alerts=kinds.count("alert"),
               healed_events=[e.get("recovery_s") for e in ev if e["event"] == "healed"],
               backend_after=backend_nodes(k, a.namespace))
    if fault == "none":
        rec["outcome"] = "false_action" if rec["evictions"] else "correct_no_action"
    elif rec["evictions"] and rec["recovered"]:
        rec["outcome"] = "rerouted"
    elif rec["alerts"] and not rec["evictions"]:
        rec["outcome"] = "escalated"
    elif not rec["app_failed"] and not rec["evictions"]:
        rec["outcome"] = "no_impact"
    else:
        rec["outcome"] = "not_recovered" if not rec["recovered"] else "recovered_without_agent"

    if fault != "none":
        fault_sh(a.kubectl, "reset")
    t1 = time.time()
    rec["failback_ok"] = wait_until(lambda: normal_state(k, a.namespace)[0] and ok(a.url), a.settle)
    rec["failback_s"] = round(time.time() - t1, 1)
    return rec


def report(trials, label):
    lines = [f"reroute evaluation ({label}): {len(trials)} trials", ""]
    by = {}
    for t in trials:
        by.setdefault(t["fault"], []).append(t)
    lines.append(f"{'fault':<10} {'n':>2}  outcomes                               downtime s (mean)  failback ok")
    for f, ts in by.items():
        outs = {}
        for t in ts:
            outs[t["outcome"]] = outs.get(t["outcome"], 0) + 1
        dts = [t["downtime_s"] for t in ts if t.get("app_failed")]
        mean = f"{sum(dts) / len(dts):.1f}" if dts else "-"
        fb = sum(1 for t in ts if t.get("failback_ok"))
        lines.append(f"{f:<10} {len(ts):>2}  {json.dumps(outs):<38} {mean:>17}  {fb}/{len(ts)}")
    false = sum(1 for t in trials if t["outcome"] == "false_action")
    lines += ["", f"false actions in 'none' trials: {false}"]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="app URL through the DB node, e.g. http://<db-node-ip>:30080/")
    ap.add_argument("--kubectl", default="kubectl")
    ap.add_argument("--namespace", default="default")
    ap.add_argument("--faults", default=",".join(DEFAULT_FAULTS))
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=180, help="max seconds to wait for recovery")
    ap.add_argument("--observe", type=int, default=120, help="seconds to watch a 'none' trial")
    ap.add_argument("--settle", type=int, default=300, help="max seconds to wait for the normal state")
    ap.add_argument("--log", default=str(REPO / "reroute_log.jsonl"), help="the agent's log")
    ap.add_argument("--label", default="agent", help="e.g. 'agent' or 'no-agent'")
    a = ap.parse_args()

    k = Kube(a.kubectl)
    plan = [f for f in a.faults.split(",") for _ in range(a.rounds)]
    random.shuffle(plan)
    out_dir = REPO / "eval" / "results" / f"reroute-{a.label}-{time.strftime('%Y%m%d-%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    trials = []
    for i, f in enumerate(plan, 1):
        print(f"[{i}/{len(plan)}] {f} ...", flush=True)
        rec = trial(a, k, f)
        trials.append(rec)
        print("   ", json.dumps(rec), flush=True)
        with open(out_dir / "trials.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    text = report(trials, a.label)
    (out_dir / "report.txt").write_text(text + "\n", encoding="utf-8")
    print("\n" + text + f"\n\nsaved to {out_dir}")


if __name__ == "__main__":
    main()
