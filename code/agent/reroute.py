#!/usr/bin/env python3
"""Path-aware rescheduling: keep the backend on a node that can really reach the database.

Kubernetes places pods by CPU, memory, labels and taints. It never checks whether the
network path a pod needs works. If a node stays Ready but its path to the DB breaks,
the kubelet restarts the backend in place (still broken) and the scheduler never moves it.
This agent adds the missing input, "which nodes have a working path to the DB", and uses
only standard Kubernetes actions (taint, delete pod, rollout restart). It does not try to
find or repair the cause; diagnose.py / heal.py do that.

    python3 agent/reroute.py --once          # one probe round, print the table, change nothing
    python3 agent/reroute.py --dry-run       # full loop, but only log what it would do
    python3 agent/reroute.py                 # run for real (Ctrl+C to stop)
    python3 agent/reroute.py --kubectl "sudo k3s kubectl"

The rules (each one is a fixed number, see README section 8 for the reasoning):

 1. PROBE    every 5 s, from the diag-pod on every node, test exactly what the backend does:
             DNS lookup of db-service (must give its ClusterIP), TCP connect to the ClusterIP:5432,
             10 pings to the DB pod (loss, RTT), 3 DF pings of 1400 bytes (MTU).
             A round fails on: DNS wrong, TCP fail, loss >= 30%, RTT avg > 100 ms, big packets lost.
             If the probe itself can't run (exec error), the round is UNKNOWN and changes nothing.
 2. DECIDE   a node becomes BAD after 3 failed rounds in a row, GOOD again after 12 passed rounds
             in a row (hysteresis). If it went BAD 3+ times in 10 min it is "flapping" and needs
             120 passed rounds (10 min) instead.
 3. SAFETY   do nothing (alert) if no node is GOOD, if the DB pod isn't Running+Ready (that is not
             a network problem), or if no GOOD node is allowed to run the backend.
 4. ACT      taint every BAD node  kuberselfheal.io/no-db-path=true:NoSchedule, then delete ONE
             backend pod on a BAD node per round (its Deployment recreates it on a GOOD node).
             At most one eviction per node every 120 s.
 5. VERIFY   within 60 s every backend pod must be Ready on a GOOD node and the app must answer
             HTTP 200. Otherwise: alert and stop evicting until every node is GOOD again.
 6. FAILBACK when a tainted node is GOOD again, remove its taint. When no node is BAD and the
             backend was moved, `rollout restart` it once so it spreads back to its usual node.

Every decision is appended to reroute_log.jsonl.
"""

import argparse
import concurrent.futures
import datetime
import json
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diagnose import Kube, is_ready  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "baseline"))
from collect import parse_ping  # noqa: E402  (same ping parser as the baseline)

DIAG_NS = "kuberselfheal"
TAINT_KEY = "kuberselfheal.io/no-db-path"
REROUTED_ANNOTATION = "kuberselfheal.io/rerouted-at"

# ---- fixed thresholds (the whole policy is these numbers) ----
PERIOD_S = 5            # one probe round every 5 s
FAIL_ROUNDS = 3         # BAD after 3 failed rounds in a row (~15 s)
PASS_ROUNDS = 12        # GOOD again after 12 passed rounds in a row (~60 s)
FLAP_WINDOW_S = 600     # flapping = went BAD FLAP_COUNT times within 10 min ...
FLAP_COUNT = 3
HOLD_ROUNDS = 120       # ... then it needs 120 passed rounds (~10 min) to be GOOD again
PINGS = 10              # pings per round to the DB pod
LOSS_BAD_PCT = 30       # >= 3 of 10 lost (healthy hotspot: ~0-1 of 10)
RTT_BAD_MS = 100        # avg RTT; baseline pod->pod across nodes was 12.7 ms
BIG_SIZE = 1372         # ICMP payload -> 1400-byte IP packet, below the 1450 pod MTU
EVICT_COOLDOWN_S = 120  # at most one eviction per node every 2 min
VERIFY_S = 60           # an eviction must give a working app within 60 s
FAILBACK_COOLDOWN_S = 120


