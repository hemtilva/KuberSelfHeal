"""Offline test of agent/reroute.py's rules, with a fake cluster (no Pis needed).

    python3 eval/test_reroute_logic.py
"""
import os, sys, tempfile, types
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))
import reroute as R

# --- judge ---
good_ping = "10 packets transmitted, 10 received, 0% packet loss, time 900ms\nrtt min/avg/max/mdev = 9.1/12.7/20.2/2.1 ms"
out = f"DNS=10.43.1.5\nTCP=ok\n## ping\n{good_ping}\n## end\nBIG=ok"
assert R.judge(out, "10.43.1.5")[0] == "pass", R.judge(out, "10.43.1.5")
bad = out.replace("BIG=ok", "BIG=fail").replace("DNS=10.43.1.5", "DNS=none")
v, reasons, m = R.judge(bad, "10.43.1.5"); assert v == "fail" and len(reasons) == 2, reasons
lossy = out.replace("10 received, 0%", "6 received, 40%"); assert R.judge(lossy, "10.43.1.5")[0] == "fail"
assert R.judge("error: unable to upgrade connection", "x")[0] == "unknown"

# --- state machine: 3 fails -> BAD, 12 passes -> GOOD, unknown changes nothing ---
s = R.NodeState(); P, F, U = ("pass", [], {}), ("fail", ["x"], {}), ("unknown", [], {})
assert [s.update(F, 0), s.update(U, 1), s.update(F, 2), s.update(F, 3)] == [None, None, None, "BAD"]
for i in range(11): assert s.update(P, 10 + i) is None
assert s.update(P, 30) == "GOOD"
# flapping: 3 BAD transitions in 10 min -> needs 120 passes
s = R.NodeState(); t = 0
for cycle in range(3):
    for _ in range(3): s.update(F, t); t += 5
    if cycle < 2:
        for _ in range(12): s.update(P, t); t += 5
assert s.state == "BAD" and s.flapping(t)
for _ in range(119): assert s.update(P, t) is None; t += 5
# (window may expire during the hold; either way it must not be GOOD before 12, and is GOOD by 120)
assert s.update(P, t) == "GOOD"

# --- scheduling rules ---
spec = {"affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [
    {"matchExpressions": [{"key": "node-role.kubernetes.io/control-plane", "operator": "DoesNotExist"}]}]}}}}
def node(name, labels=None, taints=None, ready=True, ip="10.0.0.1"):
    return {"metadata": {"name": name, "labels": labels or {}}, "spec": {"taints": taints or []},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
                       "addresses": [{"type": "InternalIP", "address": ip}]}}
assert not R.backend_allowed_on(spec, node("master", {"node-role.kubernetes.io/control-plane": "true"}))
assert R.backend_allowed_on(spec, node("user2", {"role": "database"}, [{"key": R.TAINT_KEY, "effect": "NoSchedule"}]))
assert not R.backend_allowed_on(spec, node("user2", {}, [{"key": "other", "effect": "NoSchedule"}]))
assert R.backend_allowed_on({"tolerations": [{"operator": "Exists"}]}, node("u", {}, [{"key": "other", "effect": "NoSchedule"}]))

# --- full simulated rounds with a fake cluster: route fault on user1 ---
class FakeK:
    def __init__(self): self.calls = []; self.fail = set(); self.backend_on = "user1"
    def items(self, *a): return (self.get(*a) or {}).get("items", [])
    def get(self, *a):
        a = list(a)
        if a[:2] == ["get", "nodes"]:
            return {"items": [node("master", {"node-role.kubernetes.io/control-plane": "true"}, ip="10.0.0.1"),
                              node("user1", {"role": "backend"}, self.taints("user1"), ip="10.0.0.2"),
                              node("user2", {"role": "database"}, self.taints("user2"), ip="10.0.0.3")]}
        if "deploy" in a:
            return {"metadata": {"annotations": dict(self.ann)}, "spec": {"selector": {"matchLabels": {"app": "backend"}},
                    "template": {"spec": spec}}}
        if "svc" in a and "db-service" in a: return {"spec": {"clusterIP": "10.43.1.5", "ports": [{"port": 5432}]}}
        if "svc" in a: return {"spec": {"ports": [{"port": 5000, "nodePort": 30080}]}}
        if "app=database" in a: return {"items": [self.pod("db", "user2")]}
        if "app=backend" in a: return {"items": [self.pod("be-1", self.backend_on)]}
        if "app=diag-pod" in a: return {"items": [self.pod("diag-" + n, n) for n in ("master", "user1", "user2")]}
        raise AssertionError(a)
    def pod(self, name, n):
        return {"metadata": {"name": name}, "spec": {"nodeName": n},
                "status": {"phase": "Running", "podIP": "10.42.2.9", "conditions": [{"type": "Ready", "status": "True"}]}}
    tainted = set(); ann = {}
    def taints(self, n): return [{"key": R.TAINT_KEY, "effect": "NoSchedule"}] if n in self.tainted else []
    def sh(self, ns, pod, cmd, timeout=30):
        n = pod.split("-", 1)[1]
        return (0, bad if n in self.fail else out, "")
    def run(self, *a):
        self.calls.append(" ".join(a))
        if a[0] == "taint":
            (self.tainted.discard if a[-1].endswith("-") else self.tainted.add)(a[2])
        if "annotate" in a:
            if a[-1].endswith("-"): self.ann.clear()
            elif any(x.startswith(R.REROUTED_ANNOTATION + "=") for x in a): self.ann[R.REROUTED_ANNOTATION] = "1"
        if a[:3] == ("-n", "default", "delete"): self.backend_on = "user2"   # scheduler -> only allowed GOOD node
        if "restart" in a: self.backend_on = "user1"
        return 0, "", ""

args = types.SimpleNamespace(kubectl="kubectl", namespace="default", backend_deploy="backend",
    backend_service="backend-service", db_service="db-service", db_selector="app=database",
    log=os.path.join(tempfile.mkdtemp(), "log.jsonl"), dry_run=False, verbose=False)
r = R.Rerouter(args); fk = FakeK(); r.k = fk
R.http_ok = lambda url, timeout=5: url == "http://10.0.0.3:30080/"
clock = [1000.0]; R.time.time = lambda: clock[0]
def tick(n=1):
    for _ in range(n): r.round(); clock[0] += 5

tick(5); assert not fk.calls, fk.calls                # healthy: no action at all
fk.fail = {"user1"}
tick(2); assert not fk.calls                           # 2 failures: still nothing
tick(1)                               # 3rd: taint + evict
assert any(c.startswith("taint nodes user1") for c in fk.calls) and any("delete pod be-1" in c for c in fk.calls)
tick(1); assert r.pending is None, "verify should pass: backend on user2, HTTP ok"
fk.fail = set(); n = len(fk.calls)
tick(11); assert len(fk.calls) == n, fk.calls[n:]      # 11 clean rounds: still tainted
tick(2);                            # 12th: untaint + failback restart
assert any("user1 kuberselfheal.io/no-db-path:NoSchedule-" in c for c in fk.calls[n:]) and any("restart" in c for c in fk.calls[n:])
assert fk.backend_on == "user1" and not fk.ann

# all nodes failing -> no action
fk.calls.clear(); fk.fail = {"master", "user1", "user2"}
tick(6); assert not fk.calls, fk.calls
print("ALL TESTS PASSED")
