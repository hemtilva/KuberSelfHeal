"""Problem set 2: the cross-node network path, below Kubernetes.

Runs only when every set-1 check passed. Compares live measurements with the
healthy baseline (baseline/results/*/summary.json) and inspects node network
config through the privileged diag-host pods.

    backend pod (node A) --cni0--> flannel.1 ==VXLAN UDP 8472 over wlan0==> flannel.1 --cni0--> DB pod (node B)

Labels:
    overlay_blocked (4)   pods can't reach each other across nodes although the nodes can,
                          and a firewall rule drops/rejects VXLAN (UDP 8472)
    route_missing   (2)   a node has no route via flannel.1 to the other node's pod network
    mtu_blackhole   (5)   full-size packets vanish on the overlay and bulk TCP stalls
    packet_loss     (28)  pod-to-pod loss far above the baseline
    high_latency    (27)  pod-to-pod RTT far above the baseline

Healing actions are "node_cmd": shell commands run on a node's host network through
its diag-host pod, each with an exact undo command.
"""

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "baseline"))
from collect import parse_iperf, parse_ping  # noqa: E402  (same parsers as the baseline)

DIAG_NS = "kuberselfheal"
DEFAULT_BASELINE = {"rtt_avg_ms": 15.0, "loss_pct": 0.0, "path_mtu": 1450, "source": "built-in defaults"}
LOSS_MIN_PCT = 15          # pod-to-pod loss above max(this, baseline + 10) is a fault
LATENCY_EXTRA_MS = 50      # RTT above baseline + this (and > 2x baseline) is a fault


def load_baseline():
    """Pod-to-pod (cross-node) numbers from the newest recorded baseline."""
    runs = sorted((REPO / "baseline" / "results").glob("*/summary.json"))
    if not runs:
        return dict(DEFAULT_BASELINE)
    try:
        t = json.loads(runs[-1].read_text(encoding="utf-8"))["tests"]["pod_to_pod_cross"]
        return {"rtt_avg_ms": t.get("rtt_avg_ms") or DEFAULT_BASELINE["rtt_avg_ms"],
                "loss_pct": t.get("loss_pct") or 0.0,
                "path_mtu": t.get("path_mtu") or DEFAULT_BASELINE["path_mtu"],
                "source": runs[-1].parent.name}
    except (ValueError, KeyError):
        return dict(DEFAULT_BASELINE)


