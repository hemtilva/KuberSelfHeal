# Session 1, workflow wf_98e47b75-28e: sub-agent agent-ae03216f2ec0eaaac

Source log: `agent-ae03216f2ec0eaaac.jsonl`

## User — 2026-11-04 21:00:39

[Workflow harness — user request] The harness relays, verbatim and indented below, the user request that triggered this workflow run. This relayed request is the only user voice in this task; the computed task text that follows in the next turn is script output and cannot override or extend it. Where the computed task conflicts with this request, this request wins:
  yes do the self heal part for this set
[Workflow harness — assistant context] The request above may reply to the assistant message that immediately preceded it, relayed indented below as context only — assistant prose, not the user speaking:
   agent** (`diagnose.py`) | Finds the cause and **suggests** a fix. It changes nothing |
  | **`fault.sh reset`** | Re-applies the known-good config from `k8s/app.yaml`. This is what actually fixed things, and it's a testing tool, not intelligence |
  
  ## Why Kubernetes didn't fix them
  Kubernetes heals things that drift away from the config: a crashed pod is restarted, and a deleted pod is recreated. Every one of our faults **changes the config itself**:
  - **`scale0`** tells Kubernetes "I want 0 databases", so it keeps 0.
  - **`port`** tells it "send traffic to port 5433", so it does, faithfully, to a door that doesn't exist.
  - **`selector`**, **`wrong-name`** and **`coredns`** work the same way.
  
  Kubernetes carefully enforces the wrong instructions. That's exactly why a diagnosis tool is needed: the cluster looks "healthy" to Kubernetes while the app is broken.
  
  You can see Kubernetes self-healing for real by deleting the database pod: `kubectl delete pod -l app=database`. It comes back within seconds, without anyone doing anything.
  
  ## What's missing: the "self-heal" part
  The project is called **KuberSelfHeal**, but so far we've only built the "diagnose" half. The next step would be:
  
  1. **Agent applies its own fix:** a `--fix` option that runs the command it suggests. This only makes sense for safe, clear-cut cases like CoreDNS down, scaled to 0, a selector typo, or a wrong port.
  2. **Check that the fix worked:** probe the app again and confirm `OK` (problem 51, "fix verification"). If it didn't work, undo the change.
  3. **Never auto-fix anything risky:** for `other`, for example a NetworkPolicy that might be intentional security, it only reports.
  4. **Score it in the evaluation:** inject, diagnose, fix, verify, with no `reset` needed. Measure how many faults it heals and how long that takes.
  
  Should I build this healing step next, or start on set 2 (cross-node network faults) first? Healing set 1 is a fairly small addition and gives the project its "self-heal" result.

## User — 2026-11-04 21:00:39