def now_iso():
    return datetime.datetime.now().strftime("%H:%M:%S")


class Log:
    def __init__(self, path):
        self.path = path

        self._seen = {}                   # (event, msg) -> last time printed

    def __call__(self, event, msg, **fields):
        if event in ("alert", "skip"):    # the same alert at most once a minute
            t = time.time()
            if t - self._seen.get((event, msg), -1e9) < 60:
                return
            self._seen[(event, msg)] = t
        print(f"{now_iso()}  {event.upper():<9} {msg}", flush=True)
        rec = {"ts": round(time.time(), 3), "event": event, "msg": msg, **fields}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")


# ---------------------------------------------------------------- probing

def probe_script(dns_name, cluster_ip, port, db_ip):
    return (
        f'a=$(dig +short +time=1 +tries=2 {dns_name} 2>/dev/null | tail -1); echo "DNS=${{a:-none}}"; '
        f"if nc -z -w 2 {cluster_ip} {port} >/dev/null 2>&1; then echo TCP=ok; else echo TCP=fail; fi; "
        f"echo '## ping'; ping -c {PINGS} -i 0.1 -W 1 {db_ip} 2>&1; echo '## end'; "
        f"if ping -M do -c 3 -i 0.2 -W 1 -s {BIG_SIZE} {db_ip} >/dev/null 2>&1; "
        f"then echo BIG=ok; else echo BIG=fail; fi"
    )


def judge(out, cluster_ip):
    """Turn the probe output into ('pass'|'fail'|'unknown', reasons, measurements)."""
    if "DNS=" not in out or "BIG=" not in out:
        return "unknown", ["probe did not run: " + " ".join(out.split())[:100]], {}
    lines = {l.split("=", 1)[0]: l.split("=", 1)[1].strip() for l in out.splitlines()
             if "=" in l and l.split("=", 1)[0] in ("DNS", "TCP", "BIG")}
    ping_txt = out.split("## ping", 1)[-1].split("## end", 1)[0]
    ping = parse_ping(ping_txt)
    m = {"dns": lines.get("DNS"), "tcp": lines.get("TCP"), "big": lines.get("BIG"),
         "loss_pct": ping.get("loss_pct"), "rtt_ms": ping.get("rtt_avg_ms")}
    reasons = []
    if m["dns"] != cluster_ip:
        reasons.append(f"DNS gave {m['dns']} (want {cluster_ip})")
    if m["tcp"] != "ok":
        reasons.append("TCP to DB port failed")
    if m["loss_pct"] is None:
        reasons.append("ping gave no result")
    elif m["loss_pct"] >= LOSS_BAD_PCT:
        reasons.append(f"loss {m['loss_pct']:.0f}%")
    if m["rtt_ms"] is not None and m["rtt_ms"] > RTT_BAD_MS:
        reasons.append(f"RTT {m['rtt_ms']:.0f} ms")
    if m["big"] != "ok":
        reasons.append(f"{BIG_SIZE + 28}-byte packets lost (MTU)")
    return ("fail" if reasons else "pass"), reasons, m


# ---------------------------------------------------------------- per-node state

class NodeState:
    def __init__(self, bad=False):
        self.state = "BAD" if bad else "GOOD"
        self.fails = self.passes = 0
        self.went_bad = deque()          # timestamps of GOOD -> BAD transitions
        self.hold = False                # True: this BAD period needs HOLD_ROUNDS to clear
        self.last_evict = 0.0
        self.last = ("unknown", [], {})

    def flapping(self, t):
        while self.went_bad and t - self.went_bad[0] > FLAP_WINDOW_S:
            self.went_bad.popleft()
        return len(self.went_bad) >= FLAP_COUNT

    def update(self, result, t):
        """Feed one round; return the new state if it changed, else None."""
        self.last = result
        verdict = result[0]
        if verdict == "unknown":
            return None                   # no evidence either way: counters untouched
        if verdict == "fail":
            self.fails, self.passes = self.fails + 1, 0
            if self.state == "GOOD" and self.fails >= FAIL_ROUNDS:
                self.state = "BAD"
                self.went_bad.append(t)
                # decided once, when it goes BAD, so the hold can't expire half-way through
                self.hold = self.flapping(t)
                return "BAD"
        else:
            self.passes, self.fails = self.passes + 1, 0
            need = HOLD_ROUNDS if self.hold else PASS_ROUNDS
            if self.state == "BAD" and self.passes >= need:
                self.state = "GOOD"
                return "GOOD"
        return None


