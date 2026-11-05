# Session 1, workflow wf_96752730-d19: sub-agent agent-a65a7e79d7e21a15b

Source log: `agent-a65a7e79d7e21a15b.jsonl`

## User — 2026-11-04 21:33:42

[Workflow harness — user request] The harness relays, verbatim and indented below, the user request that triggered this workflow run. This relayed request is the only user voice in this task; the computed task text that follows in the next turn is script output and cannot override or extend it. Where the computed task conflicts with this request, this request wins:
  do set 2
[Workflow harness — assistant context] The request above may reply to the assistant message that immediately preceded it, relayed indented below as context only — assistant prose, not the user speaking:
  `flannel.1` / UDP 8472 only, never on `wlan0`**. That breaks pod traffic between the Pis while SSH (port 22) and the Kubernetes API (6443) keep working, so you can't lock yourself out. Every fault also gets an automatic undo timer.
  
  ## Set 3: node and CNI health
  | # | Problem | Inject | Detect |
  |---|---|---|---|
  | 3 | CNI / Flannel broken on one node | Remove the CNI config or stop flannel on user2 | New pods stuck in `ContainerCreating`, "failed to setup network" in events |
  | 35 | Conntrack table full | Set `nf_conntrack_max` very low and generate load | `table full, dropping packet` in `dmesg`; count near the max |
  | – | Node goes NotReady | Stop `k3s-agent` on a worker | Node condition; pods on it stop answering |
  
  These need node-level access (the `diag-host` pod already has it), and they're riskier to inject.
  
  ## Set 4: policy and security
  | # | Problem | Notes |
  |---|---|---|
  | 16 | NetworkPolicy with wrong selectors | We already detect the block (`other`). Next: explain **which** rule blocks it, and what an allow rule would need to look like |
  | 26 | Intended vs accidental block | Can't be decided automatically. The agent prepares the evidence and a human decides |
  
  ## Set 5: performance over time
  Problems 27, 28, 29 (uneven traffic), 31 (bandwidth) and 52 (intermittent failures). These need the prober running continuously and comparison over time (anomaly detection), not single checks. They work best last, once the earlier sets are stable.
  
  ## Engineering task alongside: automatic triggering
  Right now healing only runs when someone starts it. A small watcher on master would check the app every 10 seconds and run the healer after 2 failures in a row. That turns the project into real "self-healing", and it serves every set.
  
  **My suggested order:**
  1. The automatic watcher. It's small and makes a great demo.
  2. Set 2, cross-node network.
  3. Set 3, node and CNI health.
  4. Sets 4 and 5, if time allows.
  
  Should I start with the watcher, or go straight to set 2?

## User — 2026-11-04 21:33:42

