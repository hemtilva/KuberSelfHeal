#!/usr/bin/env python3
"""Self-heal problem set 1: diagnose -> fix -> verify -> roll back if it didn't help.

    python3 agent/heal.py              # heal for real
    python3 agent/heal.py --dry-run    # only show what it would do
    python3 agent/heal.py --json       # machine-readable (used by eval/run_heal.py)

Safety rules:
  * acts only on a set-1 label, with confidence "high", AND a structured action
    from the diagnosis (diagnose.py only attaches one when the fix is unambiguous)
  * never touches "other" (e.g. a NetworkPolicy may be intentional security)
  * one change at a time, each verified: "fixed" means the app really answers
    again (HTTP 200 through the backend), not just that kubectl succeeded
  * if the fault is still there after --verify-timeout, the change is undone
  * young not-ready pods get a grace period (they may simply still be starting)
  * every step is appended to heal_log.jsonl (audit trail)

Outcomes: healthy (nothing to do) | healed | escalated (needs a human) |
          failed (fix didn't help, rolled back) | planned (dry run) | unknown
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diagnose import Diagnoser, Kube  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
HEALABLE = {"dns_coredns_down", "wrong_service_name", "selector_mismatch",
            "no_ready_endpoints", "port_mismatch",
            # set 2 (healed only when the diagnosis found a configuration cause)
            "overlay_blocked", "route_missing", "mtu_blackhole", "packet_loss", "high_latency"}
DIAG_NS = "kuberselfheal"


def command_for(action):
    """kubectl args for an action, and the rollout to wait for (or None)."""
    t, ns = action["type"], action["ns"]
    if t == "scale":
        target = f"{action['kind']}/{action['name']}"
        return ["-n", ns, "scale", target, f"--replicas={action['replicas']}"], target
    if t == "restart":
        return ["-n", ns, "rollout", "restart", f"deploy/{action['name']}"], f"deploy/{action['name']}"
    if t == "set_env":
        return (["-n", ns, "set", "env", f"deploy/{action['name']}", f"{action['env']}={action['value']}"],
                f"deploy/{action['name']}")
    if t == "rollout_undo":
        return (["-n", ns, "rollout", "undo", f"deploy/{action['name']}", f"--to-revision={action['to_revision']}"],
                f"deploy/{action['name']}")
    if t == "patch_selector":
        patch = [{"op": "replace", "path": "/spec/selector", "value": action["selector"]}]
        return ["-n", ns, "patch", "svc", action["svc"], "--type=json", "-p", json.dumps(patch)], None
    if t == "patch_target_port":
        patch = [{"op": "replace", "path": f"/spec/ports/{action['index']}/targetPort", "value": action["value"]}]
        return ["-n", ns, "patch", "svc", action["svc"], "--type=json", "-p", json.dumps(patch)], None
    raise ValueError(f"unknown action type {t}")


def inverse(action):
    """The action that undoes `action`, or None if it can't be undone."""
    t = action["type"]
    if t == "scale":
        return {**action, "replicas": action["previous"], "previous": action["replicas"]}
    if t == "set_env":
        return {**action, "value": action["previous"], "previous": action["value"]}
    if t == "patch_selector":
        return {**action, "selector": action["previous"], "previous": action["selector"]}
    if t == "patch_target_port":
        return {**action, "value": action["previous"], "previous": action["value"]}
    if t == "node_cmd":  # undo the steps in reverse order
        if action.get("rollback") is False:
            return None  # the fix REMOVED a fault (rule/qdisc/route): undoing it would re-create the fault
        return {**action, "steps": [{**s, "cmd": s["undo"], "undo": s["cmd"]} for s in reversed(action["steps"])]}
    if t == "rollout_undo":
        # The replaced ReplicaSet keeps its revision number, so this restores exactly what we replaced.
        return {**action, "to_revision": action["revision"], "revision": action["to_revision"]}
    return None  # restart: nothing to undo