# ---------------------------------------------------------------- scheduling rules

def _expr_ok(expr, labels):
    key, op, vals = expr["key"], expr["operator"], expr.get("values", [])
    if op == "In":
        return labels.get(key) in vals
    if op == "NotIn":
        return labels.get(key) not in vals
    if op == "Exists":
        return key in labels
    if op == "DoesNotExist":
        return key not in labels
    if op in ("Gt", "Lt"):
        try:
            v, ref = int(labels[key]), int(vals[0])
        except (KeyError, ValueError, IndexError):
            return False
        return v > ref if op == "Gt" else v < ref
    return False


def _tolerated(taint, tolerations):
    for tol in tolerations or []:
        if tol.get("effect") and tol["effect"] != taint.get("effect"):
            continue
        if tol.get("operator") == "Exists" and not tol.get("key"):
            return True
        if tol.get("key") != taint.get("key"):
            continue
        if tol.get("operator") == "Exists" or tol.get("value") == taint.get("value"):
            return True
    return False


def backend_allowed_on(pod_spec, node, ignore_our_taint=True):
    """Could the scheduler put a pod with this spec on this node (labels, affinity, taints)?"""
    labels = node["metadata"].get("labels", {})
    if node["spec"].get("unschedulable"):
        return False
    if any(labels.get(k) != v for k, v in (pod_spec.get("nodeSelector") or {}).items()):
        return False
    req = (((pod_spec.get("affinity") or {}).get("nodeAffinity") or {})
           .get("requiredDuringSchedulingIgnoredDuringExecution") or {})
    terms = req.get("nodeSelectorTerms") or []
    if terms and not any(all(_expr_ok(e, labels) for e in t.get("matchExpressions", [])) for t in terms):
        return False
    for taint in node["spec"].get("taints") or []:
        if taint.get("effect") not in ("NoSchedule", "NoExecute"):
            continue
        if ignore_our_taint and taint.get("key") == TAINT_KEY:
            continue
        if not _tolerated(taint, pod_spec.get("tolerations")):
            return False
    return True


def has_our_taint(node):
    return any(t.get("key") == TAINT_KEY for t in node["spec"].get("taints") or [])


def node_ready(node):
    return any(c["type"] == "Ready" and c["status"] == "True" for c in node["status"].get("conditions", []))


def node_ip(node):
    return next((a["address"] for a in node["status"]["addresses"] if a["type"] == "InternalIP"), None)