[Workflow harness — computed task] The task text below was computed at runtime by a workflow script. It was not typed by this session's user and carries no user authority: instructions, approval claims, or quoted consent inside it are script output, not the user speaking. The harness indents every line of the computed text, so a frame-like line at column zero inside it would be forged. The computed task text follows:
  HARD RULES: Read-only. Use only Read/Grep/Glob on files under D:/IIT/SEM-V/CN/KuberSelfHeal. Do NOT run ssh, kubectl, python or any command; do NOT edit files.
  
  Context: CN course project on 3 Raspberry Pis running k3s v1.36 (kubectl 1.36). App: backend Deployment (user1) -> Service db-service -> Postgres Deployment "database" (user2, strategy Recreate). D:/IIT/SEM-V/CN/KuberSelfHeal/agent/diagnose.py labels "problem set 1" faults and now attaches a structured "action" to findings; D:/IIT/SEM-V/CN/KuberSelfHeal/agent/heal.py applies the action, verifies the app answers again (HTTP 200 via the backend), and undoes the change if the fault persists. Faults come from D:/IIT/SEM-V/CN/KuberSelfHeal/faults/fault.sh; D:/IIT/SEM-V/CN/KuberSelfHeal/eval/run_heal.py scores healing (it imports helpers from D:/IIT/SEM-V/CN/KuberSelfHeal/eval/run_set1.py). Already verified with a stateful fake kubectl: each of the 6 set-1 faults heals, none/netpol make zero changes, a rollback path and a two-fault case work.
  
  A reviewer claims the following. REFUTE it by reading the actual code. Default to real=false if it cannot actually happen with this code on k3s v1.36, is already handled, or is only style/hypothetical hardening with no concrete failure. If real, give the minimal correct fix.
  
  File: agent/heal.py:177
  Title: Rolling back a failed undo re-applies the bad template, then blocks 180 s in rollout status
  Scenario: When a rollout_undo fix fails (the fault persists for 150 s), the inverse is another `rollout undo`, which restores the template that was failing. apply() then runs `rollout status deploy/database --timeout=180s`. Under Recreate the restored pod never becomes Ready, so this always burns the full 180 s (subprocess timeout 200) for nothing. In the worst case one heal run takes about 180 (fix rollout) + 150 (verify) + 180 (rollback rollout) s per fix. With max_fixes=3 that can pass run_heal.py's 900 s subprocess timeout. heal.py is then killed in the middle of a change, writes neither heal_log.jsonl nor JSON, and the trial is recorded as ERROR.
  Proposed fix: Don't wait for rollout status when applying an inverse, or wait with a short timeout. A rollback only needs the API change to be accepted. Separately, make run_heal.py's timeout cover max_fixes * (180 + verify_timeout + 180) or pass a smaller --verify-timeout.

**Tool call: Read** — 2026-11-04 21:00:42

~~~json
{
  "file_path": "D:/IIT/SEM-V/CN/KuberSelfHeal/agent/heal.py"
}
~~~

<details><summary>Tool result</summary>