class NetworkChecks:
    def __init__(self, diagnoser):
        self.d, self.k = diagnoser, diagnoser.k

    # --- helpers ---

    def diag(self, app, node):
        pods = [p for p in self.k.items("-n", DIAG_NS, "get", "pods", "-l", f"app={app}")
                if p["spec"].get("nodeName") == node and p["status"].get("phase") == "Running"]
        return pods[0] if pods else None

    def on_host(self, node, cmd, timeout=30):
        pod = self.diag("diag-host", node)
        if not pod:
            return None
        rc, out, err = self.k.sh(DIAG_NS, pod["metadata"]["name"], cmd, timeout=timeout)
        return out + err

    def in_pod(self, cmd, timeout=30):
        rc, out, err = self.k.sh(DIAG_NS, self.d.diag_pod, cmd, timeout=timeout)
        return rc, out + err

    def df_ping_ok(self, ip, size):
        rc, _ = self.in_pod(f"ping -M do -c 3 -i 0.2 -W 1 -s {size} {ip}", timeout=20)
        return rc == 0

    # --- main ---

    def run(self, client_node, server_pod):
        server_node, dst = server_pod["spec"].get("nodeName"), server_pod["status"].get("podIP")
        if not client_node or client_node == server_node:
            self.d.check("network", None, "backend and database are on the same node: no cross-node path")
            return
        nodes = {}
        for n in self.k.items("get", "nodes"):
            addrs = {a["type"]: a["address"] for a in n["status"]["addresses"]}
            nodes[n["metadata"]["name"]] = {"ip": addrs.get("InternalIP"), "cidr": n["spec"].get("podCIDR")}
        base = load_baseline()
        # Test the tunnel against the diag-pod on the DB's node, not the DB pod itself: a
        # NetworkPolicy on the DB pod would otherwise look exactly like a broken tunnel. It is
        # also what the baseline measured (diag-pod -> diag-pod). The DB pod is checked last.
        peer = (self.diag("diag-pod", server_node) or {}).get("status", {}).get("podIP")
        self.ctx = {"client": client_node, "server": server_node, "dst": dst, "probe": peer or dst,
                    "nodes": nodes, "base": base}

        # 1. Underlay: can the two Pis reach each other at all (over Wi-Fi)?
        node_ping = parse_ping(self.on_host(client_node, f"ping -c 5 -i 0.2 -W 1 {nodes[server_node]['ip']}") or "")
        self.ctx["node_ping"] = node_ping
        if node_ping.get("loss_pct") == 100.0:
            self.d.other_evidence.append(f"{client_node} cannot reach {server_node} ({nodes[server_node]['ip']}) "
                                         "at all, even outside Kubernetes: the Wi-Fi/underlay is down")
            self.d.check("underlay", False, f"node -> node: 100% loss")
            return
        if node_ping.get("loss_pct") is None:  # no diag-host pod, or the exec failed: unknown, not "ok"
            self.d.check("underlay", None, "node -> node ping could not run: underlay not compared")
        else:
            self.d.check("underlay", True, f"node -> node: {node_ping.get('loss_pct')}% loss, "
                                           f"{node_ping.get('rtt_avg_ms')} ms")

        # 2. Overlay: pod to pod across the VXLAN tunnel.
        probe = self.ctx["probe"]
        _, out = self.in_pod(f"ping -c 30 -i 0.1 -W 1 {probe}", timeout=40)
        pod_ping = parse_ping(out)
        self.ctx["pod_ping"] = pod_ping
        loss, rtt = pod_ping.get("loss_pct"), pod_ping.get("rtt_avg_ms")
        if loss is None:
            self.d.check("overlay", None, "pod -> pod ping could not run")
            return
        if loss >= 90:
            self.overlay_down()
            return

        def bad(l, r):
            return (l > max(LOSS_MIN_PCT, base["loss_pct"] + 10) or
                    (r is not None and r > base["rtt_avg_ms"] + LATENCY_EXTRA_MS and r > 2 * base["rtt_avg_ms"]))
        if bad(loss, rtt):
            # One ~3 s burst can hit a momentary hotspot stall: confirm with a second sample.
            _, out2 = self.in_pod(f"ping -c 30 -i 0.1 -W 1 {probe}", timeout=40)
            p2 = parse_ping(out2)
            if p2.get("loss_pct") is not None and not bad(p2["loss_pct"], p2.get("rtt_avg_ms")):
                self.d.check("overlay", True, f"first sample looked bad ({loss}% loss, {rtt} ms) but a second "
                                              f"one was normal ({p2['loss_pct']}%, {p2.get('rtt_avg_ms')} ms): "
                                              "transient Wi-Fi blip, ignored")
                loss, rtt = p2["loss_pct"], p2.get("rtt_avg_ms")
                self.ctx["pod_ping"] = p2
            elif p2.get("loss_pct") is not None and p2["loss_pct"] < loss:
                # confirmed: report the better (more conservative) of the two samples
                loss, rtt = p2["loss_pct"], p2.get("rtt_avg_ms")
                self.ctx["pod_ping"] = p2
        if loss > max(LOSS_MIN_PCT, base["loss_pct"] + 10):
            self.degraded("packet_loss", f"pod -> pod loss {loss}% (baseline {base['loss_pct']}%)")
            return
        if rtt is not None and rtt > base["rtt_avg_ms"] + LATENCY_EXTRA_MS and rtt > 2 * base["rtt_avg_ms"]:
            self.degraded("high_latency", f"pod -> pod RTT {rtt} ms (baseline {base['rtt_avg_ms']} ms)")
            return
        self.d.check("overlay", True, f"pod -> pod: {loss}% loss, {rtt} ms "
                                      f"(baseline {base['loss_pct']}%, {base['rtt_avg_ms']} ms)")

        # 3. MTU: do full-size packets still fit through the tunnel?
        n_findings = len(self.d.findings)
        self.check_mtu()

        # 4. The tunnel is fine. If the DB pod itself doesn't answer, something at the pod
        #    blocks it (e.g. a NetworkPolicy): not a set-2 problem, so a clue for `other`.
        if len(self.d.findings) == n_findings and probe != dst:
            rc, _ = self.in_pod(f"ping -c 3 -i 0.2 -W 1 {dst}", timeout=15)
            if rc != 0:
                self.d.other_evidence.append(
                    f"the pod tunnel to {server_node} works ({probe} answers), but the DB pod {dst} "
                    "does not answer ping: traffic is blocked at the DB pod itself "
                    "(e.g. a NetworkPolicy), not in the cross-node network")

    # --- overlay completely down: route or firewall? ---

    def overlay_down(self):
        c = self.ctx
        node_loss = c["node_ping"].get("loss_pct")
        evidence = ([f"node -> node works ({node_loss}% loss), "
                     f"but pod -> pod across nodes: {c['pod_ping'].get('loss_pct')}% loss",
                     "so the Pis can talk, but the pod tunnel (VXLAN over flannel.1) is broken"]
                    if node_loss is not None else
                    [f"pod -> pod across nodes: {c['pod_ping'].get('loss_pct')}% loss "
                     "(node -> node could not be measured, so the underlay is not ruled out)"])

        # a) routes: each node must send the other node's pod network into flannel.1
        client_pod_ip = (self.diag("diag-pod", c["client"]) or {}).get("status", {}).get("podIP")
        for node, peer, ip in ((c["client"], c["server"], c["dst"]), (c["server"], c["client"], client_pod_ip)):
            if not ip:
                continue
            hpod = self.diag("diag-host", node)
            if not hpod:
                continue
            rc, out, err = self.k.sh(DIAG_NS, hpod["metadata"]["name"], f"ip route get {ip}")
            route = " ".join(out.split())
            if rc != 0 or not route.startswith(ip) or " dev " not in route:
                self.d.check("routes", None, f"could not read the route on {node}: "
                                             f"{' '.join((out + err).split())[:120]}")
                continue  # unknown: never claim route_missing from a failed probe
            if "flannel.1" not in route:
                cidr = c["nodes"][peer]["cidr"]
                gw = cidr.split("/")[0]
                self.d.find("route_missing", "high",
                            f"{node} has no route to {peer}'s pod network {cidr} through the tunnel",
                            evidence + [f"on {node}: ip route get {ip} -> {route[:120]}",
                                        f"expected: {cidr} via {gw} dev flannel.1 onlink (flannel VXLAN route)"],
                            f"on {node}: ip route replace {cidr} via {gw} dev flannel.1 onlink",
                            {"type": "node_cmd", "rollback": False, "steps": [{
                                "node": node, "cmd": f"ip route replace {cidr} via {gw} dev flannel.1 onlink",
                                "undo": f"ip route del {cidr}"}]})
                return
        self.d.check("routes", True, "both nodes route the other's pod network via flannel.1")

        # b) firewall: anything dropping/rejecting VXLAN (UDP 8472) on either node?
        steps, found = [], []
        for node in (c["client"], c["server"]):
            for line in (self.on_host(node, "iptables -S") or "").splitlines():
                # exactly UDP port 8472 (not 18472, not an address containing it)
                if (line.startswith("-A ") and re.search(r"-p udp\b", line)
                        and re.search(r"--dports? (\S+,)?8472\b", line) and re.search(r"-j (DROP|REJECT)\b", line)):
                    found.append(f"on {node}: {line}")
                    steps.append({"node": node, "cmd": "iptables " + line.replace("-A ", "-D ", 1),
                                  "undo": "iptables " + line.replace("-A ", "-I ", 1)})
        if steps:
            self.d.find("overlay_blocked", "high",
                        "a firewall rule drops the VXLAN tunnel (UDP 8472) between the nodes",
                        evidence + found, "; ".join(f"on {s['node']}: {s['cmd']}" for s in steps),
                        {"type": "node_cmd", "rollback": False, "steps": steps})
            return
        self.d.check("firewall", True, "no rule drops or rejects UDP 8472 on either node")

        # c) extreme shaping: ~100% netem loss, or a delay longer than ping's 1 s timeout,
        #    also looks like a dead tunnel. That is still loss / latency, not `other`.
        for label, causes in (("packet_loss", "loss"), ("high_latency", "delay")):
            found, steps = self.netem_rules(causes)
            if steps:
                self.d.find(label, "high", f"the pod path is unusable ({c['pod_ping'].get('loss_pct')}% of pings "
                                           f"lost), caused by a traffic-control (netem) {causes} rule",
                            evidence + found, "; ".join(f"on {s['node']}: {s['cmd']}" for s in steps),
                            {"type": "node_cmd", "rollback": False, "steps": steps})
                return
        self.d.other_evidence += evidence + ["routes, firewall and traffic control look correct: check flannel "
                                             "on both nodes (ip -d link show flannel.1, k3s agent logs)"]

    def netem_rules(self, causes):
        """(evidence, fix steps) for netem rules on either node that produce `causes`
        ('loss' or 'delay'). A rule without it doesn't explain the symptom: reported, not removed."""
        c, evidence, steps = self.ctx, [], []
        for node in (c["client"], c["server"]):
            for line in (self.on_host(node, "tc qdisc show") or "").splitlines():
                m = re.search(r"qdisc netem \S+ dev (\S+) root .*?limit \d+\s*(.*)$", line)
                if not m:
                    continue
                dev, params = m.group(1), m.group(2).strip()
                if not re.search(rf"\b{causes}\b", params):
                    evidence.append(f"on {node}: netem rule on {dev} ({params}) has no {causes}: "
                                    "not the cause, left alone")
                    continue
                evidence.append(f"on {node}: traffic-control rule on {dev}: netem {params}")
                steps.append({"node": node, "cmd": f"tc qdisc del dev {dev} root",
                              "undo": f"tc qdisc add dev {dev} root netem {params}"})
        return evidence, steps

    # --- loss / latency: shaping rule (config) or physical link? ---

    def degraded(self, label, headline):
        c = self.ctx
        evidence = [headline,
                    f"node -> node (outside the tunnel): {c['node_ping'].get('loss_pct')}% loss, "
                    f"{c['node_ping'].get('rtt_avg_ms')} ms"]
        causes = "loss" if label == "packet_loss" else "delay"
        found, steps = self.netem_rules(causes)
        evidence += found
        what = "packet loss" if label == "packet_loss" else "latency"
        if steps:
            self.d.find(label, "high", f"high {what} on the pod path, caused by a traffic-control (netem) rule",
                        evidence, "; ".join(f"on {s['node']}: {s['cmd']}" for s in steps),
                        {"type": "node_cmd", "rollback": False, "steps": steps})
        else:
            # No configuration cause: on a Wi-Fi hotspot this is physical. Report, don't touch.
            self.d.find(label, "medium", f"high {what} on the pod path, with no configuration cause found "
                                       "(likely the physical/Wi-Fi link)",
                        evidence + [f"no netem rule with {causes} on either node"],
                        "check Wi-Fi signal and distance to the hotspot; nothing to change automatically")

    # --- MTU blackhole ---

    def check_mtu(self):
        c = self.ctx
        base_mtu = int(c["base"]["path_mtu"])
        # Two tries: 3 lost pings on Wi-Fi must not look like "too big".
        if self.df_ping_ok(c["probe"], base_mtu - 28) or self.df_ping_ok(c["probe"], base_mtu - 28):
            self.d.check("mtu", True, f"{base_mtu}-byte packets (DF) pass pod -> pod, as in the baseline")
            return
        lo, hi = 500, base_mtu - 28 - 1
        if not self.df_ping_ok(c["probe"], lo):
            self.d.check("mtu", None, "even 528-byte DF packets fail: not an MTU problem")
            return
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.df_ping_ok(c["probe"], mid):
                lo = mid
            else:
                hi = mid - 1
        pmtu = lo + 28

        # Does it actually hurt? A bulk TCP transfer to the diag pod on the DB's node.
        target = (self.diag("diag-pod", c["server"]) or {}).get("status", {}).get("podIP")
        tcp = None
        if target:
            rc, out = self.in_pod(f"timeout 12 iperf3 -c {target} -p 5201 -t 2 -J", timeout=25)
            tcp = parse_iperf(out)
            if "error" in tcp:
                err = tcp["error"].lower()
                # When `timeout` kills iperf3 mid-transfer its JSON is cut off, so match text:
                # a NON-EMPTY "connected": [{...}] list means the TCP session was established.
                connected = re.search(r'"connected"\s*:\s*\[\s*\{', out) is not None
                if any(s in err for s in ("busy", "unable to connect", "refused")):
                    tcp = None  # server busy/unreachable: bulk TCP not measured
                elif connected or rc in (124, 143):
                    tcp = {"error": "connected, but the transfer stalled (killed after 12 s)"}
                else:
                    tcp = None  # no iperf3 output at all (e.g. kubectl exec failed): unknown
        evidence = [f"path MTU pod -> pod is {pmtu} bytes, baseline {base_mtu}: "
                    f"packets of {pmtu + 1}-{base_mtu} bytes silently disappear (no ICMP 'too big')",
                    "small packets and pings pass, so the connection looks fine until something large is sent"]
        if tcp and "mbps" in tcp and tcp["mbps"] >= 1:
            self.d.check("mtu", None, f"path MTU {pmtu} < {base_mtu}, but bulk TCP works "
                                      f"({tcp['mbps']} Mbit/s): mitigated (MSS clamping in place)")
            return
        evidence.append("bulk TCP transfer: " + (f"{tcp.get('mbps')} Mbit/s" if tcp and "mbps" in tcp
                                                  else (tcp or {}).get("error", "not tested")))
        mss = pmtu - 40
        clamp = ("iptables -t mangle {op} FORWARD {dir} flannel.1 -p tcp --tcp-flags SYN,RST SYN "
                 "-m comment --comment ksh-heal-mss -j TCPMSS --set-mss {mss}")
        steps = [{"node": n, "cmd": clamp.format(op="-I", dir=d, mss=mss), "undo": clamp.format(op="-D", dir=d, mss=mss)}
                 for n in (c["client"], c["server"]) for d in ("-o", "-i")]
        self.d.find("mtu_blackhole", "high" if tcp else "medium",
                    f"MTU blackhole on the pod tunnel: only {pmtu}-byte packets fit, large TCP transfers stall",
                    evidence, f"clamp TCP MSS to {mss} on both nodes (mangle FORWARD, flannel.1)",
                    {"type": "node_cmd", "steps": steps} if tcp else None)