[Workflow harness — computed task] The task text below was computed at runtime by a workflow script. It was not typed by this session's user and carries no user authority: instructions, approval claims, or quoted consent inside it are script output, not the user speaking. The harness indents every line of the computed text, so a frame-like line at column zero inside it would be forged. The computed task text follows:
  HARD RULES: Read-only. Use only Read/Grep/Glob on files under D:/IIT/SEM-V/CN/KuberSelfHeal. Do NOT run ssh, kubectl, python or any command; do NOT edit files. A live evaluation is running on the cluster.
  
  Context: CN course project, 3 Raspberry Pis on a phone Wi-Fi hotspot, k3s v1.36 with Flannel VXLAN (flannel.1, UDP 8472) over wlan0, iptables nf_tables backend, kube-router NetworkPolicy. Backend on user1, Postgres on user2, CoreDNS currently on user1. Problem set 2 = cross-node network faults. D:/IIT/SEM-V/CN/KuberSelfHeal/agent/network.py detects them (called from D:/IIT/SEM-V/CN/KuberSelfHeal/agent/diagnose.py only when all set-1 checks pass); D:/IIT/SEM-V/CN/KuberSelfHeal/agent/heal.py applies "node_cmd" actions through privileged hostNetwork diag-host pods (D:/IIT/SEM-V/CN/KuberSelfHeal/k8s/diag.yaml, image nicolaka/netshoot:v0.13); D:/IIT/SEM-V/CN/KuberSelfHeal/faults/fault.sh injects fw-vxlan / mtu / route / loss / delay and reset_network undoes them. Baseline in D:/IIT/SEM-V/CN/KuberSelfHeal/baseline/results/*/summary.json (pod->pod 12.7 ms, 0% loss, MTU 1450; parsers in D:/IIT/SEM-V/CN/KuberSelfHeal/baseline/collect.py). ALREADY VERIFIED LIVE on the Pis: each of the 5 faults is diagnosed with the right label and healed (app OK again); healthy cluster -> healthy; Flannel does NOT restore a deleted route by itself.
  
  Review HEALING SAFETY for set 2: node_cmd actions built in D:/IIT/SEM-V/CN/KuberSelfHeal/agent/network.py and executed by D:/IIT/SEM-V/CN/KuberSelfHeal/agent/heal.py (apply_node_cmd, inverse, verify). Can it make the network worse or lock the operator out (SSH over wlan0, k3s API 6443)? Consider: deleting a firewall rule that is legitimately someone's (any 8472 DROP is deleted), undo commands that are not the exact inverse (iptables -D vs -I position; tc 'seed' param in the re-add; 'ip route del' undo of a 'replace' that overwrote a different existing route), partial failure mid-way through several steps (no rollback of the steps already applied), the MSS clamp staying forever after the fault is gone, verification declaring 'healthy' while the MTU fault persists (intended: mitigated), and re-running heal when a clamp already exists (duplicate rules). Report only concrete problems.

**Tool call: Read** — 2026-11-04 21:33:44

~~~json
{
  "file_path": "D:/IIT/SEM-V/CN/KuberSelfHeal/agent/network.py"
}
~~~

<details><summary>Tool result</summary>

~~~
1	"""Problem set 2: the cross-node network path, below Kubernetes.
2	
3	Runs only when every set-1 check passed. Compares live measurements with the
4	healthy baseline (baseline/results/*/summary.json) and inspects node network
5	config through the privileged diag-host pods.
6	
7	    backend pod (node A) --cni0--> flannel.1 ==VXLAN UDP 8472 over wlan0==> flannel.1 --cni0--> DB pod (node B)
8	
9	Labels:
10	    overlay_blocked (4)   pods can't reach each other across nodes although the nodes can,
11	                          and a firewall rule drops/rejects VXLAN (UDP 8472)
12	    route_missing   (2)   a node has no route via flannel.1 to the other node's pod network
13	    mtu_blackhole   (5)   full-size packets vanish on the overlay and bulk TCP stalls
14	    packet_loss     (28)  pod-to-pod loss far above the baseline
15	    high_latency    (27)  pod-to-pod RTT far above the baseline
16	
17	Healing actions are "node_cmd": shell commands run on a node's host network through
18	its diag-host pod, each with an exact undo command.
19	"""
20	
21	import json
22	import re
23	import sys
24	from pathlib import Path
25	
26	REPO = Path(__file__).resolve().parent.parent
27	sys.path.insert(0, str(REPO / "baseline"))
28	from collect import parse_iperf, parse_ping  # noqa: E402  (same parsers as the baseline)
29	
30	DIAG_NS = "kuberselfheal"
31	DEFAULT_BASELINE = {"rtt_avg_ms": 15.0, "loss_pct": 0.0, "path_mtu": 1450, "source": "built-in defaults"}
32	LOSS_MIN_PCT = 15          # pod-to-pod loss above max(this, baseline + 10) is a fault
33	LATENCY_EXTRA_MS = 50      # RTT above baseline + this (and > 2x baseline) is a fault
34	
35	
36	def load_baseline():
37	    """Pod-to-pod (cross-node) numbers from the newest recorded baseline."""
38	    runs = sorted((REPO / "baseline" / "results").glob("*/summary.json"))
39	    if not runs:
40	        return dict(DEFAULT_BASELINE)
41	    try:
42	        t = json.loads(runs[-1].read_text(encoding="utf-8"))["tests"]["pod_to_pod_cross"]
43	        return {"rtt_avg_ms": t.get("rtt_avg_ms") or DEFAULT_BASELINE["rtt_avg_ms"],
44	                "loss_pct": t.get("loss_pct") or 0.0,
45	                "path_mtu": t.get("path_mtu") or DEFAULT_BASELINE["path_mtu"],
46	                "source": runs[-1].parent.name}
47	    except (ValueError, KeyError):
48	        return dict(DEFAULT_BASELINE)
49	
50	
51	class NetworkChecks:
52	    def __init__(self, diagnoser):
53	        self.d, self.k = diagnoser, diagnoser.k
54	
55	    # --- helpers ---
56	
57	    def diag(self, app, node):
58	        pods = [p for p in self.k.items("-n", DIAG_NS, "get", "pods", "-l", f"app={app}")
59	                if p["spec"].get("nodeName") == node and p["status"].get("phase") == "Running"]
60	        return pods[0] if pods else None
61	
62	    def on_host(self, node, cmd, timeout=30):
63	        pod = self.diag("diag-host", node)
64	        if not pod:
65	            return None
66	        rc, out, err = self.k.sh(DIAG_NS, pod["metadata"]["name"], cmd, timeout=timeout)
67	        return out + err
68	
69	    def in_pod(self, cmd, timeout=30):
70	        rc, out, err = self.k.sh(DIAG_NS, self.d.diag_pod, cmd, timeout=timeout)
71	        return rc, out + err
72	
73	    def df_ping_ok(self, ip, size):
74	        rc, _ = self.in_pod(f"ping -M do -c 3 -i 0.2 -W 1 -s {size} {ip}", timeout=20)
75	        return rc == 0
76	
77	    # --- main ---
78	
79	    def run(self, client_node, server_pod):
80	        server_node, dst = server_pod["spec"].get("nodeName"), server_pod["status"].get("podIP")
81	        if not client_node or client_node == server_node:
82	            self.d.check("network", None, "backend and database are on the same node: no cross-node path")
83	            return
84	        nodes = {}
85	        for n in self.k.items("get", "nodes"):
86	            addrs = {a["type"]: a["address"] for a in n["status"]["addresses"]}
87	            nodes[n["metadata"]["name"]] = {"ip": addrs.get("InternalIP"), "cidr": n["spec"].get("podCIDR")}
88	        base = load_baseline()
89	        self.ctx = {"client": client_node, "server": server_node, "dst": dst, "nodes": nodes, "base": base}
90	
91	        # 1. Underlay: can the two Pis reach each other at all (over Wi-Fi)?
92	        node_ping = parse_ping(self.on_host(client_node, f"ping -c 5 -i 0.2 -W 1 {nodes[server_node]['ip']}") or "")
93	        self.ctx["node_ping"] = node_ping
94	        if node_ping.get("loss_pct") == 100.0:
95	            self.d.other_evidence.append(f"{client_node} cannot reach {server_node} ({nodes[server_node]['ip']}) "
96	                                         "at all, even outside Kubernetes: the Wi-Fi/underlay is down")
97	            self.d.check("underlay", False, f"node -> node: 100% loss")
98	            return
99	        self.d.check("underlay", True, f"node -> node: {node_ping.get('loss_pct')}% loss, "
100	                                       f"{node_ping.get('rtt_avg_ms')} ms")
101	
102	        # 2. Overlay: pod to pod across the VXLAN tunnel.
103	        _, out = self.in_pod(f"ping -c 30 -i 0.1 -W 1 {dst}", timeout=40)
104	        pod_ping = parse_ping(out)
105	        self.ctx["pod_ping"] = pod_ping
106	        loss, rtt = pod_ping.get("loss_pct"), pod_ping.get("rtt_avg_ms")
107	        if loss is None:
108	            self.d.check("overlay", None, "pod -> pod ping could not run")
109	            return
110	        if loss >= 90:
111	            self.overlay_down()
112	            return
113	        if loss > max(LOSS_MIN_PCT, base["loss_pct"] + 10):
114	            self.degraded("packet_loss", f"pod -> pod loss {loss}% (baseline {base['loss_pct']}%)")
115	            return
116	        if rtt is not None and rtt > base["rtt_avg_ms"] + LATENCY_EXTRA_MS and rtt > 2 * base["rtt_avg_ms"]:
117	            self.degraded("high_latency", f"pod -> pod RTT {rtt} ms (baseline {base['rtt_avg_ms']} ms)")
118	            return
119	        self.d.check("overlay", True, f"pod -> pod: {loss}% loss, {rtt} ms "
120	                                      f"(baseline {base['loss_pct']}%, {base['rtt_avg_ms']} ms)")
121	
122	        # 3. MTU: do full-size packets still fit through the tunnel?
123	        self.check_mtu()
124	
125	    # --- overlay completely down: route or firewall? ---
126	
127	    def overlay_down(self):
128	        c = self.ctx
129	        evidence = [f"node -> node works ({c['node_ping'].get('loss_pct')}% loss), "
130	                    f"but pod -> pod across nodes: {c['pod_ping'].get('loss_pct')}% loss",
131	                    "so the Pis can talk, but the pod tunnel (VXLAN over flannel.1) is broken"]
132	
133	        # a) routes: each node must send the other node's pod network into flannel.1
134	        client_pod_ip = (self.diag("diag-pod", c["client"]) or {}).get("status", {}).get("podIP")
135	        for node, peer, ip in ((c["client"], c["server"], c["dst"]), (c["server"], c["client"], client_pod_ip)):
136	            if not ip:
137	                continue
138	            route = " ".join((self.on_host(node, f"ip route get {ip}") or "").split())
139	            if route and "flannel.1" not in route:
140	                cidr = c["nodes"][peer]["cidr"]
141	                gw = cidr.split("/")[0]
142	                self.d.find("route_missing", "high",
143	                            f"{node} has no route to {peer}'s pod network {cidr} through the tunnel",
144	                            evidence + [f"on {node}: ip route get {ip} -> {route[:120]}",
145	                                        f"expected: {cidr} via {gw} dev flannel.1 onlink (flannel VXLAN route)"],
146	                            f"on {node}: ip route replace {cidr} via {gw} dev flannel.1 onlink",
147	                            {"type": "node_cmd", "steps": [{
148	                                "node": node, "cmd": f"ip route replace {cidr} via {gw} dev flannel.1 onlink",
149	                                "undo": f"ip route del {cidr}"}]})
150	                return
151	        self.d.check("routes", True, "both nodes route the other's pod network via flannel.1")
152	
153	        # b) firewall: anything dropping/rejecting VXLAN (UDP 8472) on either node?
154	        steps, found = [], []
155	        for node in (c["client"], c["server"]):
156	            for line in (self.on_host(node, "iptables -S") or "").splitlines():
157	                if "8472" in line and re.search(r"-j (DROP|REJECT)\b", line) and line.startswith("-A "):
158	                    found.append(f"on {node}: {line}")
159	                    steps.append({"node": node, "cmd": "iptables " + line.replace("-A ", "-D ", 1),
160	                                  "undo": "iptables " + line.replace("-A ", "-I ", 1)})
161	        if steps:
162	            self.d.find("overlay_blocked", "high",
163	                        "a firewall rule drops the VXLAN tunnel (UDP 8472) between the nodes",
164	                        evidence + found, "; ".join(f"on {s['node']}: {s['cmd']}" for s in steps),
165	                        {"type": "node_cmd", "steps": steps})
166	            return
167	        self.d.check("firewall", True, "no rule drops or rejects UDP 8472 on either node")
168	        self.d.other_evidence += evidence + ["routes and firewall look correct: check flannel on both nodes "
169	                                             "(ip -d link show flannel.1, k3s agent logs)"]
170	
171	    # --- loss / latency: shaping rule (config) or physical link? ---
172	
173	    def degraded(self, label, headline):
174	        c = self.ctx
175	        evidence = [headline,
176	                    f"node -> node (outside the tunnel): {c['node_ping'].get('loss_pct')}% loss, "
177	                    f"{c['node_ping'].get('rtt_avg_ms')} ms"]
178	        steps = []
179	        for node in (c["client"], c["server"]):
180	            for line in (self.on_host(node, "tc qdisc show") or "").splitlines():
181	                m = re.search(r"qdisc netem \S+ dev (\S+) root .*?limit \d+\s*(.*)$", line)
182	                if m:
183	                    dev, params = m.group(1), m.group(2).strip()
184	                    evidence.append(f"on {node}: traffic-control rule on {dev}: netem {params}")
185	                    steps.append({"node": node, "cmd": f"tc qdisc del dev {dev} root",
186	                                  "undo": f"tc qdisc add dev {dev} root netem {params}"})
187	        what = "packet loss" if label == "packet_loss" else "latency"
188	        if steps:
189	            self.d.find(label, "high", f"high {what} on the pod path, caused by a traffic-control (netem) rule",
190	                        evidence, "; ".join(f"on {s['node']}: {s['cmd']}" for s in steps),
191	                        {"type": "node_cmd", "steps": steps})
192	        else:
193	            # No configuration cause: on a Wi-Fi hotspot this is physical. Report, don't touch.
194	            self.d.find(label, "high", f"high {what} on the pod path, with no configuration cause found "
195	                                       "(likely the physical/Wi-Fi link)",
196	                        evidence + ["no netem/shaping rule on either node"],
197	                        "check Wi-Fi signal and distance to the hotspot; nothing to change automatically")
198	
199	    # --- MTU blackhole ---
200	
201	    def check_mtu(self):
202	        c = self.ctx
203	        base_mtu = int(c["base"]["path_mtu"])
204	        if self.df_ping_ok(c["dst"], base_mtu - 28):
205	            self.d.check("mtu", True, f"{base_mtu}-byte packets (DF) pass pod -> pod, as in the baseline")
206	            return
207	        lo, hi = 500, base_mtu - 28 - 1
208	        if not self.df_ping_ok(c["dst"], lo):
209	            self.d.check("mtu", None, "even 528-byte DF packets fail: not an MTU problem")
210	            return
211	        while lo < hi:
212	            mid = (lo + hi + 1) // 2
213	            if self.df_ping_ok(c["dst"], mid):
214	                lo = mid
215	            else:
216	                hi = mid - 1
217	        pmtu = lo + 28
218	
219	        # Does it actually hurt? A bulk TCP transfer to the diag pod on the DB's node.
220	        target = (self.diag("diag-pod", c["server"]) or {}).get("status", {}).get("podIP")
221	        tcp = None
222	        if target:
223	            rc, out = self.in_pod(f"timeout 12 iperf3 -c {target} -p 5201 -t 2 -J", timeout=25)
224	            tcp = parse_iperf(out)
225	            if "error" in tcp and ('"connected"' in out or rc in (124, 143)):
226	                # connected, then killed by `timeout` mid-transfer: the data stalled
227	                tcp = {"error": "connected, but the transfer stalled (killed after 12 s)"}
228	            elif "error" in tcp and "busy" in tcp["error"]:
229	                tcp = None  # iperf3 server busy: unknown, don't conclude anything
230	        evidence = [f"path MTU pod -> pod is {pmtu} bytes, baseline {base_mtu}: "
231	                    f"packets of {pmtu + 1}-{base_mtu} bytes silently disappear (no ICMP 'too big')",
232	                    "small packets and pings pass, so the connection looks fine until something large is sent"]
233	        if tcp and "mbps" in tcp and tcp["mbps"] >= 1:
234	            self.d.check("mtu", None, f"path MTU {pmtu} < {base_mtu}, but bulk TCP works "
235	                                      f"({tcp['mbps']} Mbit/s): mitigated (MSS clamping in place)")
236	            return
237	        evidence.append("bulk TCP transfer: " + (f"{tcp.get('mbps')} Mbit/s" if tcp and "mbps" in tcp
238	                                                  else (tcp or {}).get("error", "not tested")))
239	        mss = pmtu - 40
240	        clamp = ("iptables -t mangle {op} FORWARD {dir} flannel.1 -p tcp --tcp-flags SYN,RST SYN "
241	                 "-m comment --comment ksh-heal-mss -j TCPMSS --set-mss {mss}")
242	        steps = [{"node": n, "cmd": clamp.format(op="-I", dir=d, mss=mss), "undo": clamp.format(op="-D", dir=d, mss=mss)}
243	                 for n in (c["client"], c["server"]) for d in ("-o", "-i")]
244	        self.d.find("mtu_blackhole", "high" if tcp else "medium",
245	                    f"MTU blackhole on the pod tunnel: only {pmtu}-byte packets fit, large TCP transfers stall",
246	                    evidence, f"clamp TCP MSS to {mss} on both nodes (mangle FORWARD, flannel.1)",
247	                    {"type": "node_cmd", "steps": steps} if tcp else None)
248
~~~

</details>

**Tool call: Grep** — 2026-11-04 21:33:45

~~~json
{
  "pattern": "node_cmd|def apply_node_cmd|def inverse|def verify|def heal|rollback",
  "path": "D:/IIT/SEM-V/CN/KuberSelfHeal/agent/heal.py",
  "output_mode": "content"
}
~~~

<details><summary>Tool result</summary>

~~~
62:def inverse(action):
73:    if t == "node_cmd":  # undo the steps in reverse order
94:        if action["type"] == "node_cmd":
95:            return self.apply_node_cmd(action)
106:    def apply_node_cmd(self, action):
121:    def verify(self, fault_label):
141:    def heal(self, max_fixes=3):
184:                if action["type"] == "node_cmd":
~~~

</details>

**Tool call: Read** — 2026-11-04 21:33:48

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
33	            "no_ready_endpoints", "port_mismatch",
34	            # set 2 (healed only when the diagnosis found a configuration cause)
35	            "overlay_blocked", "route_missing", "mtu_blackhole", "packet_loss", "high_latency"}
36	DIAG_NS = "kuberselfheal"
37	
38	
39	def command_for(action):
40	    """kubectl args for an action, and the rollout to wait for (or None)."""
41	    t, ns = action["type"], action["ns"]
42	    if t == "scale":
43	        target = f"{action['kind']}/{action['name']}"
44	        return ["-n", ns, "scale", target, f"--replicas={action['replicas']}"], target
45	    if t == "restart":
46	        return ["-n", ns, "rollout", "restart", f"deploy/{action['name']}"], f"deploy/{action['name']}"
47	    if t == "set_env":
48	        return (["-n", ns, "set", "env", f"deploy/{action['name']}", f"{action['env']}={action['value']}"],
49	                f"deploy/{action['name']}")
50	    if t == "rollout_undo":
51	        return (["-n", ns, "rollout", "undo", f"deploy/{action['name']}", f"--to-revision={action['to_revision']}"],
52	                f"deploy/{action['name']}")
53	    if t == "patch_selector":
54	        patch = [{"op": "replace", "path": "/spec/selector", "value": action["selector"]}]
55	        return ["-n", ns, "patch", "svc", action["svc"], "--type=json", "-p", json.dumps(patch)], None
56	    if t == "patch_target_port":
57	        patch = [{"op": "replace", "path": f"/spec/ports/{action['index']}/targetPort", "value": action["value"]}]
58	        return ["-n", ns, "patch", "svc", action["svc"], "--type=json", "-p", json.dumps(patch)], None
59	    raise ValueError(f"unknown action type {t}")
60	
61	
62	def inverse(action):
63	    """The action that undoes `action`, or None if it can't be undone."""
64	    t = action["type"]
65	    if t == "scale":
66	        return {**action, "replicas": action["previous"], "previous": action["replicas"]}
67	    if t == "set_env":
68	        return {**action, "value": action["previous"], "previous": action["value"]}
69	    if t == "patch_selector":
70	        return {**action, "selector": action["previous"], "previous": action["selector"]}
71	    if t == "patch_target_port":
72	        return {**action, "value": action["previous"], "previous": action["value"]}
73	    if t == "node_cmd":  # undo the steps in reverse order
74	        return {**action, "steps": [{**s, "cmd": s["undo"], "undo": s["cmd"]} for s in reversed(action["steps"])]}
75	    if t == "rollout_undo":
76	        # The replaced ReplicaSet keeps its revision number, so this restores exactly what we replaced.
77	        return {**action, "to_revision": action["revision"], "revision": action["to_revision"]}
78	    return None  # restart: nothing to undo
79	
80	
81	class Healer:
82	    def __init__(self, kube, diag_kwargs, verify_timeout=150, grace=45, dry_run=False, quiet=False):
83	        self.k, self.diag_kwargs = kube, diag_kwargs
84	        self.verify_timeout, self.grace, self.dry_run, self.quiet = verify_timeout, grace, dry_run, quiet
85	
86	    def say(self, msg):
87	        if not self.quiet:
88	            print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)
89	
90	    def diagnose(self):
91	        return Diagnoser(self.k, **self.diag_kwargs).run()
92	
93	    def apply(self, action):
94	        if action["type"] == "node_cmd":
95	            return self.apply_node_cmd(action)
96	        args, rollout = command_for(action)
97	        cmd = "kubectl " + " ".join(args)
98	        self.say(f"applying: {cmd}")
99	        rc, out, err = self.k.run(*args)
100	        if rc != 0:
101	            raise RuntimeError(f"{cmd} failed: {err.strip()}")
102	        if rollout:
103	            self.k.run("-n", action["ns"], "rollout", "status", rollout, "--timeout=180s", timeout=200)
104	        return cmd
105	
106	    def apply_node_cmd(self, action):
107	        """Run shell commands on nodes' host network through their diag-host pods."""
108	        done = []
109	        for s in action["steps"]:
110	            pods = [p for p in self.k.items("-n", DIAG_NS, "get", "pods", "-l", "app=diag-host")
111	                    if p["spec"].get("nodeName") == s["node"] and p["status"].get("phase") == "Running"]
112	            if not pods:
113	                raise RuntimeError(f"no diag-host pod on {s['node']}")
114	            self.say(f"on {s['node']}: {s['cmd']}")
115	            rc, out, err = self.k.sh(DIAG_NS, pods[0]["metadata"]["name"], s["cmd"])
116	            if rc != 0:
117	                raise RuntimeError(f"on {s['node']}: {s['cmd']} failed: {(out + err).strip()[:200]}")
118	            done.append(f"on {s['node']}: {s['cmd']}")
119	        return "; ".join(done)
120	
121	    def verify(self, fault_label):
122	        """Wait until the app really works. Returns (status, last_result, seconds) where
123	        status is 'healthy', 'changed' (a different set-1 fault is now first in line),
124	        or 'timeout'. 'other' while things restart is treated as transient."""
125	        start = time.monotonic()
126	        last, changed_seen = None, 0
127	        while time.monotonic() - start < self.verify_timeout:
128	            last = self.diagnose()
129	            label = last["label"]
130	            if label == "healthy" and (last.get("symptom") or {}).get("ok"):
131	                return "healthy", last, round(time.monotonic() - start, 1)
132	            if label in HEALABLE and label != fault_label:
133	                changed_seen += 1
134	                if changed_seen >= 2:  # stable for two checks, not a mid-rollout blip
135	                    return "changed", last, round(time.monotonic() - start, 1)
136	            else:
137	                changed_seen = 0
138	            time.sleep(3)
139	        return "timeout", last, round(time.monotonic() - start, 1)
140	
141	    def heal(self, max_fixes=3):
142	        t0 = time.monotonic()
143	        report = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "steps": [], "outcome": None}
144	        fixes, waited = 0, False
145	        while True:
146	            r = self.diagnose()
147	            label, symptom = r["label"], r.get("symptom")
148	            if label == "healthy":
149	                if symptom and symptom.get("ok"):
150	                    report["outcome"] = "healed" if fixes else "healthy"
151	                else:
152	                    report["outcome"] = "unknown"
153	                    report["reason"] = "set-1 checks pass but the app could not be probed (no diag-pod?)"
154	                break
155	
156	            finding = r["all_findings"][0] if r["all_findings"] else None
157	            action = finding.get("action") if finding else None
158	            if label not in HEALABLE:
159	                report.update(outcome="escalated", reason=f"'{label}': outside problem set 1, "
160	                              "or possibly intentional (e.g. a NetworkPolicy) - a human must decide",
161	                              diagnosis=summary(r))
162	                break
163	            if r["confidence"] != "high" or not action:
164	                report.update(outcome="escalated", reason=f"{label}: no unambiguous automatic fix "
165	                              f"(confidence {r['confidence']})", diagnosis=summary(r))
166	                break
167	            if fixes >= max_fixes:
168	                report.update(outcome="escalated", reason=f"still failing after {fixes} fixes",
169	                              diagnosis=summary(r))
170	                break
171	
172	            # Young pods that aren't Ready may just be starting: give them a grace period once.
173	            if action["type"] == "rollout_undo" and action.get("pod_age_s", 1e9) < self.grace and not waited:
174	                wait = self.grace - action["pod_age_s"]
175	                self.say(f"{label}: pods are only {action['pod_age_s']:.0f} s old, waiting {wait:.0f} s "
176	                         "before rolling back")
177	                waited = True
178	                time.sleep(wait)
179	                continue
180	
181	            step = {"label": label, "problem": r.get("problem"), "summary": r["summary"],
182	                    "evidence": r["evidence"], "action": action}
183	            if self.dry_run:
184	                if action["type"] == "node_cmd":
185	                    step["would_run"] = "; ".join(f"on {s['node']}: {s['cmd']}" for s in action["steps"])
186	                else:
187	                    args, _ = command_for(action)
188	                    step["would_run"] = "kubectl " + " ".join(args)
189	                report["steps"].append(step)
190	                report["outcome"] = "planned"
191	                break
192	
193	            self.say(f"diagnosis: {label} (problem {r.get('problem')}): {r['summary']}")
194	            t_fix = time.monotonic()
195	            step["command"] = self.apply(action)
196	            fixes += 1
197	            status, after, secs = self.verify(label)
198	            # fix_to_ok_s: from the fix command until the app answers (includes the rollout wait)
199	            step.update(verify=status, verify_s=secs, fix_to_ok_s=round(time.monotonic() - t_fix, 1),
200	                        after=after["label"] if after else None)
201	            self.say(f"verify: {status} after {secs} s (now: {step['after']})")
202	
203	            if status == "timeout" and after and after["label"] == label:
204	                undo = inverse(action)
205	                if undo:
206	                    self.say("the fault is still there: undoing the change")
207	                    step["rolled_back"] = self.apply(undo)
208	                report["steps"].append(step)
209	                report.update(outcome="failed", reason=f"{label} persisted {secs} s after the fix",
210	                              diagnosis=summary(after))
211	                break
212	            report["steps"].append(step)
213	            if status == "timeout":
214	                report.update(outcome="escalated", reason=f"fixed {label}, but the app still fails: "
215	                              f"{after['label'] if after else 'unknown'}",
216	                              diagnosis=summary(after) if after else None)
217	                break
218	            # 'healthy' or 'changed' -> loop: the next diagnosis decides
219	
220	        report["total_s"] = round(time.monotonic() - t0, 1)
221	        report["mttr_s"] = (round(sum(s.get("fix_to_ok_s", 0) for s in report["steps"]), 1)
222	                            if report["outcome"] == "healed" else None)
223	        if not self.dry_run:
224	            with open(REPO / "heal_log.jsonl", "a", encoding="utf-8") as log:
225	                log.write(json.dumps(report) + "\n")
226	        return report
227	
228	
229	def summary(r):
230	    return {"label": r["label"], "confidence": r.get("confidence"), "summary": r.get("summary"),
231	            "evidence": r.get("evidence", [])[:6]}
232	
233	
234	def print_report(rep):
235	    print(f"\n  OUTCOME: {rep['outcome'].upper()}   ({rep['total_s']} s)")
236	    if rep.get("reason"):
237	        print(f"  {rep['reason']}")
238	    for i, s in enumerate(rep["steps"], 1):
239	        print(f"\n  step {i}: {s['label']} (problem {s['problem']})")
240	        print(f"    {s['summary']}")
241	        if s.get("would_run"):
242	            print(f"    would run: {s['would_run']}")
243	        if s.get("command"):
244	            print(f"    ran      : {s['command']}")
245	            print(f"    verified : {s['verify']}, app OK {s.get('fix_to_ok_s')} s after the fix")
246	        if s.get("rolled_back"):
247	            print(f"    undone   : {s['rolled_back']}")
248	    d = rep.get("diagnosis")
249	    if d and rep["outcome"] in ("escalated", "failed"):
250	        print(f"\n  for a human: {d['label']}: {d['summary']}")
251	        for e in d["evidence"]:
252	            print(f"    - {e}")
253	    if rep.get("mttr_s") is not None:
254	        print(f"\n  time to recover after the fix: {rep['mttr_s']} s")
255	    print()
256	
257	
258	def main():
259	    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
260	    ap.add_argument("--kubectl", default="kubectl")
261	    ap.add_argument("--namespace", default="default")
262	    ap.add_argument("--client-deploy", default="backend")
263	    ap.add_argument("--client-service", default="backend-service")
264	    ap.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
265	    ap.add_argument("--verify-timeout", type=float, default=150, help="seconds to wait for the app to recover")
266	    ap.add_argument("--grace", type=float, default=45, help="min pod age before rolling back not-ready pods")
267	    ap.add_argument("--max-fixes", type=int, default=3)
268	    ap.add_argument("--json", action="store_true")
269	    args = ap.parse_args()
270	
271	    healer = Healer(Kube(args.kubectl),
272	                    dict(ns=args.namespace, client_deploy=args.client_deploy,
273	                         client_service=args.client_service),
274	                    verify_timeout=args.verify_timeout, grace=args.grace,
275	                    dry_run=args.dry_run, quiet=args.json)
276	    rep = healer.heal(max_fixes=args.max_fixes)
277	    if args.json:
278	        print(json.dumps(rep, indent=2))
279	    else:
280	        print_report(rep)
281	
282	
283	if __name__ == "__main__":
284	    main()
285
~~~