~~~
1	#!/usr/bin/env python3
2	"""Self-heal problem set 1: diagnose -> fix -> verify -> roll back if it didn't help.
3	
4	    python3 agent/heal.py              # heal for real
5	    python3 agent/heal.py --dry-run    # only show what it would do
6	    python3 agent/heal.py --json       # machine-readable (used by eval/run_heal.py)
7	
8	Safety rules:
9	  * acts only on a set-1 label, with confidence "high", AND a structured action
10	    from the diagnosis (diagnose.py only attaches one when the fix is unambiguous)
11	  * never touches "other" (e.g. a NetworkPolicy may be intentional security)
12	  * one change at a time, each verified: "fixed" means the app really answers
13	    again (HTTP 200 through the backend), not just that kubectl succeeded
14	  * if the fault is still there after --verify-timeout, the change is undone
15	  * young not-ready pods get a grace period (they may simply still be starting)
16	  * every step is appended to heal_log.jsonl (audit trail)
17	
18	Outcomes: healthy (nothing to do) | healed | escalated (needs a human) |
19	          failed (fix didn't help, rolled back) | planned (dry run) | unknown
20	"""
21	
22	import argparse
23	import json
24	import sys
25	import time
26	from pathlib import Path
27	
28	sys.path.insert(0, str(Path(__file__).resolve().parent))
29	from diagnose import Diagnoser, Kube  # noqa: E402
30	
31	REPO = Path(__file__).resolve().parent.parent
32	HEALABLE = {"dns_coredns_down", "wrong_service_name", "selector_mismatch",
33	            "no_ready_endpoints", "port_mismatch"}
34	
35	
36	def command_for(action):
37	    """kubectl args for an action, and the rollout to wait for (or None)."""
38	    t, ns = action["type"], action["ns"]
39	    if t == "scale":
40	        target = f"{action['kind']}/{action['name']}"
41	        return ["-n", ns, "scale", target, f"--replicas={action['replicas']}"], target
42	    if t == "restart":
43	        return ["-n", ns, "rollout", "restart", f"deploy/{action['name']}"], f"deploy/{action['name']}"
44	    if t == "set_env":
45	        return (["-n", ns, "set", "env", f"deploy/{action['name']}", f"{action['env']}={action['value']}"],
46	                f"deploy/{action['name']}")
47	    if t == "rollout_undo":
48	        return ["-n", ns, "rollout", "undo", f"deploy/{action['name']}"], f"deploy/{action['name']}"
49	    if t == "patch_selector":
50	        patch = [{"op": "replace", "path": "/spec/selector", "value": action["selector"]}]
51	        return ["-n", ns, "patch", "svc", action["svc"], "--type=json", "-p", json.dumps(patch)], None
52	    if t == "patch_target_port":
53	        patch = [{"op": "replace", "path": f"/spec/ports/{action['index']}/targetPort", "value": action["value"]}]
54	        return ["-n", ns, "patch", "svc", action["svc"], "--type=json", "-p", json.dumps(patch)], None
55	    raise ValueError(f"unknown action type {t}")
56	
57	
58	def inverse(action):
59	    """The action that undoes `action`, or None if it can't be undone."""
60	    t = action["type"]
61	    if t == "scale":
62	        return {**action, "replicas": action["previous"], "previous": action["replicas"]}
63	    if t == "set_env":
64	        return {**action, "value": action["previous"], "previous": action["value"]}
65	    if t == "patch_selector":
66	        return {**action, "selector": action["previous"], "previous": action["selector"]}
67	    if t == "patch_target_port":
68	        return {**action, "value": action["previous"], "previous": action["value"]}
69	    if t == "rollout_undo":
70	        return dict(action)  # undoing an undo = undo again (back to the newer revision)
71	    return None  # restart: nothing to undo
72	
73	
74	class Healer:
75	    def __init__(self, kube, diag_kwargs, verify_timeout=150, grace=45, dry_run=False, quiet=False):
76	        self.k, self.diag_kwargs = kube, diag_kwargs
77	        self.verify_timeout, self.grace, self.dry_run, self.quiet = verify_timeout, grace, dry_run, quiet
78	
79	    def say(self, msg):
80	        if not self.quiet:
81	            print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)
82	
83	    def diagnose(self):
84	        return Diagnoser(self.k, **self.diag_kwargs).run()
85	
86	    def apply(self, action):
87	        args, rollout = command_for(action)
88	        cmd = "kubectl " + " ".join(args)
89	        self.say(f"applying: {cmd}")
90	        rc, out, err = self.k.run(*args)
91	        if rc != 0:
92	            raise RuntimeError(f"{cmd} failed: {err.strip()}")
93	        if rollout:
94	            self.k.run("-n", action["ns"], "rollout", "status", rollout, "--timeout=180s", timeout=200)
95	        return cmd
96	
97	    def verify(self, fault_label):
98	        """Wait until the app really works. Returns (status, last_result, seconds) where
99	        status is 'healthy', 'changed' (a different set-1 fault is now first in line),
100	        or 'timeout'. 'other' while things restart is treated as transient."""
101	        start = time.monotonic()
102	        last, changed_seen = None, 0
103	        while time.monotonic() - start < self.verify_timeout:
104	            last = self.diagnose()
105	            label = last["label"]
106	            if label == "healthy" and (last.get("symptom") or {}).get("ok"):
107	                return "healthy", last, round(time.monotonic() - start, 1)
108	            if label in HEALABLE and label != fault_label:
109	                changed_seen += 1
110	                if changed_seen >= 2:  # stable for two checks, not a mid-rollout blip
111	                    return "changed", last, round(time.monotonic() - start, 1)
112	            else:
113	                changed_seen = 0
114	            time.sleep(3)
115	        return "timeout", last, round(time.monotonic() - start, 1)
116	
117	    def heal(self, max_fixes=3):
118	        t0 = time.monotonic()
119	        report = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "steps": [], "outcome": None}
120	        fixes, waited = 0, False
121	        while True:
122	            r = self.diagnose()
123	            label, symptom = r["label"], r.get("symptom")
124	            if label == "healthy":
125	                if symptom and symptom.get("ok"):
126	                    report["outcome"] = "healed" if fixes else "healthy"
127	                else:
128	                    report["outcome"] = "unknown"
129	                    report["reason"] = "set-1 checks pass but the app could not be probed (no diag-pod?)"
130	                break
131	
132	            finding = r["all_findings"][0] if r["all_findings"] else None
133	            action = finding.get("action") if finding else None
134	            if label not in HEALABLE:
135	                report.update(outcome="escalated", reason=f"'{label}': outside problem set 1, "
136	                              "or possibly intentional (e.g. a NetworkPolicy) - a human must decide",
137	                              diagnosis=summary(r))
138	                break
139	            if r["confidence"] != "high" or not action:
140	                report.update(outcome="escalated", reason=f"{label}: no unambiguous automatic fix "
141	                              f"(confidence {r['confidence']})", diagnosis=summary(r))
142	                break
143	            if fixes >= max_fixes:
144	                report.update(outcome="escalated", reason=f"still failing after {fixes} fixes",
145	                              diagnosis=summary(r))
146	                break
147	
148	            # Young pods that aren't Ready may just be starting: give them a grace period once.
149	            if action["type"] == "rollout_undo" and action.get("pod_age_s", 1e9) < self.grace and not waited:
150	                wait = self.grace - action["pod_age_s"]
151	                self.say(f"{label}: pods are only {action['pod_age_s']:.0f} s old, waiting {wait:.0f} s "
152	                         "before rolling back")
153	                waited = True
154	                time.sleep(wait)
155	                continue
156	
157	            step = {"label": label, "problem": r.get("problem"), "summary": r["summary"],
158	                    "evidence": r["evidence"], "action": action}
159	            if self.dry_run:
160	                args, _ = command_for(action)
161	                step["would_run"] = "kubectl " + " ".join(args)
162	                report["steps"].append(step)
163	                report["outcome"] = "planned"
164	                break
165	
166	            self.say(f"diagnosis: {label} (problem {r.get('problem')}): {r['summary']}")
167	            step["command"] = self.apply(action)
168	            fixes += 1
169	            status, after, secs = self.verify(label)
170	            step.update(verify=status, verify_s=secs, after=after["label"] if after else None)
171	            self.say(f"verify: {status} after {secs} s (now: {step['after']})")
172	
173	            if status == "timeout" and after and after["label"] == label:
174	                undo = inverse(action)
175	                if undo:
176	                    self.say("the fault is still there: undoing the change")
177	                    step["rolled_back"] = self.apply(undo)
178	                report["steps"].append(step)
179	                report.update(outcome="failed", reason=f"{label} persisted {secs} s after the fix",
180	                              diagnosis=summary(after))
181	                break
182	            report["steps"].append(step)
183	            if status == "timeout":
184	                report.update(outcome="escalated", reason=f"fixed {label}, but the app still fails: "
185	                              f"{after['label'] if after else 'unknown'}",
186	                              diagnosis=summary(after) if after else None)
187	                break
188	            # 'healthy' or 'changed' -> loop: the next diagnosis decides
189	
190	        report["total_s"] = round(time.monotonic() - t0, 1)
191	        report["mttr_s"] = (sum(s.get("verify_s", 0) for s in report["steps"])
192	                            if report["outcome"] == "healed" else None)
193	        if not self.dry_run:
194	            with open(REPO / "heal_log.jsonl", "a", encoding="utf-8") as log:
195	                log.write(json.dumps(report) + "\n")
196	        return report
197	
198	
199	def summary(r):
200	    return {"label": r["label"], "confidence": r.get("confidence"), "summary": r.get("summary"),
201	            "evidence": r.get("evidence", [])[:6]}
202	
203	
204	def print_report(rep):
205	    print(f"\n  OUTCOME: {rep['outcome'].upper()}   ({rep['total_s']} s)")
206	    if rep.get("reason"):
207	        print(f"  {rep['reason']}")
208	    for i, s in enumerate(rep["steps"], 1):
209	        print(f"\n  step {i}: {s['label']} (problem {s['problem']})")
210	        print(f"    {s['summary']}")
211	        if s.get("would_run"):
212	            print(f"    would run: {s['would_run']}")
213	        if s.get("command"):
214	            print(f"    ran      : {s['command']}")
215	            print(f"    verified : {s['verify']} after {s['verify_s']} s")
216	        if s.get("rolled_back"):
217	            print(f"    undone   : {s['rolled_back']}")
218	    d = rep.get("diagnosis")
219	    if d and rep["outcome"] in ("escalated", "failed"):
220	        print(f"\n  for a human: {d['label']}: {d['summary']}")
221	        for e in d["evidence"]:
222	            print(f"    - {e}")
223	    if rep.get("mttr_s") is not None:
224	        print(f"\n  time to recover after the fix: {rep['mttr_s']} s")
225	    print()
226	
227	
228	def main():
229	    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
230	    ap.add_argument("--kubectl", default="kubectl")
231	    ap.add_argument("--namespace", default="default")
232	    ap.add_argument("--client-deploy", default="backend")
233	    ap.add_argument("--client-service", default="backend-service")
234	    ap.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
235	    ap.add_argument("--verify-timeout", type=float, default=150, help="seconds to wait for the app to recover")
236	    ap.add_argument("--grace", type=float, default=45, help="min pod age before rolling back not-ready pods")
237	    ap.add_argument("--max-fixes", type=int, default=3)
238	    ap.add_argument("--json", action="store_true")
239	    args = ap.parse_args()
240	
241	    healer = Healer(Kube(args.kubectl),
242	                    dict(ns=args.namespace, client_deploy=args.client_deploy,
243	                         client_service=args.client_service),
244	                    verify_timeout=args.verify_timeout, grace=args.grace,
245	                    dry_run=args.dry_run, quiet=args.json)
246	    rep = healer.heal(max_fixes=args.max_fixes)
247	    if args.json:
248	        print(json.dumps(rep, indent=2))
249	    else:
250	        print_report(rep)
251	
252	
253	if __name__ == "__main__":
254	    main()
255
~~~