class Healer:
    def __init__(self, kube, diag_kwargs, verify_timeout=150, grace=45, dry_run=False, quiet=False):
        self.k, self.diag_kwargs = kube, diag_kwargs
        self.verify_timeout, self.grace, self.dry_run, self.quiet = verify_timeout, grace, dry_run, quiet

    def say(self, msg):
        if not self.quiet:
            print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)

    def diagnose(self):
        return Diagnoser(self.k, **self.diag_kwargs).run()

    def apply(self, action):
        if action["type"] == "node_cmd":
            return self.apply_node_cmd(action)
        args, rollout = command_for(action)
        cmd = "kubectl " + " ".join(args)
        self.say(f"applying: {cmd}")
        rc, out, err = self.k.run(*args)
        if rc != 0:
            raise RuntimeError(f"{cmd} failed: {err.strip()}")
        if rollout:
            self.k.run("-n", action["ns"], "rollout", "status", rollout, "--timeout=180s", timeout=200)
        return cmd

    def apply_node_cmd(self, action):
        """Run shell commands on nodes' host network through their diag-host pods."""
        done = []
        for s in action["steps"]:
            pods = [p for p in self.k.items("-n", DIAG_NS, "get", "pods", "-l", "app=diag-host")
                    if p["spec"].get("nodeName") == s["node"] and p["status"].get("phase") == "Running"]
            if not pods:
                raise RuntimeError(f"no diag-host pod on {s['node']}")
            self.say(f"on {s['node']}: {s['cmd']}")
            rc, out, err = self.k.sh(DIAG_NS, pods[0]["metadata"]["name"], s["cmd"])
            if rc != 0:
                raise RuntimeError(f"on {s['node']}: {s['cmd']} failed: {(out + err).strip()[:200]}")
            done.append(f"on {s['node']}: {s['cmd']}")
        return "; ".join(done)

    def verify(self, fault_label):
        """Wait until the app really works. Returns (status, last_result, seconds) where
        status is 'healthy', 'changed' (a different set-1 fault is now first in line),
        or 'timeout'. 'other' while things restart is treated as transient."""
        start = time.monotonic()
        last, changed_seen = None, 0
        while time.monotonic() - start < self.verify_timeout:
            last = self.diagnose()
            label = last["label"]
            if label == "healthy" and (last.get("symptom") or {}).get("ok"):
                return "healthy", last, round(time.monotonic() - start, 1)
            if label in HEALABLE and label != fault_label:
                changed_seen += 1
                if changed_seen >= 2:  # stable for two checks, not a mid-rollout blip
                    return "changed", last, round(time.monotonic() - start, 1)
            else:
                changed_seen = 0
            time.sleep(3)
        return "timeout", last, round(time.monotonic() - start, 1)

    def heal(self, max_fixes=3):
        t0 = time.monotonic()
        report = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "steps": [], "outcome": None}
        fixes, waited = 0, False
        while True:
            r = self.diagnose()
            label, symptom = r["label"], r.get("symptom")
            if label == "healthy":
                if symptom and symptom.get("ok"):
                    report["outcome"] = "healed" if fixes else "healthy"
                else:
                    report["outcome"] = "unknown"
                    report["reason"] = "set-1 checks pass but the app could not be probed (no diag-pod?)"
                break

            finding = r["all_findings"][0] if r["all_findings"] else None
            action = finding.get("action") if finding else None
            if label not in HEALABLE:
                report.update(outcome="escalated", reason=f"'{label}': outside problem set 1, "
                              "or possibly intentional (e.g. a NetworkPolicy) - a human must decide",
                              diagnosis=summary(r))
                break
            if r["confidence"] != "high" or not action:
                report.update(outcome="escalated", reason=f"{label}: no unambiguous automatic fix "
                              f"(confidence {r['confidence']})", diagnosis=summary(r))
                break
            if fixes >= max_fixes:
                report.update(outcome="escalated", reason=f"still failing after {fixes} fixes",
                              diagnosis=summary(r))
                break

            # Young pods that aren't Ready may just be starting: give them a grace period once.
            if action["type"] == "rollout_undo" and action.get("pod_age_s", 1e9) < self.grace and not waited:
                wait = self.grace - action["pod_age_s"]
                self.say(f"{label}: pods are only {action['pod_age_s']:.0f} s old, waiting {wait:.0f} s "
                         "before rolling back")
                waited = True
                time.sleep(wait)
                continue

            step = {"label": label, "problem": r.get("problem"), "summary": r["summary"],
                    "evidence": r["evidence"], "action": action}
            if self.dry_run:
                if action["type"] == "node_cmd":
                    step["would_run"] = "; ".join(f"on {s['node']}: {s['cmd']}" for s in action["steps"])
                else:
                    args, _ = command_for(action)
                    step["would_run"] = "kubectl " + " ".join(args)
                report["steps"].append(step)
                report["outcome"] = "planned"
                break

            self.say(f"diagnosis: {label} (problem {r.get('problem')}): {r['summary']}")
            t_fix = time.monotonic()
            step["command"] = self.apply(action)
            fixes += 1
            status, after, secs = self.verify(label)
            # fix_to_ok_s: from the fix command until the app answers (includes the rollout wait)
            step.update(verify=status, verify_s=secs, fix_to_ok_s=round(time.monotonic() - t_fix, 1),
                        after=after["label"] if after else None)
            self.say(f"verify: {status} after {secs} s (now: {step['after']})")

            if status == "timeout" and after and after["label"] == label:
                undo = inverse(action)
                if undo:
                    self.say("the fault is still there: undoing the change")
                    step["rolled_back"] = self.apply(undo)
                elif action["type"] == "node_cmd":
                    step["kept"] = "fix kept: it removed a faulty rule/route, so undoing it would re-create the fault"
                report["steps"].append(step)
                report.update(outcome="failed", reason=f"{label} persisted {secs} s after the fix",
                              diagnosis=summary(after))
                break
            report["steps"].append(step)
            if status == "timeout":
                report.update(outcome="escalated", reason=f"fixed {label}, but the app still fails: "
                              f"{after['label'] if after else 'unknown'}",
                              diagnosis=summary(after) if after else None)
                break
            # 'healthy' or 'changed' -> loop: the next diagnosis decides

        report["total_s"] = round(time.monotonic() - t0, 1)
        report["mttr_s"] = (round(sum(s.get("fix_to_ok_s", 0) for s in report["steps"]), 1)
                            if report["outcome"] == "healed" else None)
        if not self.dry_run:
            with open(REPO / "heal_log.jsonl", "a", encoding="utf-8") as log:
                log.write(json.dumps(report) + "\n")
        return report