</details>

**Tool call: Read** — 2026-11-04 21:33:50

~~~json
{
  "file_path": "D:/IIT/SEM-V/CN/KuberSelfHeal/faults/fault.sh"
}
~~~

<details><summary>Tool result</summary>

~~~
1	#!/usr/bin/env bash
2	# Inject / undo faults.
3	#   Set 1 (Service discovery & DNS): kubectl changes only.
4	#   Set 2 (cross-node network): node-level changes made through the privileged
5	#     diag-host pod, ONLY on the overlay (flannel.1 / VXLAN UDP 8472), never on
6	#     wlan0, so SSH and the Kubernetes API keep working. Each set-2 fault also
7	#     schedules its own undo after AUTO_REVERT seconds (default 300) as a safety net.
8	#
9	#   faults/fault.sh list
10	#   faults/fault.sh inject <fault>
11	#   faults/fault.sh reset            # undo ANY fault: re-apply k8s/app.yaml, restore CoreDNS
12	#
13	#   KUBECTL="sudo k3s kubectl" faults/fault.sh ...   # if kubeconfig needs root
14	set -euo pipefail
15	
16	K=${KUBECTL:-kubectl}
17	NS=${APP_NAMESPACE:-default}
18	REPO=$(cd "$(dirname "$0")/.." && pwd)
19	
20	# fault | problem # | expected agent label | what it does
21	FAULTS="
22	selector    7   selector_mismatch    db-service selector changed to app=databse (typo)
23	scale0      8   no_ready_endpoints   database scaled to 0 replicas
24	readiness   8   no_ready_endpoints   database readiness probe always fails
25	port        9   port_mismatch        db-service targetPort changed 5432 -> 5433
26	wrong-name  12  wrong_service_name   backend DB_HOST changed to db-svc (no such Service)
27	coredns     14  dns_coredns_down     CoreDNS scaled to 0 replicas
28	netpol      -   other                NetworkPolicy blocks backend->db (outside set 1, tests 'other')
29	fw-vxlan    4   overlay_blocked      [set 2] DB node drops VXLAN (UDP 8472): the pod tunnel is cut
30	mtu         5   mtu_blackhole        [set 2] overlay silently drops packets > 1200 bytes (MTU blackhole)
31	route       2   route_missing        [set 2] backend node loses its route to the DB node's pod network
32	loss        28  packet_loss          [set 2] DB node's overlay drops 40% of packets (tc netem)
33	delay       27  high_latency         [set 2] DB node's overlay adds 150 ms delay (tc netem)
34	"
35	
36	AUTO_REVERT=${AUTO_REVERT:-300}
37	DIAG_NS=kuberselfheal
38	
39	# Run a shell command on a node's host network, through its diag-host pod.
40	on_node() {
41	  local pod
42	  pod=$($K -n $DIAG_NS get pod -l app=diag-host --field-selector "spec.nodeName=$1" -o name | head -1)
43	  [ -n "$pod" ] || { echo "no diag-host pod on node $1 (kubectl apply -f k8s/diag.yaml)" >&2; return 1; }
44	  $K -n $DIAG_NS exec "$pod" -- sh -c "$2"
45	}
46	node_of()  { $K -n "$NS" get pod -l "app=$1" -o jsonpath='{.items[0].spec.nodeName}'; }
47	workers()  { $K get nodes -l '!node-role.kubernetes.io/control-plane' -o jsonpath='{.items[*].metadata.name}'; }
48	pod_cidr() { $K get node "$1" -o jsonpath='{.spec.podCIDR}'; }
49	
50	# Undo commands for set-2 faults (idempotent: safe to run when nothing is there).
51	UNDO_FW='while iptables -D INPUT -p udp --dport 8472 -j DROP 2>/dev/null; do :; done'
52	UNDO_MTU='for d in -i -o; do while iptables -D FORWARD $d flannel.1 -m length --length 1201:65535 -j DROP 2>/dev/null; do :; done; done'
53	UNDO_TC='tc qdisc show dev flannel.1 | grep -q netem && tc qdisc del dev flannel.1 root || true'
54	# The healer's MSS clamp (tagged ksh-heal-mss) is only needed while the MTU fault exists.
55	UNDO_MSS='iptables -t mangle -S FORWARD | grep ksh-heal-mss | sed "s/^-A/-D/" | while read -r r; do eval iptables -t mangle $r; done'
56	
57	# Schedule an automatic undo on the node (safety net if nobody resets).
58	auto_revert() {
59	  on_node "$1" "nohup sh -c 'sleep $AUTO_REVERT; : ksh-revert; $2' >/dev/null 2>&1 &"
60	}
61	
62	wait_rollouts() {
63	  $K -n "$NS" rollout status deploy/backend --timeout=300s >/dev/null
64	  $K -n "$NS" rollout status deploy/database --timeout=300s >/dev/null || true
65	}
66	
67	inject() {
68	  case "$1" in
69	    selector)
70	      $K -n "$NS" patch svc db-service --type=merge -p '{"spec":{"selector":{"app":"databse"}}}' ;;
71	    scale0)
72	      $K -n "$NS" scale deploy/database --replicas=0
73	      $K -n "$NS" wait --for=delete pod -l app=database --timeout=120s || true ;;
74	    readiness)
75	      # Recreate strategy: the old (Ready) pod is deleted, the new one never becomes Ready.
76	      old=$($K -n "$NS" get pods -l app=database -o name)
77	      $K -n "$NS" patch deploy/database --type=json -p \
78	        '[{"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/exec/command","value":["sh","-c","exit 1"]}]'
79	      [ -z "$old" ] || $K -n "$NS" wait --for=delete $old --timeout=180s || true ;;
80	    port)
81	      $K -n "$NS" patch svc db-service --type=json -p \
82	        '[{"op":"replace","path":"/spec/ports/0/targetPort","value":5433}]' ;;
83	    wrong-name)
84	      $K -n "$NS" set env deploy/backend DB_HOST=db-svc
85	      $K -n "$NS" rollout status deploy/backend --timeout=300s ;;
86	    coredns)
87	      $K -n kube-system scale deploy/coredns --replicas=0
88	      $K -n kube-system wait --for=delete pod -l k8s-app=kube-dns --timeout=120s || true ;;
89	    netpol)
90	      $K -n "$NS" apply -f - <<EOF
91	apiVersion: networking.k8s.io/v1
92	kind: NetworkPolicy
93	metadata:
94	  name: ksh-fault-netpol
95	  labels: {kuberselfheal/fault: "true"}
96	spec:
97	  podSelector: {matchLabels: {app: database}}
98	  policyTypes: [Ingress]
99	  ingress:
100	    - from: [{podSelector: {matchLabels: {app: frontend}}}]
101	EOF
102	      ;;
103	    fw-vxlan)
104	      n=$(node_of database)
105	      on_node "$n" "iptables -I INPUT -p udp --dport 8472 -j DROP"
106	      auto_revert "$n" "$UNDO_FW" ;;
107	    mtu)
108	      n=$(node_of database)
109	      on_node "$n" "iptables -I FORWARD -i flannel.1 -m length --length 1201:65535 -j DROP;
110	                    iptables -I FORWARD -o flannel.1 -m length --length 1201:65535 -j DROP"
111	      auto_revert "$n" "$UNDO_MTU" ;;
112	    route)
113	      n=$(node_of backend); cidr=$(pod_cidr "$(node_of database)")
114	      on_node "$n" "ip route del $cidr"
115	      auto_revert "$n" "ip route replace $cidr via ${cidr%/*} dev flannel.1 onlink" ;;
116	    loss)
117	      n=$(node_of database)
118	      on_node "$n" "tc qdisc add dev flannel.1 root netem loss 40%"
119	      auto_revert "$n" "$UNDO_TC" ;;
120	    delay)
121	      n=$(node_of database)
122	      on_node "$n" "tc qdisc add dev flannel.1 root netem delay 150ms"
123	      auto_revert "$n" "$UNDO_TC" ;;
124	    *) echo "unknown fault '$1' (see: $0 list)" >&2; exit 2 ;;
125	  esac
126	}
127	
128	# Undo every set-2 fault (and the healer's MSS clamp) on all workers.
129	reset_network() {
130	  local nodes n m cidr
131	  nodes=$(workers)
132	  for n in $nodes; do
133	    on_node "$n" "pkill -f 'ksh[-]revert' 2>/dev/null; $UNDO_FW; $UNDO_MTU; $UNDO_TC; $UNDO_MSS" || true
134	    for m in $nodes; do   # restore flannel's route to every other worker's pod network
135	      [ "$m" = "$n" ] && continue
136	      cidr=$(pod_cidr "$m")
137	      on_node "$n" "ip route replace $cidr via ${cidr%/*} dev flannel.1 onlink" || true
138	    done
139	  done
140	}
141	
142	reset() {
143	  reset_network
144	  $K -n "$NS" delete networkpolicy -l kuberselfheal/fault=true --ignore-not-found >/dev/null
145	  $K -n kube-system scale deploy/coredns --replicas=1 >/dev/null
146	  $K -n "$NS" apply -f "$REPO/k8s/app.yaml" >/dev/null
147	  $K -n kube-system rollout status deploy/coredns --timeout=300s >/dev/null
148	  wait_rollouts
149	  echo "reset done"
150	}
151	
152	case "${1:-}" in
153	  list)   echo "$FAULTS" | sed '/^$/d' ;;
154	  inject) [ $# -eq 2 ] || { echo "usage: $0 inject <fault>" >&2; exit 2; }
155	          inject "$2"; echo "injected: $2" ;;
156	  reset)  reset ;;
157	  *)      echo "usage: $0 list | inject <fault> | reset" >&2; exit 2 ;;
158	esac
159
~~~