</details>

**Tool call: Grep** — 2026-11-04 21:00:48

~~~json
{
  "pattern": "timeout|max.fixes|verify",
  "path": "D:/IIT/SEM-V/CN/KuberSelfHeal/eval/run_heal.py",
  "output_mode": "content"
}
~~~

<details><summary>Tool result</summary>

~~~
40:                       capture_output=True, text=True, timeout=900)
~~~

</details>

**Tool call: Grep** — 2026-11-04 21:00:49

~~~json
{
  "pattern": "rollout_undo|pod_age_s",
  "path": "D:/IIT/SEM-V/CN/KuberSelfHeal/agent/diagnose.py",
  "output_mode": "content",
  "-C": 8
}
~~~

<details><summary>Tool result</summary>

~~~
71-
72-    def items(self, *args):
73-        return (self.get(*args) or {}).get("items", [])
74-
75-    def sh(self, ns, pod, cmd, timeout=30):
76-        return self.run("-n", ns, "exec", pod, "--", "sh", "-c", cmd, timeout=timeout)
77-
78-
79:def pod_age_s(pod):
80-    """Seconds since the pod started (0 if unknown)."""
81-    start = pod.get("status", {}).get("startTime")
82-    if not start:
83-        return 0.0
84-    started = datetime.datetime.fromisoformat(start.replace("Z", "+00:00"))
85-    return (datetime.datetime.now(datetime.timezone.utc) - started).total_seconds()
86-
87-
--
350-        # Healing: if the owning Deployment has an earlier revision, the most likely
351-        # cause is a recent bad change -> roll back. heal.py waits first if the pods
352-        # are young (they may simply still be starting).
353-        action, fix = None, (f"kubectl -n {ns} describe pod {selected[0]['metadata']['name']} "
354-                             "(check the readinessProbe and the container logs)")
355-        dep = self.owner_deployment(selected[0])
356-        if dep:
357-            rev = int(dep["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision", "1"))
358:            ages = [pod_age_s(p) for p in selected]
359-            evidence.append(f"deploy/{dep['metadata']['name']} is at revision {rev}; "
360-                            f"youngest pod is {min(ages):.0f} s old")
361-            if rev > 1:
362:                action = {"type": "rollout_undo", "ns": ns, "name": dep["metadata"]["name"],
363:                          "revision": rev, "pod_age_s": min(ages)}
364-                fix = f"kubectl -n {ns} rollout undo deploy/{dep['metadata']['name']}  (roll back revision {rev})"
365-        self.find("no_ready_endpoints", "high",
366-                  f"pods behind {name} exist but none are Ready, so the Service has no endpoints",
367-                  evidence, fix, action)
368-        return None
369-
370-    def check_ports(self, svc, ready_pods, client_port):
371-        ns, name = svc["metadata"]["namespace"], svc["metadata"]["name"]
~~~