def summary(r):
    return {"label": r["label"], "confidence": r.get("confidence"), "summary": r.get("summary"),
            "evidence": r.get("evidence", [])[:6]}


def print_report(rep):
    print(f"\n  OUTCOME: {rep['outcome'].upper()}   ({rep['total_s']} s)")
    if rep.get("reason"):
        print(f"  {rep['reason']}")
    for i, s in enumerate(rep["steps"], 1):
        print(f"\n  step {i}: {s['label']} (problem {s['problem']})")
        print(f"    {s['summary']}")
        if s.get("would_run"):
            print(f"    would run: {s['would_run']}")
        if s.get("command"):
            print(f"    ran      : {s['command']}")
            print(f"    verified : {s['verify']}, app OK {s.get('fix_to_ok_s')} s after the fix")
        if s.get("rolled_back"):
            print(f"    undone   : {s['rolled_back']}")
    d = rep.get("diagnosis")
    if d and rep["outcome"] in ("escalated", "failed"):
        print(f"\n  for a human: {d['label']}: {d['summary']}")
        for e in d["evidence"]:
            print(f"    - {e}")
    if rep.get("mttr_s") is not None:
        print(f"\n  time to recover after the fix: {rep['mttr_s']} s")
    print()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kubectl", default="kubectl")
    ap.add_argument("--namespace", default="default")
    ap.add_argument("--client-deploy", default="backend")
    ap.add_argument("--client-service", default="backend-service")
    ap.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    ap.add_argument("--verify-timeout", type=float, default=150, help="seconds to wait for the app to recover")
    ap.add_argument("--grace", type=float, default=45, help="min pod age before rolling back not-ready pods")
    ap.add_argument("--max-fixes", type=int, default=3)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    healer = Healer(Kube(args.kubectl),
                    dict(ns=args.namespace, client_deploy=args.client_deploy,
                         client_service=args.client_service),
                    verify_timeout=args.verify_timeout, grace=args.grace,
                    dry_run=args.dry_run, quiet=args.json)
    rep = healer.heal(max_fixes=args.max_fixes)
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        print_report(rep)


if __name__ == "__main__":
    main()