</details>

**Tool call: Grep** — 2026-11-04 21:33:51

~~~json
{
  "pattern": "def sh|def run|def items|exec",
  "path": "D:/IIT/SEM-V/CN/KuberSelfHeal/agent/diagnose.py",
  "output_mode": "content",
  "-A": 8
}
~~~

<details><summary>Tool result</summary>

~~~
63:    def run(self, *args, timeout=60):
64-        try:
65-            p = subprocess.run(self.cmd + list(args), capture_output=True, text=True, timeout=timeout)
66-        except subprocess.TimeoutExpired:
67-            return 124, "", f"timed out after {timeout}s"
68-        return p.returncode, p.stdout, p.stderr
69-
70-    def get(self, *args):
71-        """kubectl get ... -o json; None if the object doesn't exist."""
--
79:    def items(self, *args):
80-        return (self.get(*args) or {}).get("items", [])
81-
82:    def sh(self, ns, pod, cmd, timeout=30):
83:        return self.run("-n", ns, "exec", pod, "--", "sh", "-c", cmd, timeout=timeout)
84-
85-
86-def pod_age_s(pod):
87-    """Seconds since the pod started (0 if unknown)."""
88-    start = pod.get("status", {}).get("startTime")
89-    if not start:
90-        return 0.0
91-    started = datetime.datetime.fromisoformat(start.replace("Z", "+00:00"))
--
166:        """'open' | 'refused' | 'timeout' | None (no diag pod, or kubectl exec itself failed)."""
167-        if not self.diag_pod:
168-            return None
169-        rc, out, err = self.k.sh(DIAG_NS, self.diag_pod, f"nc -zv -w 3 {ip} {port} 2>&1")
170-        if rc == 0:
171-            return "open"
172-        text = (out + err).lower()
173-        if "nc:" not in text and "connect" not in text:
174:            # No output from nc at all: the exec failed (timeout, stream error over the
175-            # hotspot, diag pod restarting). That says nothing about the port.
176-            self.check("active_tests", None, f"TCP test to {ip}:{port} could not run: "
177-                                             + " ".join((out + err).split())[:120])
178-            return None
179-        return "refused" if "refused" in text else "timeout"
180-
181-    def probe_app(self):
182-        """Ask the backend for the DB value, through its ClusterIP (not DNS, which may be broken).
--
185:        into a false alarm. Returns None if curl never ran (exec failed): unknown, not a failure.
186-        """
187-        svc = self.k.get("-n", self.ns, "get", "svc", self.client_service)
188-        if not svc or not self.diag_pod:
189-            return None
190-        ip, port = svc["spec"]["clusterIP"], svc["spec"]["ports"][0]["port"]
191-        for attempt in range(2):
192-            if attempt:
193-                time.sleep(1)
--
485:    def run(self):
486-        start = time.monotonic()
487-        result = {"target": None, "symptom": None}
488-
489-        client = self.k.get("-n", self.ns, "get", "deploy", self.client_deploy)
490-        if not client:
491-            return self.finish(start, result, error=f"Deployment {self.client_deploy} not found in {self.ns}")
492-        env = {e["name"]: e.get("value") for c in client["spec"]["template"]["spec"]["containers"]
493-               for e in c.get("env", [])}
~~~