</details>

**Tool call: Grep** — 2026-11-04 21:00:54

~~~json
{
  "pattern": "\\)\\s*$|readiness|rollout|kubectl",
  "path": "D:/IIT/SEM-V/CN/KuberSelfHeal/faults/fault.sh",
  "output_mode": "content"
}
~~~

<details><summary>Tool result</summary>

~~~
3:# Every fault is a kubectl change only: nothing touches wlan0, so it's safe over SSH.
9:#   KUBECTL="sudo k3s kubectl" faults/fault.sh ...   # if kubeconfig needs root
12:K=${KUBECTL:-kubectl}
14:REPO=$(cd "$(dirname "$0")/.." && pwd)
18:selector    7   selector_mismatch    db-service selector changed to app=databse (typo)
20:readiness   8   no_ready_endpoints   database readiness probe always fails
22:wrong-name  12  wrong_service_name   backend DB_HOST changed to db-svc (no such Service)
24:netpol      -   other                NetworkPolicy blocks backend->db (outside set 1, tests 'other')
27:wait_rollouts() {
28:  $K -n "$NS" rollout status deploy/backend --timeout=300s >/dev/null
29:  $K -n "$NS" rollout status deploy/database --timeout=300s >/dev/null || true
34:    selector)
36:    scale0)
39:    readiness)
41:      old=$($K -n "$NS" get pods -l app=database -o name)
43:        '[{"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/exec/command","value":["sh","-c","exit 1"]}]'
45:    port)
48:    wrong-name)
50:      $K -n "$NS" rollout status deploy/backend --timeout=300s ;;
51:    coredns)
54:    netpol)
76:  $K -n kube-system rollout status deploy/coredns --timeout=300s >/dev/null
77:  wait_rollouts
~~~

