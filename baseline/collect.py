#!/usr/bin/env python3
"""Record what the cluster's network looks like when it is HEALTHY.

Run on the control-plane Pi (needs kubectl access; standard library only):

    python3 baseline/collect.py
    python3 baseline/collect.py --kubectl "sudo k3s kubectl"   # if kubeconfig needs root

It measures the path from the backend worker (role=backend) to the database
worker (role=database) at three levels, which are the same comparisons the
diagnosis agent will use later:

    node -> node          the hotspot / Wi-Fi link itself
    pod  -> pod (cross)   the same link, inside the Flannel VXLAN overlay
    pod  -> pod (same)    the control case: never leaves the node

plus DNS lookup time, end-to-end app latency (via prober.py), and a snapshot of
node network facts (MTUs, VXLAN port and interface, Wi-Fi signal, conntrack,
CIDR overlap check).

Output: baseline/results/<timestamp>/summary.json, plus raw/ with every command
and its output. Run it 3+ times at different moments; Wi-Fi varies a lot.
"""

import argparse
import ipaddress
import json
import math
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NS = "kuberselfheal"


# ---------------------------------------------------------------- parsers --

def parse_ping(out):
    res = {"loss_pct": None}
    m = re.search(r"([\d.]+)% packet loss", out)
    if m:
        res["loss_pct"] = float(m.group(1))
    m = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)(?:/([\d.]+))? ms", out)
    if m:
        res.update(rtt_min_ms=float(m.group(1)), rtt_avg_ms=float(m.group(2)),
                   rtt_max_ms=float(m.group(3)),
                   rtt_mdev_ms=float(m.group(4)) if m.group(4) else None)
    return res


def parse_iperf(out):
    try:
        data = json.loads(out)
    except ValueError:
        return {"error": "no JSON from iperf3: " + " ".join(out.split())[:200]}
    if "error" in data:
        return {"error": data["error"]}
    end = data["end"]
    return {"mbps": round(end["sum_received"]["bits_per_second"] / 1e6, 1),
            "retransmits": end["sum_sent"].get("retransmits")}


def sections(out):
    """Split output of `echo '## name'; cmd; ...` into {name: text}."""
    found, name = {}, None
    for line in out.splitlines():
        if line.startswith("## "):
            name = line[3:].strip()
            found[name] = []
        elif name:
            found[name].append(line)
    return {k: "\n".join(v) for k, v in found.items()}


def percentile(values, p):
    if not values:
        return None
    values = sorted(values)  # nearest-rank percentile
    return values[max(0, math.ceil(p / 100 * len(values)) - 1)]


# -------------------------------------------------------------- collector --