</details>

**Tool call: Grep** — 2026-11-04 21:35:08

~~~json
{
  "pattern": "except|heal\\(|Healer",
  "path": "D:/IIT/SEM-V/CN/KuberSelfHeal/eval/run_heal.py",
  "output_mode": "content",
  "-n": true
}
~~~

<details><summary>Tool result</summary>

~~~
42:def heal(kubectl):
49:    except ValueError:
88:                    rep, took = heal(args.kubectl)
89:                except Exception as exc:
94:                except Exception as exc:  # incl. subprocess.TimeoutExpired: keep the trial
100:                except Exception as exc:
~~~

</details>

**Tool call: Read** — 2026-11-04 21:35:09

~~~json
{
  "file_path": "D:/IIT/SEM-V/CN/KuberSelfHeal/k8s/diag.yaml"
}
~~~

<details><summary>Tool result</summary>

~~~
1	# Diagnostic toolbox, one copy per node. Used for the baseline now, and later by
2	# the diagnosis agent for active tests (category C) and node-level checks.
3	#
4	#   diag-host : hostNetwork + privileged, so it sees the NODE's network:
5	#               wlan0, routes, iptables, conntrack, flannel.1, dmesg.
6	#   diag-pod  : a normal pod on the pod network, so its traffic goes through
7	#               the CNI / VXLAN overlay exactly like the app's traffic.
8	#
9	# Both run iperf3 servers (host: 5202, pod: 5201) for throughput tests.
10	# Note: nicolaka/netshoot is large (a few hundred MB per node). On a phone
11	# hotspot, the first pull uses mobile data and takes a while.
12	apiVersion: v1
13	kind: Namespace
14	metadata:
15	  name: kuberselfheal
16	---
17	apiVersion: apps/v1
18	kind: DaemonSet
19	metadata:
20	  name: diag-host
21	  namespace: kuberselfheal
22	spec:
23	  selector:
24	    matchLabels:
25	      app: diag-host
26	  template:
27	    metadata:
28	      labels:
29	        app: diag-host
30	    spec:
31	      hostNetwork: true
32	      hostPID: true
33	      dnsPolicy: ClusterFirstWithHostNet
34	      tolerations:
35	        - operator: Exists          # also run on the control-plane Pi
36	      terminationGracePeriodSeconds: 1
37	      containers:
38	        - name: netshoot
39	          image: nicolaka/netshoot:v0.13
40	          command: ["sh", "-c", "iperf3 -s -p 5202 & exec sleep infinity"]
41	          securityContext:
42	            privileged: true
43	          resources:
44	            requests: {cpu: 10m, memory: 32Mi}
45	            limits: {memory: 256Mi}
46	---
47	apiVersion: apps/v1
48	kind: DaemonSet
49	metadata:
50	  name: diag-pod
51	  namespace: kuberselfheal
52	spec:
53	  selector:
54	    matchLabels:
55	      app: diag-pod
56	  template:
57	    metadata:
58	      labels:
59	        app: diag-pod
60	    spec:
61	      tolerations:
62	        - operator: Exists
63	      terminationGracePeriodSeconds: 1
64	      containers:
65	        - name: netshoot
66	          image: nicolaka/netshoot:v0.13
67	          command: ["sh", "-c", "iperf3 -s -p 5201 & exec sleep infinity"]
68	          securityContext:
69	            capabilities:
70	              add: ["NET_RAW", "NET_ADMIN"]
71	          resources:
72	            requests: {cpu: 10m, memory: 32Mi}
73	            limits: {memory: 256Mi}
74
~~~