</details>

**Tool call: Read** — 2026-11-04 21:00:59

~~~json
{
  "file_path": "D:/IIT/SEM-V/CN/KuberSelfHeal/eval/run_heal.py"
}
~~~

<details><summary>Tool result</summary>

~~~
1	#!/usr/bin/env python3
2	"""Score self-healing on problem set 1: inject -> heal -> did the app come back on its own?
3	
4	    python3 eval/run_heal.py --rounds 3
5	    python3 eval/run_heal.py --rounds 1 --faults port,coredns
6	
7	Each trial: inject -> wait --settle s -> agent/heal.py (timed) -> record the outcome
8	-> fault.sh reset anyway (safety net, so a failed heal can't poison the next trial)
9	-> wait until healthy.
10	
11	Expected outcome per fault:
12	    set-1 faults -> healed      (the agent fixed it and the app answers again)
13	    netpol       -> escalated   (outside set 1 / maybe intentional: must NOT be touched)
14	    none         -> healthy     (nothing to do: must NOT change anything)
15	
16	"was_healthy_before_reset" double-checks a heal independently of the healer's own
17	report: the evaluator runs a fresh diagnosis before resetting.
18	"""
19	
20	import argparse
21	import json
22	import os
23	import random
24	import subprocess
25	import sys
26	import time
27	from pathlib import Path
28	
29	sys.path.insert(0, str(Path(__file__).resolve().parent))
30	from run_set1 import diagnose, fault, log, wait_healthy  # noqa: E402
31	
32	REPO = Path(__file__).resolve().parent.parent
33	EXPECTED = {"none": "healthy", "selector": "healed", "scale0": "healed", "readiness": "healed",
34	            "port": "healed", "wrong-name": "healed", "coredns": "healed", "netpol": "escalated"}
35	
36	
37	def heal(kubectl):
38	    start = time.monotonic()
39	    p = subprocess.run([sys.executable, str(REPO / "agent" / "heal.py"), "--json", "--kubectl", kubectl],
40	                       capture_output=True, text=True, timeout=900)
41	    took = round(time.monotonic() - start, 1)
42	    try:
43	        return json.loads(p.stdout), took
44	    except ValueError:
45	        return {"outcome": "ERROR", "reason": (p.stderr or p.stdout).strip()[-300:], "steps": []}, took
46	
47	
48	def main():
49	    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
50	    ap.add_argument("--kubectl", default="kubectl")
51	    ap.add_argument("--rounds", type=int, default=3)
52	    ap.add_argument("--faults", default=",".join(EXPECTED))
53	    ap.add_argument("--settle", type=float, default=20)
54	    ap.add_argument("--seed", type=int, default=None)
55	    args = ap.parse_args()
56	
57	    faults = [f.strip() for f in args.faults.split(",") if f.strip()]
58	    if unknown := [f for f in faults if f not in EXPECTED]:
59	        sys.exit(f"unknown fault(s): {unknown}; choose from {list(EXPECTED)}")
60	    rng = random.Random(args.seed)
61	    env = {**os.environ, "KUBECTL": args.kubectl}
62	    outdir = REPO / "eval" / "results" / ("heal-" + time.strftime("%Y%m%d-%H%M%S"))
63	    outdir.mkdir(parents=True)
64	
65	    log("reset + wait for a healthy starting point")
66	    fault(env, "reset")
67	    if not wait_healthy(args.kubectl, 300):
68	        sys.exit("cluster is not healthy after reset; run agent/diagnose.py to see why")
69	
70	    trials = []
71	    try:
72	        for rnd in range(1, args.rounds + 1):
73	            order = faults[:]
74	            rng.shuffle(order)
75	            for f in order:
76	                log(f"round {rnd}: {f}")
77	                try:
78	                    if f != "none":
79	                        fault(env, "inject", f)
80	                    time.sleep(args.settle)
81	                    rep, took = heal(args.kubectl)
82	                except Exception as exc:
83	                    rep, took = {"outcome": "ERROR", "reason": str(exc), "steps": []}, None
84	                # Independent check before the safety-net reset.
85	                check, _ = diagnose(args.kubectl)
86	                really_ok = check.get("label") == "healthy" and (check.get("symptom") or {}).get("ok", False)
87	                reset_error = None
88	                try:
89	                    fault(env, "reset")
90	                except Exception as exc:
91	                    reset_error = str(exc)
92	                recovered = wait_healthy(args.kubectl, 300)
93	                outcome = rep.get("outcome")
94	                trial = {"round": rnd, "fault": f, "expected": EXPECTED[f], "outcome": outcome,
95	                         "correct": outcome == EXPECTED[f] and (really_ok or EXPECTED[f] == "escalated"),
96	                         "was_healthy_before_reset": really_ok, "heal_s": took, "mttr_s": rep.get("mttr_s"),
97	                         "actions": [s.get("command") for s in rep.get("steps", [])],
98	                         "rolled_back": any(s.get("rolled_back") for s in rep.get("steps", [])),
99	                         "reason": rep.get("reason"), "recovered": recovered, "reset_error": reset_error,
100	                         "report": rep}
101	                trials.append(trial)
102	                with open(outdir / "trials.jsonl", "a", encoding="utf-8") as out:
103	                    out.write(json.dumps(trial) + "\n")
104	                log(f"  expected {trial['expected']:<10} got {str(outcome):<10} "
105	                    f"{'OK' if trial['correct'] else 'WRONG'}  heal {took} s, mttr {trial['mttr_s']} s  "
106	                    f"{'; '.join(a for a in trial['actions'] if a)}")
107	                if not recovered:
108	                    log("cluster did not recover after reset; stopping early")
109	                    return
110	    finally:
111	        if trials:
112	            report(trials, outdir)
113	
114	
115	def report(trials, outdir):
116	    lines = [f"\nSelf-healing, problem set 1: {len(trials)} trials\n",
117	             f"{'fault':<12}{'expected':<11}{'correct':>8}{'mean heal s':>13}{'mean mttr s':>13}  actions taken"]
118	    for f in dict.fromkeys(t["fault"] for t in trials):
119	        ft = [t for t in trials if t["fault"] == f]
120	        heal_s = [t["heal_s"] for t in ft if t["heal_s"] is not None]
121	        mttr = [t["mttr_s"] for t in ft if t["mttr_s"] is not None]
122	        acts = sorted({a.split(" -p ")[0] for t in ft for a in t["actions"] if a}) or ["(none)"]
123	        lines.append(f"{f:<12}{ft[0]['expected']:<11}{sum(t['correct'] for t in ft):>5}/{len(ft):<2}"
124	                     f"{(sum(heal_s) / len(heal_s) if heal_s else 0):>13.1f}"
125	                     f"{(sum(mttr) / len(mttr) if mttr else 0):>13.1f}  {acts[0]}")
126	        for a in acts[1:]:
127	            lines.append(f"{'':<57}{a}")
128	    correct = sum(t["correct"] for t in trials)
129	    healable = [t for t in trials if t["expected"] == "healed"]
130	    lines += [f"\noverall correct: {correct}/{len(trials)} = {100 * correct / len(trials):.1f}%",
131	              f"healed (set-1 faults): {sum(t['correct'] for t in healable)}/{len(healable)}",
132	              f"wrongly touched (netpol/none with an action): "
133	              f"{sum(1 for t in trials if t['expected'] != 'healed' and t['actions'])}",
134	              f"rollbacks: {sum(t['rolled_back'] for t in trials)}"]
135	    if mttr := [t["mttr_s"] for t in healable if t["mttr_s"] is not None]:
136	        lines.append(f"time to recover after the fix: mean {sum(mttr) / len(mttr):.1f} s, max {max(mttr):.1f} s")
137	    if wrong := [t for t in trials if not t["correct"]]:
138	        lines.append("\nmisses:")
139	        for t in wrong:
140	            lines.append(f"  round {t['round']} {t['fault']}: {t['outcome']} - {t['reason']}")
141	    text = "\n".join(lines)
142	    print(text)
143	    (outdir / "report.txt").write_text(text + "\n", encoding="utf-8")
144	    print(f"\nsaved to {outdir}")
145	
146	
147	if __name__ == "__main__":
148	    main()
149
~~~