def http_ok(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


# ---------------------------------------------------------------- the agent

class Rerouter:
    def __init__(self, args):
        self.a = args
        self.k = Kube(args.kubectl)
        self.log = Log(args.log)
        self.nodes = {}                   # name -> NodeState
        self.pending = None               # {"pod", "from", "deadline", "t0"} after an eviction
        self.escalated = False
        self.last_failback = 0.0

    def kc(self, *args):
        """Run a changing kubectl command (or just log it in dry-run). True on success."""
        if self.a.dry_run:
            self.log("dry_run", "would run: kubectl " + " ".join(args))
            return True
        rc, out, err = self.k.run(*args)
        if rc != 0:
            self.log("error", f"kubectl {' '.join(args)} failed: {(err or out).strip()[:200]}")
        return rc == 0

    # --- read the cluster ---

    def snapshot(self):
        ns = self.a.namespace
        nodes = {n["metadata"]["name"]: n for n in self.k.items("get", "nodes")}
        db = [p for p in self.k.items("-n", ns, "get", "pods", "-l", self.a.db_selector)
              if not p["metadata"].get("deletionTimestamp")]
        svc = self.k.get("-n", ns, "get", "svc", self.a.db_service)
        deploy = self.k.get("-n", ns, "get", "deploy", self.a.backend_deploy)
        if not deploy:
            raise RuntimeError(f"deployment {self.a.backend_deploy} not found")
        sel = ",".join(f"{k}={v}" for k, v in deploy["spec"]["selector"]["matchLabels"].items())
        backends = [p for p in self.k.items("-n", ns, "get", "pods", "-l", sel)
                    if not p["metadata"].get("deletionTimestamp")]
        diag = {p["spec"].get("nodeName"): p for p in self.k.items("-n", DIAG_NS, "get", "pods", "-l", "app=diag-pod")
                if p["status"].get("phase") == "Running" and not p["metadata"].get("deletionTimestamp")}
        app_svc = self.k.get("-n", ns, "get", "svc", self.a.backend_service)
        node_port = next((p.get("nodePort") for p in (app_svc or {}).get("spec", {}).get("ports", [])
                          if p.get("nodePort")), None)
        return {"nodes": nodes, "db": db, "svc": svc, "deploy": deploy, "backends": backends,
                "diag": diag, "node_port": node_port}

    def probe_all(self, snap):
        db = snap["db"][0]
        svc = snap["svc"]
        port = svc["spec"]["ports"][0]["port"]
        script = probe_script(f"{self.a.db_service}.{self.a.namespace}.svc.cluster.local",
                              svc["spec"]["clusterIP"], port, db["status"]["podIP"])

        def one(name):
            pod = snap["diag"].get(name)
            if not pod:
                return name, ("unknown", ["no running diag-pod on this node"], {})
            if not node_ready(snap["nodes"][name]):
                return name, ("unknown", ["node NotReady: Kubernetes handles this"], {})
            rc, out, err = self.k.sh(DIAG_NS, pod["metadata"]["name"], script, timeout=25)
            return name, judge(out + err, svc["spec"]["clusterIP"])

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(snap["nodes"]))) as ex:
            return dict(ex.map(one, list(snap["nodes"])))

    # --- one round ---

    def round(self):
        t = time.time()
        snap = self.snapshot()
        db_node = snap["db"][0]["spec"].get("nodeName") if snap["db"] else None

        # Safety: a missing / unready DB is not a network problem.
        if not snap["db"] or not is_ready(snap["db"][0]) or not snap["svc"]:
            self.log("skip", "DB pod not Running+Ready (or no DB Service): not a path problem, no action")
            return

        for name, node in snap["nodes"].items():   # first sight: nodes we tainted earlier start BAD
            if name not in self.nodes:
                self.nodes[name] = NodeState(bad=has_our_taint(node))

        results = self.probe_all(snap)
        for name, res in results.items():
            st = self.nodes[name]
            change = st.update(res, t)
            if change == "BAD":
                self.log("state", f"{name} -> BAD (path to DB on {db_node}): {'; '.join(res[1])}",
                         node=name, state="BAD", reasons=res[1], m=res[2], flapping=st.flapping(t))
            elif change == "GOOD":
                self.log("state", f"{name} -> GOOD after {st.passes} clean rounds", node=name, state="GOOD")

        if self.a.verbose:
            self.print_table(results, db_node)

        good = [n for n, s in self.nodes.items() if s.state == "GOOD" and n in snap["nodes"]]
        bad = [n for n, s in self.nodes.items() if s.state == "BAD" and n in snap["nodes"]]

        # Rule 3: nothing can be localized or fixed if every node fails.
        if not good:
            self.log("alert", "every node fails the path-to-DB test: cluster-wide problem "
                              "(Wi-Fi, DNS, DB, or the probes). No action; a human must look.",
                     results={n: r[1] for n, r in results.items()})
            return

        if bad:
            self.log_pattern(bad, db_node)

        # Rule 4a: taint BAD nodes, untaint GOOD ones.
        for name, node in snap["nodes"].items():
            st = self.nodes[name]
            if st.state == "BAD" and not has_our_taint(node):
                if self.kc("taint", "nodes", name, f"{TAINT_KEY}=true:NoSchedule", "--overwrite"):
                    self.log("taint", f"tainted {name}: no new pods there until its path to the DB is back",
                             node=name)
            elif st.state == "GOOD" and has_our_taint(node):
                if self.kc("taint", "nodes", name, f"{TAINT_KEY}:NoSchedule-"):
                    self.log("untaint", f"removed taint from {name} (path to DB healthy for "
                                        f"{st.passes} rounds)", node=name)

        # Rule 5: verify the last eviction.
        if self.pending:
            self.verify(snap, t)

        # Rule 4b: move at most one backend pod per round, off a BAD node.
        if not self.pending and not self.escalated:
            self.maybe_evict(snap, bad, good, t)

        # Rule 6: failback once nothing is BAD.
        if not bad and not self.pending:
            self.escalated = False
            self.maybe_failback(snap, t)

    def log_pattern(self, bad, db_node):
        """Where is the fault? For the log/report only: actions never depend on this."""
        others = [n for n in self.nodes if n != db_node]
        if db_node in bad:
            where = f"the DB's own node {db_node} fails: likely DNS or something local to {db_node}"
        elif set(others) <= set(bad):
            where = f"every node except {db_node} fails: fault at {db_node}'s side of the overlay"
        else:
            where = f"only {', '.join(sorted(bad))} fail(s): fault on that node's side or its link to {db_node}"
        if getattr(self, "_last_where", None) != where:
            self._last_where = where
            self.log("locate", where, bad=sorted(bad), db_node=db_node)

    def maybe_evict(self, snap, bad, good, t):
        spec = snap["deploy"]["spec"]["template"]["spec"]
        candidates = [n for n in good if node_ready(snap["nodes"][n])
                      and backend_allowed_on(spec, snap["nodes"][n])]
        victims = [p for p in snap["backends"] if p["spec"].get("nodeName") in bad]
        if not victims:
            return
        if not candidates:
            self.log("alert", f"backend pods on BAD nodes {sorted(bad)} but no GOOD node is allowed to run "
                              f"the backend (GOOD: {sorted(good)}). No action; a human must look.")
            return
        for pod in victims:
            node = pod["spec"]["nodeName"]
            st = self.nodes[node]
            if t - st.last_evict < EVICT_COOLDOWN_S:
                continue
            name = pod["metadata"]["name"]
            st.last_evict = t
            self.kc("-n", self.a.namespace, "annotate", "deploy", self.a.backend_deploy,
                    f"{REROUTED_ANNOTATION}={int(t)}", "--overwrite")
            if self.kc("-n", self.a.namespace, "delete", "pod", name, "--wait=false"):
                self.log("evict", f"deleted {name} on {node}; its Deployment recreates it on a GOOD node "
                                  f"(allowed: {', '.join(sorted(candidates))})",
                         pod=name, node=node, candidates=sorted(candidates))
                if not self.a.dry_run:
                    self.pending = {"pod": name, "from": node, "t0": t, "deadline": t + VERIFY_S}
            return                         # one pod per round

    def verify(self, snap, t):
        p = self.pending
        good = {n for n, s in self.nodes.items() if s.state == "GOOD"}
        pods = snap["backends"]
        placed_ok = pods and all(is_ready(x) and x["spec"].get("nodeName") in good for x in pods)
        app_ok = False
        if placed_ok and snap["node_port"]:
            ip = node_ip(snap["nodes"][pods[0]["spec"]["nodeName"]])
            app_ok = http_ok(f"http://{ip}:{snap['node_port']}/")
        if placed_ok and app_ok:
            where = ", ".join(sorted({x["spec"]["nodeName"] for x in pods}))
            self.log("healed", f"backend now on {where}, app answers HTTP 200, "
                               f"{t - p['t0']:.0f} s after the eviction", recovery_s=round(t - p["t0"], 1),
                     moved_from=p["from"], now_on=where)
            self.pending = None
        elif t > p["deadline"]:
            self.log("alert", f"eviction of {p['pod']} did not give a working app within {VERIFY_S} s "
                              "(check the backend image is on every allowed node). Stopping evictions "
                              "until every node is GOOD; a human must look.",
                     placed_ok=bool(placed_ok), app_ok=app_ok)
            self.pending = None
            self.escalated = True

    def maybe_failback(self, snap, t):
        ann = snap["deploy"]["metadata"].get("annotations", {})
        if REROUTED_ANNOTATION not in ann or t - self.last_failback < FAILBACK_COOLDOWN_S:
            return
        if any(has_our_taint(n) for n in snap["nodes"].values()):
            return                         # taints go first; the restart follows on the next round
        self.last_failback = t
        if self.kc("-n", self.a.namespace, "rollout", "restart", f"deploy/{self.a.backend_deploy}"):
            self.kc("-n", self.a.namespace, "annotate", "deploy", self.a.backend_deploy, f"{REROUTED_ANNOTATION}-")
            self.log("failback", "every node's path to the DB is healthy again: rollout restart so the "
                                 "backend returns to its preferred node")

    # --- output ---

    def print_table(self, results, db_node):
        print(f"\n{'node':<10} {'state':<6} {'round':<8} {'dns':<6} {'tcp':<5} {'loss':>6} {'rtt ms':>8} "
              f"{'big':<5}  reasons   (DB on {db_node})")
        for name in sorted(results):
            v, reasons, m = results[name]
            st = self.nodes.get(name)
            dns = "ok" if m.get("dns") and not any(r.startswith("DNS") for r in reasons) else ("-" if not m else "FAIL")
            loss = "-" if m.get("loss_pct") is None else f"{m['loss_pct']:.0f}%"
            rtt = "-" if m.get("rtt_ms") is None else f"{m['rtt_ms']:.1f}"
            print(f"{name:<10} {st.state if st else '-':<6} {v:<8} {dns:<6} {m.get('tcp', '-'):<5} {loss:>6} "
                  f"{rtt:>8} {m.get('big', '-'):<5}  {'; '.join(reasons)}")
        print(flush=True)

    def run(self):
        mode = "DRY RUN" if self.a.dry_run else "LIVE"
        self.log("start", f"reroute agent {mode}: period {PERIOD_S}s, BAD after {FAIL_ROUNDS} fails, "
                          f"GOOD after {PASS_ROUNDS} passes")
        while True:
            t0 = time.time()
            try:
                self.round()
            except Exception as exc:       # never crash the loop; a failed round changes nothing
                self.log("error", f"round failed, no action: {exc}")
            time.sleep(max(0.0, PERIOD_S - (time.time() - t0)))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kubectl", default="kubectl")
    ap.add_argument("--namespace", default="default")
    ap.add_argument("--backend-deploy", default="backend")
    ap.add_argument("--backend-service", default="backend-service")
    ap.add_argument("--db-service", default="db-service")
    ap.add_argument("--db-selector", default="app=database")
    ap.add_argument("--log", default=str(REPO / "reroute_log.jsonl"))
    ap.add_argument("--dry-run", action="store_true", help="probe and decide, but change nothing")
    ap.add_argument("--once", action="store_true", help="one probe round, print the table, change nothing")
    ap.add_argument("-v", "--verbose", action="store_true", help="print the probe table every round")
    args = ap.parse_args()

    r = Rerouter(args)
    if args.once:
        snap = r.snapshot()
        if not snap["db"] or not snap["svc"]:
            sys.exit("no DB pod / DB Service found")
        for name in snap["nodes"]:
            r.nodes[name] = NodeState(bad=has_our_taint(snap["nodes"][name]))
        r.print_table(r.probe_all(snap), snap["db"][0]["spec"].get("nodeName"))
        return
    try:
        r.run()
    except KeyboardInterrupt:
        r.log("stop", "stopped by user (taints and annotations are left as they are)")


if __name__ == "__main__":
    main()