</details>

**Tool call: StructuredOutput** — 2026-11-04 21:35:53

~~~json
{
  "findings": [
    {
      "file": "agent/network.py",
      "line": 157,
      "title": "Firewall heal deletes any rule containing '8472' + DROP/REJECT, and rollback re-inserts it at the top of the chain",
      "failure_scenario": "The match is a plain substring check ('8472' in line and -j DROP|REJECT). It never checks whether the rule actually matches the VXLAN traffic or where it sits in the chain. Example: a node has the common allow-list pattern '-A INPUT -s <peer-ip> -p udp --dport 8472 -j ACCEPT' followed by '-A INPUT -p udp --dport 8472 -j DROP'. The overlay then goes down for an unrelated reason (flannel stopped, peer flannel.1 broken). Routes look fine, so the code labels the problem overlay_blocked with high confidence and deletes the DROP. That opens VXLAN to every host on the hotspot. Verify times out with the label still overlay_blocked (the real cause is unchanged, and the rule is still listed on the other node or reappears in a re-scan), so the rollback runs 'iptables -I INPUT ...DROP'. -I puts it at position 1, ahead of the ACCEPT, so VXLAN is now dropped from every peer. That stays broken after the real cause is fixed, and there is no auto-revert timer. Same problem with a negated rule like '-A INPUT -p udp ! --dport 8472 -j DROP' (drop all UDP except VXLAN): it gets deleted, and on rollback it lands above the lo/ESTABLISHED/DHCP accepts. On wlan0 that drops DHCP (UDP 67/68) and DNS replies, so the Pi can lose its hotspot lease and SSH/6443 with it. Substring matches like '--dport 18472' or a comment containing 8472 are also picked up.",
      "severity": "high",
      "suggested_fix": "Only act on rules that really drop VXLAN: parse '--dport 8472' exactly, with no '!' before it, and skip rules with -s/-i restrictions that don't cover the peer. Also require the rule's packet counter (iptables -L CHAIN -v -x --line-numbers) to grow during a pod->pod ping. Record each rule's position and build the undo as 'iptables -I CHAIN <pos> ...' so the original order comes back. Better still, don't re-add a DROP rule on rollback at all: removing a DROP can't be what keeps the overlay down, so escalate instead."
    },
    {
      "file": "agent/network.py",
      "line": 146,
      "title": "Route heal undo 'ip route del' deletes a route that existed before (replace is not inverted)",
      "failure_scenario": "route_missing fires whenever the 'ip route get <ip>' output has no 'flannel.1' in it. on_host returns out+err, so a kubectl exec timeout ('timed out after 30s') or a stream error over the hotspot also counts as route_missing. So does a more specific route or policy rule that sends the pod IP somewhere else while the normal '10.42.X.0/24 via 10.42.X.0 dev flannel.1 onlink' route is still there. The fix 'ip route replace' then changes nothing, because the identical route already exists. If verify times out with route_missing still reported (the more specific route still wins, or exec keeps flaking), the rollback runs 'ip route del <cidr>' and removes flannel's real route. It was verified live that Flannel does not restore a deleted route, so this creates a permanent cross-node outage that wasn't there before. The same thing happens when replace overwrote a different pre-existing route for the CIDR: the undo deletes it instead of restoring it.",
      "severity": "high",
      "suggested_fix": "Before proposing the fix, run 'ip route show <cidr>'. If the exact flannel route is already there, don't emit route_missing (look for the more specific route or rule instead). Treat exec failures (rc != 0, or no output that looks like a route) as unknown, not missing. Build the undo from the captured previous route: 'ip route replace <old route>' if there was one, 'ip route del <cidr> via <gw> dev flannel.1' only if there was none, so del can only remove the route the healer added."
    },
    {
      "file": "agent/heal.py",
      "line": 116,
      "title": "Partial failure of a multi-step node_cmd leaves earlier steps applied, unrecorded and never undone",
      "failure_scenario": "apply_node_cmd raises RuntimeError on the first step whose diag-host pod is missing or whose kubectl exec returns non-zero. Over the phone hotspot an exec can return non-zero even though the command ran on the node. heal() calls self.apply(action) without try/except, so the exception escapes heal(). Steps already run are not rolled back, nothing goes into step['command'], and heal_log.jsonl is never written (the write is at the end of heal()). Examples: the MTU clamp has 4 steps over 2 nodes. If node B's diag-host is restarting, node A keeps two TCPMSS rules with no record of them. The firewall fix can delete the DROP on node A and fail on node B, so the deleted rule is gone with no audit trail and no undo. The same happens inside the rollback: apply(undo) raises on the first failing step, for example '-D' of a clamp already removed by reset_network or 'tc qdisc add ... seed N' rejected by the kernel. The rest of the undo steps never run, and the log is lost again.",
      "severity": "medium",
      "suggested_fix": "In apply_node_cmd, wrap each step. On failure, run the undos of the steps already done (in reverse order, best effort) and then raise. Or return the list of done steps so heal() can record and undo them. In heal(), catch exceptions from apply/rollback, record outcome='failed' with the commands that actually ran, and always write heal_log.jsonl (try/finally). Make undo steps idempotent ('iptables -C ... && iptables -D ...', 'tc qdisc del ... 2>/dev/null || true') so one missing rule doesn't stop the rest of the rollback."
    },
    {
      "file": "agent/heal.py",
      "line": 203,
      "title": "Rollback of a removal-type fix re-injects the fault (netem/DROP) with no safety timer",
      "failure_scenario": "For overlay_blocked, packet_loss and high_latency, the 'fix' deletes a DROP rule or a netem qdisc, and the 'undo' re-creates it. Take a netem loss 40% rule on the DB node plus weak Wi-Fi: after the netem is deleted, pod->pod loss is still above 15% because of the physical link, so the label stays packet_loss. Verify times out and the rollback runs 'tc qdisc add dev flannel.1 root netem loss 40%'. The healer has knowingly made the network worse than it was a moment earlier. Outside the eval, a real fault has no fault.sh auto-revert, so the re-added rule stays forever. In the eval, the re-add can happen after the 300 s auto-revert has fired or been killed by reset (pkill ksh-revert), with the same result. The netem undo also uses any device the regex matched: the regex accepts netem on any device, including wlan0 or cni0, which are outside the fault model, so a netem on wlan0 is deleted and then re-added.",
      "severity": "medium",
      "suggested_fix": "Mark node_cmd steps that only remove a harmful rule as 'no rollback' (inverse returns None for them). If the problem persists after the rule is gone, escalate with the evidence instead of re-adding the rule. Limit the netem and firewall search to flannel.1 / UDP 8472, as the fault model says, and leave wlan0 qdiscs alone."
    },
    {
      "file": "agent/network.py",
      "line": 233,
      "title": "MSS clamp is never removed, and 'mitigated' hides the still-present MTU fault (non-TCP traffic still blackholed)",
      "failure_scenario": "After the clamp (TCPMSS --set-mss pmtu-40, tagged ksh-heal-mss) is added, every later diagnosis sees a low path MTU with working bulk TCP and returns check 'mtu' None 'mitigated', so the cluster reports healthy. Only fault.sh reset_network ever removes the clamp. In real operation it stays forever after the underlying MTU problem is gone, and nothing flags that it's stale (check_mtu returns at the first DF ping without looking for ksh-heal-mss rules). Meanwhile the MTU fault itself is still there for anything the clamp doesn't cover: UDP (large DNS responses), ICMP, and node/hostNetwork-originated TCP to pods (OUTPUT chain, not FORWARD). Those still blackhole while the system says healthy. Separately: when iperf3 fails for a non-MTU reason (e.g. 'Connection refused' because the diag-pod iperf server died), tcp is a non-empty error dict. The finding is then high and carries the clamp action, so with a clamp already in place a re-run inserts 4 more duplicate rules before rolling them back.",
      "severity": "low",
      "suggested_fix": "When the DF ping at the baseline size passes, check for ksh-heal-mss rules and report them as stale, or remove them with an audit log entry. Report the mitigated state as a distinct label (e.g. 'mtu_mitigated') rather than healthy, so the leftover fault is still visible. Attach the clamp action only when iperf connected and then stalled. Before inserting, check with 'iptables -t mangle -C' and skip rules that already exist."
    }
  ]
}
~~~

<details><summary>Tool result</summary>

~~~
Structured output provided successfully
~~~

</details>