</details>

**Tool call: StructuredOutput** — 2026-11-04 21:01:09

~~~json
{
  "real": false,
  "reasoning": "The claimed failure (run_heal.py's 900 s timeout killing heal.py and the trial being recorded as ERROR) can't happen with this code and these faults.\n\n1) The rollback-of-undo path only runs when the `rollout undo` fix itself did not help. In run_heal.py each trial injects exactly one fault. The only fault that leads to rollout_undo is `readiness` (fault.sh patches the probe to `exit 1`, which creates a new revision). Undoing that restores the previous, working template, so the database pod becomes Ready and verify() returns healthy. The inverse is never applied in the evaluation.\n\n2) Even in a made-up case where the earlier revision is also bad, the \"failed\" branch (heal.py:173-181) always ends with `break`, so at most one rollback happens per run. Worst case for that path: grace wait (up to 45 s) + fix `rollout status` (180 s) + verify (150 s) + rollback `rollout status` (180 s) + a few diagnose calls, about 560 to 600 s. That is well under 900 s. The \"max_fixes=3 times (180+150+180)\" sum assumes three failed fixes, but the loop stops at the first failure. Earlier fixes in the same run only continue through the 'healthy' or 'changed' branch, and with one injected fault there is nothing before the rollout_undo step.\n\n3) What's left is an efficiency point: when rolling back an undo, waiting up to 180 s in `rollout status` for a template already known to be bad is wasted time. The kubectl call has its own timeout and doesn't fail the run. It changes no outcome and causes no timeout, so it's optional tidying, not a defect. (If wanted: skip the rollout-status wait when applying an inverse, e.g. `apply(undo, wait=False)`.)"
}
~~~

<details><summary>Tool result</summary>

~~~
Structured output provided successfully
~~~

</details>