class Collector:
    def __init__(self, kubectl, outdir):
        self.kubectl_cmd = shlex.split(kubectl)
        self.outdir = outdir
        (outdir / "raw").mkdir(parents=True, exist_ok=True)

    def kubectl(self, *args, timeout=120, check=True):
        try:
            p = subprocess.run(self.kubectl_cmd + list(args), capture_output=True,
                               text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            if check:
                raise RuntimeError(f"kubectl {' '.join(args)} timed out after {timeout}s")
            return subprocess.CompletedProcess(args, 124, "", f"timed out after {timeout}s")
        if check and p.returncode != 0:
            raise RuntimeError(f"kubectl {' '.join(args)} failed: {p.stderr.strip()}")
        return p

    def kjson(self, *args):
        return json.loads(self.kubectl(*args, "-o", "json").stdout)

    def sh(self, pod, cmd, timeout=120, raw=None):
        """Run a shell command inside a diag pod; optionally save it to raw/."""
        p = self.kubectl("-n", NS, "exec", pod, "--", "sh", "-c", cmd,
                         timeout=timeout, check=False)
        if raw:
            text = f"$ {cmd}\n[exit {p.returncode}]\n{p.stdout}"
            if p.stderr.strip():
                text += f"\n[stderr]\n{p.stderr}"
            (self.outdir / "raw" / f"{raw}.txt").write_text(text, encoding="utf-8")
        return p.returncode, p.stdout

    # --- tests ---

    def ping(self, pod, target, raw, count=20):
        _, out = self.sh(pod, f"ping -c {count} -i 0.2 -W 2 {target}", timeout=count + 30, raw=raw)
        return parse_ping(out)

    def path_mtu(self, pod, target, raw, lo=500, hi=1472):
        """Largest packet that gets through with Don't-Fragment set (payload + 28)."""
        tries = []

        def passes(size):
            rc, _ = self.sh(pod, f"ping -M do -c 3 -i 0.2 -W 1 -s {size} {target}", timeout=30)
            tries.append((size, rc == 0))
            return rc == 0

        if not passes(lo):
            result = None
        else:
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if passes(mid):
                    lo = mid
                else:
                    hi = mid - 1
            result = lo + 28
        (self.outdir / "raw" / f"{raw}.txt").write_text(
            f"path MTU search to {target}: (payload size, passed)\n"
            + "\n".join(f"{s}\t{ok}" for s, ok in tries)
            + f"\nresult: {result}\n", encoding="utf-8")
        return result

    def iperf(self, pod, target, port, raw, seconds=5):
        _, out = self.sh(pod, f"iperf3 -c {target} -p {port} -t {seconds} -J",
                         timeout=seconds + 40, raw=raw)
        return parse_iperf(out)

    def dns(self, pod, name, raw, count=20):
        cmd = (f"for i in $(seq {count}); do "
               f"dig +tries=1 +time=2 {name} | grep -E 'Query time|status:|timed out'; done")
        _, out = self.sh(pod, cmd, timeout=count * 5 + 30, raw=raw)
        times = [int(x) for x in re.findall(r"Query time: (\d+) msec", out)]
        ok = len(re.findall(r"status: NOERROR", out))
        return {"queries": count, "ok": ok,
                "avg_ms": round(sum(times) / len(times), 1) if times else None,
                "p95_ms": percentile(times, 95), "max_ms": max(times) if times else None}

    def node_facts(self, pod, node, raw):
        cmd = ("echo '## link'; ip -o link; "
               "echo '## addr'; ip -o -4 addr; "
               "echo '## route'; ip route; "
               "echo '## flannel'; ip -d link show flannel.1; "
               "echo '## wireless'; cat /proc/net/wireless; "
               "echo '## conntrack'; cat /proc/sys/net/netfilter/nf_conntrack_count "
               "/proc/sys/net/netfilter/nf_conntrack_max")
        _, out = self.sh(pod, cmd, raw=raw)
        sec = sections(out)
        facts = {"mtu": {}}
        for m in re.finditer(r"^\d+:\s+([^:@\s]+)(?:@\S+)?:\s.*?\bmtu (\d+)", sec.get("link", ""), re.M):
            if m.group(1) in ("eth0", "wlan0", "flannel.1", "cni0", "tailscale0"):
                facts["mtu"][m.group(1)] = int(m.group(2))
        for m in re.finditer(r"^\d+:\s+(\S+)\s+inet (\S+)", sec.get("addr", ""), re.M):
            iface = ipaddress.ip_interface(m.group(2))
            if str(iface.ip) == node["ip"]:
                facts["node_iface"] = m.group(1)
                facts["node_network"] = str(iface.network)
        fl = sec.get("flannel", "")
        if m := re.search(r"dstport (\d+)", fl):
            facts["vxlan_port"] = int(m.group(1))
        if m := re.search(r"\blocal (\S+) dev (\S+)", fl):
            facts["vxlan_local_ip"], facts["vxlan_dev"] = m.group(1), m.group(2)
        for line in sec.get("wireless", "").splitlines():
            if m := re.match(r"\s*(\w+):\s+\w+\s+([-\d.]+)\s+([-\d.]+)", line):
                facts["wifi"] = {"iface": m.group(1), "link_quality": float(m.group(2)),
                                 "signal_dbm": float(m.group(3))}
        nums = sec.get("conntrack", "").split()
        if len(nums) == 2 and all(n.isdigit() for n in nums):
            facts["conntrack_count"], facts["conntrack_max"] = int(nums[0]), int(nums[1])
        return facts

    def app_latency(self, url, count):
        out_file = self.outdir / "app_probes.jsonl"
        with open(self.outdir / "raw" / "prober.txt", "w", encoding="utf-8") as log:
            subprocess.run([sys.executable, str(REPO / "prober" / "prober.py"), "--url", url,
                            "--count", str(count), "--interval", "0.5", "--out", str(out_file)],
                           stdout=log, stderr=subprocess.STDOUT, timeout=count * 15 + 60)
        recs = [json.loads(line) for line in out_file.read_text(encoding="utf-8").splitlines() if line]
        lat = [r["latency_ms"] for r in recs if r["ok"]]
        return {"url": url, "probes": len(recs), "ok": len(lat),
                "p50_ms": percentile(lat, 50), "p95_ms": percentile(lat, 95),
                "p99_ms": percentile(lat, 99), "max_ms": max(lat) if lat else None,
                "failure_hints": sorted({r["hint"] for r in recs if not r["ok"]})}


# ------------------------------------------------------------------ main --

def running_pods(c, ns, selector):
    pods = c.kjson("-n", ns, "get", "pods", "-l", selector)["items"]
    return {p["spec"]["nodeName"]: {"name": p["metadata"]["name"], "ip": p["status"].get("podIP")}
            for p in pods if p["status"].get("phase") == "Running"}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kubectl", default="kubectl", help='kubectl command, e.g. "sudo k3s kubectl"')
    ap.add_argument("--app-namespace", default="default")
    ap.add_argument("--cluster-cidr", default="10.42.0.0/16", help="k3s default pod CIDR")
    ap.add_argument("--service-cidr", default="10.43.0.0/16", help="k3s default Service CIDR")
    ap.add_argument("--app-probes", type=int, default=60, help="requests sent through the app")
    ap.add_argument("--no-apply", action="store_true", help="don't (re)apply k8s/diag.yaml")
    args = ap.parse_args()

    outdir = REPO / "baseline" / "results" / time.strftime("%Y%m%d-%H%M%S")
    c = Collector(args.kubectl, outdir)
    step = lambda msg: print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)

    # --- nodes ---
    nodes = {}
    for n in c.kjson("get", "nodes")["items"]:
        addrs = {a["type"]: a["address"] for a in n["status"]["addresses"]}
        ready = any(cd["type"] == "Ready" and cd["status"] == "True" for cd in n["status"]["conditions"])
        nodes[n["metadata"]["name"]] = {"ip": addrs.get("InternalIP"), "ready": ready,
                                        "role": n["metadata"]["labels"].get("role"),
                                        "pod_cidr": n["spec"].get("podCIDR")}
    by_role = {v["role"]: k for k, v in nodes.items() if v["role"]}
    if "backend" not in by_role or "database" not in by_role:
        sys.exit("Label the workers first: kubectl label node <pi> role=backend / role=database")
    src, dst = by_role["backend"], by_role["database"]
    step(f"path under test: {src} ({nodes[src]['ip']}) -> {dst} ({nodes[dst]['ip']})")

    # --- diag pods ---
    if not args.no_apply:
        step("applying k8s/diag.yaml (first image pull can take several minutes on a hotspot)")
        c.kubectl("apply", "-f", str(REPO / "k8s" / "diag.yaml"))
    for ds in ("diag-host", "diag-pod"):
        c.kubectl("-n", NS, "rollout", "status", f"ds/{ds}", "--timeout=900s", timeout=960)
    host = running_pods(c, NS, "app=diag-host")
    podnet = running_pods(c, NS, "app=diag-pod")

    summary = {"collected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "src_node": src, "dst_node": dst,
               "nodes": nodes, "node_facts": {}, "tests": {}, "warnings": []}
    T, W = summary["tests"], summary["warnings"]

    step("node network facts")
    for name in nodes:
        if name in host:
            summary["node_facts"][name] = c.node_facts(host[name]["name"], nodes[name], f"facts_{name}")

    step("node -> node (ping, path MTU, iperf3)")
    T["node_to_node"] = {**c.ping(host[src]["name"], nodes[dst]["ip"], "ping_node"),
                         "path_mtu": c.path_mtu(host[src]["name"], nodes[dst]["ip"], "mtu_node"),
                         **c.iperf(host[src]["name"], nodes[dst]["ip"], 5202, "iperf_node")}

    step("pod -> pod across nodes (ping, path MTU, iperf3)")
    T["pod_to_pod_cross"] = {**c.ping(podnet[src]["name"], podnet[dst]["ip"], "ping_pod_cross"),
                             "path_mtu": c.path_mtu(podnet[src]["name"], podnet[dst]["ip"], "mtu_pod_cross"),
                             **c.iperf(podnet[src]["name"], podnet[dst]["ip"], 5201, "iperf_pod_cross")}

    backend = running_pods(c, args.app_namespace, "app=backend")
    database = running_pods(c, args.app_namespace, "app=database")
    if src in backend:
        step("pod -> pod on the same node (control case)")
        T["pod_to_pod_same"] = c.ping(podnet[src]["name"], backend[src]["ip"], "ping_pod_same")
    if dst in database:
        step("pod -> database pod")
        T["pod_to_db_pod"] = c.ping(podnet[src]["name"], database[dst]["ip"], "ping_pod_db")

    step("DNS lookups of db-service")
    T["dns"] = c.dns(podnet[src]["name"], f"db-service.{args.app_namespace}.svc.cluster.local", "dns")

    if backend:
        url = f"http://{nodes[src]['ip']}:30080/"
        step(f"app latency: {args.app_probes} requests to {url}")
        T["app"] = c.app_latency(url, args.app_probes)
    else:
        W.append("backend pod not running: skipped app latency")

    # --- deterministic sanity checks on the healthy cluster ---
    nets = {"cluster": ipaddress.ip_network(args.cluster_cidr), "service": ipaddress.ip_network(args.service_cidr)}
    for name, f in summary["node_facts"].items():
        if "node_network" in f:
            for label, net in nets.items():
                if ipaddress.ip_network(f["node_network"]).overlaps(net):
                    W.append(f"{name}: node network {f['node_network']} overlaps the {label} CIDR {net} (subnet clash)")
        if f.get("vxlan_local_ip") and f["vxlan_local_ip"] != nodes[name]["ip"]:
            W.append(f"{name}: VXLAN uses {f['vxlan_local_ip']} on {f.get('vxlan_dev')}, "
                     f"but node IP is {nodes[name]['ip']} (wrong --flannel-iface?)")
        if f.get("vxlan_port") and f["vxlan_port"] != 8472:
            W.append(f"{name}: VXLAN port is {f['vxlan_port']}, not 8472")
    for test in ("node_to_node", "pod_to_pod_cross"):
        if (T[test].get("loss_pct") or 0) > 0:
            W.append(f"{test}: {T[test]['loss_pct']}% packet loss even when healthy (Wi-Fi?)")
        if "error" in T[test]:
            W.append(f"{test}: iperf3 failed: {T[test]['error']}")
    if T.get("app") and T["app"]["ok"] < T["app"]["probes"]:
        W.append(f"app: {T['app']['probes'] - T['app']['ok']} of {T['app']['probes']} requests failed "
                 f"({', '.join(T['app']['failure_hints'])})")

    (outdir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print_report(summary, outdir)


def print_report(s, outdir):
    fmt = lambda v, unit="": "-" if v is None else f"{v}{unit}"
    T = s["tests"]
    print(f"\nBaseline {s['collected_at']}   {s['src_node']} -> {s['dst_node']}\n")
    print(f"{'path':<22}{'loss':>7}{'rtt avg':>11}{'jitter':>9}{'path MTU':>10}{'throughput':>14}")
    for key, label in [("node_to_node", "node -> node"), ("pod_to_pod_cross", "pod -> pod (cross)"),
                       ("pod_to_db_pod", "pod -> db pod"), ("pod_to_pod_same", "pod -> pod (same)")]:
        t = T.get(key)
        if not t:
            continue
        print(f"{label:<22}{fmt(t.get('loss_pct'), '%'):>7}{fmt(t.get('rtt_avg_ms'), ' ms'):>11}"
              f"{fmt(t.get('rtt_mdev_ms'), ' ms'):>9}{fmt(t.get('path_mtu')):>10}"
              f"{fmt(t.get('mbps'), ' Mbit/s'):>14}")
    d = T["dns"]
    print(f"\nDNS   {d['ok']}/{d['queries']} ok, avg {fmt(d['avg_ms'], ' ms')}, p95 {fmt(d['p95_ms'], ' ms')}")
    if a := T.get("app"):
        print(f"App   {a['ok']}/{a['probes']} ok, p50 {fmt(a['p50_ms'], ' ms')}, "
              f"p95 {fmt(a['p95_ms'], ' ms')}, p99 {fmt(a['p99_ms'], ' ms')}")
    print("\nNode facts")
    for name, f in s["node_facts"].items():
        wifi = f.get("wifi", {})
        mtus = ", ".join(f"{k} {v}" for k, v in f["mtu"].items())
        print(f"  {name:<16} {f.get('node_iface', '?')} {f.get('node_network', '?'):<18} "
              f"MTU [{mtus}]  vxlan {f.get('vxlan_dev', '?')}:{f.get('vxlan_port', '?')}  "
              f"signal {fmt(wifi.get('signal_dbm'), ' dBm')}  "
              f"conntrack {fmt(f.get('conntrack_count'))}/{fmt(f.get('conntrack_max'))}")
    print("\nWarnings" if s["warnings"] else "\nNo warnings.")
    for w in s["warnings"]:
        print(f"  ! {w}")
    print(f"\nSaved to {outdir}")


if __name__ == "__main__":
    main()
