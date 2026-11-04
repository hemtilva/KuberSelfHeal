#!/usr/bin/env python3
"""Diagnose why the backend can't reach its database: problem set 1.

Set 1 = Service discovery & DNS. The agent follows the path the backend's
connection takes and stops at the first broken link:

    DB_HOST --DNS--> Service --selector--> Pods --readiness--> Endpoints --targetPort--> Postgres

Labels:
    healthy             the app works and no set-1 check failed
    dns_coredns_down    (14) CoreDNS has no ready pods / kube-dns has no endpoints
    wrong_service_name  (12) DB_HOST names a Service that doesn't exist
    selector_mismatch   (7)  the Service's selector matches no pods
    no_ready_endpoints  (8)  pods match, but none are Ready (or the workload is scaled to 0)
    port_mismatch       (9)  Service port / targetPort doesn't reach the port Postgres listens on
    other               the app fails but every set-1 check passes: some other layer
                        (network, firewall, MTU, NetworkPolicy, ...), left for later sets

Run on the control-plane Pi (standard library + kubectl):

    python3 agent/diagnose.py
    python3 agent/diagnose.py --json          # machine-readable, used by eval/
    python3 agent/diagnose.py --kubectl "sudo k3s kubectl"

Active tests (category C) run inside the diag-pod on the backend's node, so
deploy k8s/diag.yaml first (baseline/collect.py does that). Without it the
agent falls back to configuration checks only.
"""

import argparse
import datetime
import difflib
import ipaddress
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "prober"))
from prober import hint_for  # noqa: E402  (same error-text classifier as the prober)

DIAG_NS = "kuberselfheal"
LABELS = {  # label -> problem number from the 60-problem list
    "healthy": None, "dns_coredns_down": 14, "wrong_service_name": 12,
    "selector_mismatch": 7, "no_ready_endpoints": 8, "port_mismatch": 9, "other": None,
}


class Kube:
    def __init__(self, cmd):
        self.cmd = shlex.split(cmd)

    def run(self, *args, timeout=60):
        try:
            p = subprocess.run(self.cmd + list(args), capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, "", f"timed out after {timeout}s"
        return p.returncode, p.stdout, p.stderr

    def get(self, *args):
        """kubectl get ... -o json; None if the object doesn't exist."""
        rc, out, err = self.run(*args, "-o", "json")
        if rc != 0:
            if "NotFound" in err or "not found" in err:
                return None
            raise RuntimeError(f"kubectl {' '.join(args)}: {err.strip()}")
        return json.loads(out)

    def items(self, *args):
        return (self.get(*args) or {}).get("items", [])

    def sh(self, ns, pod, cmd, timeout=30):
        return self.run("-n", ns, "exec", pod, "--", "sh", "-c", cmd, timeout=timeout)


def pod_age_s(pod):
    """Seconds since the pod started (0 if unknown)."""
    start = pod.get("status", {}).get("startTime")
    if not start:
        return 0.0
    started = datetime.datetime.fromisoformat(start.replace("Z", "+00:00"))
    return (datetime.datetime.now(datetime.timezone.utc) - started).total_seconds()


def matches(selector, labels):
    return all(labels.get(k) == v for k, v in selector.items())


def is_ready(pod):
    return any(c["type"] == "Ready" and c["status"] == "True"
               for c in pod.get("status", {}).get("conditions", []))


def parse_host(host, default_ns):
    """DB_HOST -> (service, namespace), or None for an IP / external name."""
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    parts = host.rstrip(".").split(".")
    if len(parts) == 1:
        return parts[0], default_ns
    if len(parts) == 2 or parts[2] == "svc":
        return parts[0], parts[1]
    return None


class Diagnoser:
    def __init__(self, kube, ns, client_deploy, client_service, active=True):
        self.k, self.ns = kube, ns
        self.client_deploy, self.client_service = client_deploy, client_service
        self.active = active
        self.diag_pod = None
        self.findings, self.checks = [], []
        self.client_labels = {}   # pod labels of the client (never point a Service at it)
        self.client_port = None
        self.other_evidence = []  # clues for the "other" label (e.g. NetworkPolicies on the DB pod)

    # --- bookkeeping ---

    def check(self, name, ok, detail):
        self.checks.append({"check": name, "ok": ok, "detail": detail})

    def find(self, label, confidence, summary, evidence, fix, action=None):
        """Record a finding. `action` is a machine-readable fix for agent/heal.py
        (None = no safe automatic fix; a human has to decide)."""
        self.findings.append({"label": label, "problem": LABELS[label], "confidence": confidence,
                              "summary": summary, "evidence": evidence, "fix": fix, "action": action})

    def owner_deployment(self, pod):
        """Deployment that owns a pod (pod -> ReplicaSet -> Deployment), or None."""
        ns = pod["metadata"].get("namespace", self.ns)
        rs_ref = next((o for o in pod["metadata"].get("ownerReferences", []) if o["kind"] == "ReplicaSet"), None)
        if not rs_ref:
            return None
        rs = self.k.get("-n", ns, "get", "replicaset", rs_ref["name"])
        dep_ref = next((o for o in (rs or {}).get("metadata", {}).get("ownerReferences", [])
                        if o["kind"] == "Deployment"), None)
        return self.k.get("-n", ns, "get", "deploy", dep_ref["name"]) if dep_ref else None

    # --- active tests, run from the diag-pod on the backend's node ---

    def tcp(self, ip, port):
        """'open' | 'refused' | 'timeout' | None (no diag pod, or kubectl exec itself failed)."""
        if not self.diag_pod:
            return None
        rc, out, err = self.k.sh(DIAG_NS, self.diag_pod, f"nc -zv -w 3 {ip} {port} 2>&1")
        if rc == 0:
            return "open"
        text = (out + err).lower()
        if "nc:" not in text and "connect" not in text:
            # No output from nc at all: the exec failed (timeout, stream error over the
            # hotspot, diag pod restarting). That says nothing about the port.
            self.check("active_tests", None, f"TCP test to {ip}:{port} could not run: "
                                             + " ".join((out + err).split())[:120])
            return None
        return "refused" if "refused" in text else "timeout"

    def probe_app(self):
        """Ask the backend for the DB value, through its ClusterIP (not DNS, which may be broken).

        Tries twice, so one lost packet on the hotspot doesn't turn a healthy cluster
        into a false alarm. Returns None if curl never ran (exec failed): unknown, not a failure.
        """
        svc = self.k.get("-n", self.ns, "get", "svc", self.client_service)
        if not svc or not self.diag_pod:
            return None
        ip, port = svc["spec"]["clusterIP"], svc["spec"]["ports"][0]["port"]
        for attempt in range(2):
            if attempt:
                time.sleep(1)
            rc, out, err = self.k.sh(DIAG_NS, self.diag_pod,
                                     f"curl -s -m 12 -w '\\n%{{http_code}}' http://{ip}:{port}/", timeout=20)
            body, _, code = out.rstrip().rpartition("\n")
            code = code.strip()
            if code == "200":
                return {"ok": True, "http_status": 200, "attempts": attempt + 1}
        if not code.isdigit():  # curl always prints a code (000 if it couldn't connect)
            self.check("app_probe", None, "could not run curl in the diag pod: "
                                          + " ".join((out + err).split())[:120])
            return None
        try:
            data = json.loads(body)
        except ValueError:
            data = {}
        error = data.get("error") or f"backend unreachable at {ip}:{port} (curl exit {rc}, HTTP {code})"
        return {"ok": False, "http_status": int(code) or None, "attempts": 2,
                "phase": data.get("phase"), "error": error, "hint": hint_for(error)}

    def policies_selecting(self, pod):
        """NetworkPolicies in the pod's namespace whose podSelector selects it."""
        found = []
        labels = pod["metadata"].get("labels", {})
        for np in self.k.items("-n", pod["metadata"].get("namespace", self.ns), "get", "networkpolicy"):
            ps = np["spec"].get("podSelector", {})
            if ps.get("matchExpressions") or matches(ps.get("matchLabels", {}), labels):
                types = ",".join(np["spec"].get("policyTypes", ["Ingress"]))
                found.append(f"NetworkPolicy {np['metadata']['name']} ({types}) selects pod "
                             f"{pod['metadata']['name']}")
        return found

    # --- set-1 checks, in path order ---

    def check_coredns(self):
        dep = self.k.get("-n", "kube-system", "get", "deploy", "coredns")
        if dep is None:
            self.check("coredns", None, "no coredns Deployment in kube-system (skipped)")
            return
        ready = dep.get("status", {}).get("readyReplicas", 0) or 0
        desired = dep["spec"].get("replicas", 1)
        eps = [e for s in self.k.items("-n", "kube-system", "get", "endpointslices",
                                       "-l", "kubernetes.io/service-name=kube-dns")
               for e in s.get("endpoints") or [] if e.get("conditions", {}).get("ready") is not False]
        if ready == 0 or not eps:
            if desired == 0:
                action = {"type": "scale", "ns": "kube-system", "kind": "deploy", "name": "coredns",
                          "replicas": 1, "previous": 0}
                fix = "kubectl -n kube-system scale deploy/coredns --replicas=1"
            else:  # pods exist but aren't ready: restarting them is the safe first step
                action = {"type": "restart", "ns": "kube-system", "name": "coredns"}
                fix = "kubectl -n kube-system rollout restart deploy/coredns"
            self.find("dns_coredns_down", "high",
                      "CoreDNS is down, so no Service name in the cluster can be resolved",
                      [f"coredns Deployment: {ready}/{desired} ready replicas",
                       f"kube-dns Service: {len(eps)} ready endpoints"], fix, action)
        else:
            self.check("coredns", True, f"{ready}/{desired} ready, {len(eps)} kube-dns endpoints")

    def check_service_exists(self, name, ns, host):
        svc = self.k.get("-n", ns, "get", "svc", name)
        if svc:
            self.check("service_exists", True, f"{host} -> Service {name}.{ns}")
            return svc
        all_svcs = [(s["metadata"]["name"], s["metadata"]["namespace"])
                    for s in self.k.items("get", "svc", "-A")]
        evidence = [f"backend DB_HOST={host} -> looks for Service '{name}' in namespace '{ns}': not found"]
        same_name = [f"{n}.{s}" for n, s in all_svcs if n == name]
        if same_name:
            evidence.append(f"a Service with that name exists in another namespace: {', '.join(same_name)}")
        # Candidates: same namespace, never the client's own Service, never the API server.
        candidates = sorted({n for n, s in all_svcs if s == ns and n not in (self.client_service, "kubernetes")})
        close = difflib.get_close_matches(name, candidates, n=3, cutoff=0.5)
        if close:
            evidence.append(f"similar Services in '{ns}': {', '.join(close)}")
        suggestion = same_name[0] if same_name else (close[0] if close else None)
        # Auto-fix only for exactly one close match in the same namespace that actually
        # exposes the port the client uses. A same-named Service in another namespace
        # would be a cross-environment change: report it, never apply it.
        unique = not same_name and len(close) == 1
        if unique:
            cand = self.k.get("-n", ns, "get", "svc", close[0]) or {}
            if self.client_port not in [p.get("port") for p in cand.get("spec", {}).get("ports", [])]:
                unique = False
                evidence.append(f"Service {close[0]} does not expose port {self.client_port}")
        action = ({"type": "set_env", "ns": self.ns, "name": self.client_deploy, "env": "DB_HOST",
                   "value": suggestion, "previous": host} if suggestion and unique else None)
        if suggestion and not unique:
            evidence.append("ambiguous, cross-namespace or port-incompatible candidate: not fixing automatically")
        self.find("wrong_service_name", "high",
                  f"DB_HOST points to a Service that doesn't exist ({host})", evidence,
                  f"kubectl -n {self.ns} set env deploy/{self.client_deploy} DB_HOST={suggestion}"
                  if suggestion else "fix DB_HOST in the backend Deployment", action)
        return None

    def check_selector(self, svc):
        """Return the pods the Service selects, or None to stop (finding recorded)."""
        ns, name = svc["metadata"]["namespace"], svc["metadata"]["name"]
        selector = svc["spec"].get("selector")
        if not selector:
            self.check("selector", None, "Service has no selector (manual endpoints); skipped")
            return []
        pods = [p for p in self.k.items("-n", ns, "get", "pods")
                if not p["metadata"].get("deletionTimestamp")]
        selected = [p for p in pods if matches(selector, p["metadata"].get("labels", {}))]
        sel_txt = ",".join(f"{k}={v}" for k, v in selector.items())
        if selected:
            self.check("selector", True, f"selector {sel_txt} matches {len(selected)} pod(s)")
            return selected

        # No pods: is a workload meant to provide them (scaled to 0) or is the selector wrong?
        for kind in ("deploy", "statefulset"):
            for w in self.k.items("-n", ns, "get", kind):
                tmpl = w["spec"]["template"]["metadata"].get("labels", {})
                if matches(selector, tmpl):
                    replicas = w["spec"].get("replicas", 1)
                    wname = w["metadata"]["name"]
                    # Restore the replica count the operator last applied (kubectl apply
                    # records it); fall back to 1.
                    try:
                        applied = json.loads(w["metadata"].get("annotations", {}).get(
                            "kubectl.kubernetes.io/last-applied-configuration", "{}"))
                        want = int(applied.get("spec", {}).get("replicas", 1)) or 1
                    except (ValueError, TypeError):
                        want = 1
                    action = ({"type": "scale", "ns": ns, "kind": kind, "name": wname,
                               "replicas": want, "previous": 0} if replicas == 0 else None)
                    self.find("no_ready_endpoints", "high" if replicas == 0 else "medium",
                              f"Service {name} has no backing pods: {kind}/{wname} "
                              + ("is scaled to 0" if replicas == 0 else "has no pods yet"),
                              [f"selector {sel_txt} matches the pod template of {kind}/{wname}",
                               f"{kind}/{wname}: spec.replicas={replicas}, matching pods=0"],
                              f"kubectl -n {ns} scale {kind}/{wname} --replicas={want}" if replicas == 0
                              else f"kubectl -n {ns} describe {kind}/{wname}", action)
                    return None

        evidence = [f"Service {name} selector {sel_txt} matches 0 of {len(pods)} pods in '{ns}'"]
        # Near misses: pods that have the selector's keys but different values,
        # most similar first (app=databse is closer to app=database than to app=backend).
        near = []
        for p in pods:
            labels = p["metadata"].get("labels", {})
            if not all(k in labels for k in selector):
                continue
            score = sum(difflib.SequenceMatcher(None, v, labels[k]).ratio() for k, v in selector.items())
            near.append((score, p["metadata"]["name"], {k: labels[k] for k in selector}))
        near.sort(key=lambda n: -n[0])
        for _, pname, labels in near[:3]:
            evidence.append(f"pod {pname} has {','.join(f'{k}={v}' for k, v in labels.items())}")
        fix = (f"kubectl -n {ns} patch svc {name} --type=merge -p "
               f"'{json.dumps({'spec': {'selector': near[0][2]}})}'"
               if near else f"set the selector of Service {name} to the labels of the intended pods")
        # Auto-fix only with a clear winner: the best near miss must beat the runner-up,
        # and must not be the client itself (the Service would then point at the backend).
        client_labels = self.client_labels or {}
        clear = (near and (len(near) == 1 or near[0][0] > near[1][0])
                 and not matches(near[0][2], client_labels))
        action = ({"type": "patch_selector", "ns": ns, "svc": name, "selector": near[0][2],
                   "previous": selector} if clear else None)
        self.find("selector_mismatch", "high" if near else "medium",
                  f"Service {name}'s selector matches no pods", evidence, fix, action)
        return None

    def check_endpoints(self, svc, selected):
        """Return the Ready pods behind the Service, or None to stop."""
        ns, name = svc["metadata"]["namespace"], svc["metadata"]["name"]
        slices = self.k.items("-n", ns, "get", "endpointslices", "-l", f"kubernetes.io/service-name={name}")
        ready_ips = {a for s in slices for e in s.get("endpoints") or []
                     if e.get("conditions", {}).get("ready") is not False for a in e.get("addresses", [])}
        ready_pods = [p for p in selected if p["status"].get("podIP") in ready_ips]
        if ready_pods:
            self.check("endpoints", True, f"{len(ready_pods)} ready endpoint(s): "
                       + ", ".join(p["status"]["podIP"] for p in ready_pods))
            return ready_pods

        evidence = [f"Service {name}: 0 ready endpoints, although {len(selected)} pod(s) match the selector"]
        for p in selected[:3]:
            pname = p["metadata"]["name"]
            restarts = sum(c.get("restartCount", 0) for c in p["status"].get("containerStatuses", []))
            evidence.append(f"pod {pname}: phase={p['status'].get('phase')}, "
                            f"ready={is_ready(p)}, restarts={restarts}")
            events = self.k.items("-n", ns, "get", "events", "--field-selector", f"involvedObject.name={pname}")
            msgs = [e.get("message", "").strip() for e in events if e.get("reason") in ("Unhealthy", "BackOff", "Failed")]
            for m in msgs[-2:]:
                evidence.append(f"event on {pname}: {m[:160]}")

        # Healing: if the owning Deployment has an earlier revision, the most likely
        # cause is a recent bad change -> roll back. heal.py waits first if the pods
        # are young (they may simply still be starting).
        action, fix = None, (f"kubectl -n {ns} describe pod {selected[0]['metadata']['name']} "
                             "(check the readinessProbe and the container logs)")
        dep = self.owner_deployment(selected[0])
        if dep:
            dname = dep["metadata"]["name"]
            rev = int(dep["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision", "1"))
            ages = [pod_age_s(p) for p in selected]
            # Roll back only if the CURRENT revision never finished rolling out: then a recent
            # change is the likely cause. If it once served fine (NewReplicaSetAvailable), the
            # problem is something else and rolling back could restore an older bad template.
            prog = next((c for c in dep.get("status", {}).get("conditions", []) if c.get("type") == "Progressing"), {})
            rollout_done = prog.get("reason") == "NewReplicaSetAvailable"
            dsel = ",".join(f"{k}={v}" for k, v in dep["spec"]["selector"].get("matchLabels", {}).items())
            revs = {int(rs["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision", "0"))
                    for rs in self.k.items("-n", ns, "get", "replicaset", "-l", dsel)
                    if any(o.get("kind") == "Deployment" and o.get("name") == dname
                           for o in rs["metadata"].get("ownerReferences", []))}
            prev = max((r for r in revs if r < rev), default=None)
            evidence.append(f"deploy/{dname} is at revision {rev}, rollout "
                            + ("completed earlier" if rollout_done else f"not completed ({prog.get('reason')})")
                            + f"; youngest pod is {min(ages):.0f} s old")
            if prev and not rollout_done:
                action = {"type": "rollout_undo", "ns": ns, "name": dname, "revision": rev,
                          "to_revision": prev, "pod_age_s": min(ages)}
                fix = f"kubectl -n {ns} rollout undo deploy/{dname} --to-revision={prev}  (revision {rev} never became ready)"
        self.find("no_ready_endpoints", "high",
                  f"pods behind {name} exist but none are Ready, so the Service has no endpoints",
                  evidence, fix, action)
        return None

    def check_ports(self, svc, ready_pods, client_port):
        ns, name = svc["metadata"]["namespace"], svc["metadata"]["name"]
        ports = svc["spec"].get("ports", [])
        sp = next((p for p in ports if p["port"] == client_port), None)
        if sp is None:
            self.find("port_mismatch", "high",
                      f"backend connects to port {client_port}, but Service {name} doesn't expose it",
                      [f"backend DB_PORT={client_port}",
                       f"Service {name} ports: {', '.join(str(p['port']) for p in ports)}"],
                      f"set DB_PORT to {ports[0]['port']} or add port {client_port} to Service {name}")
            return
        tp = sp.get("targetPort", sp["port"])
        pod = ready_pods[0]
        pod_ip, pod_name = pod["status"]["podIP"], pod["metadata"]["name"]
        cports = [(cp.get("name"), cp["containerPort"])
                  for c in pod["spec"]["containers"] for cp in c.get("ports", [])]
        declared = [n for _, n in cports]

        if isinstance(tp, str):
            resolved = next((n for nm, n in cports if nm == tp), None)
            if resolved is None:
                self.find("port_mismatch", "high", f"Service {name} targetPort '{tp}' is not a port name on the pod",
                          [f"pod {pod_name} named ports: {', '.join(f'{nm}={n}' for nm, n in cports) or 'none'}"],
                          f"set targetPort of Service {name} to one of the pod's ports")
                return
            tp = resolved

        state = self.tcp(pod_ip, tp)
        evidence = [f"Service {name}: port {sp['port']} -> targetPort {tp}",
                    f"pod {pod_name} declares containerPort(s): {', '.join(map(str, declared)) or 'none'}"]
        if state:
            evidence.append(f"TCP {pod_ip}:{tp} from the backend's node: {state}")

        # 'refused' alone does NOT prove a wrong port: k3s's NetworkPolicy controller
        # (kube-router) REJECTs denied traffic, which also looks like "connection refused".
        # So we need positive evidence that the port is wrong: the targetPort isn't a
        # declared port, or another declared port is open on the same pod.
        open_alts = []
        if state == "refused":
            open_alts = [alt for alt in declared if alt != tp and self.tcp(pod_ip, alt) == "open"]
            for alt in open_alts:
                evidence.append(f"TCP {pod_ip}:{alt}: open  <- the pod is listening here")
        wrong_port = bool(open_alts) or bool(declared and tp not in declared)

        if state == "refused" and wrong_port:
            fix_port = open_alts[0] if open_alts else declared[0]
            self.find("port_mismatch", "high",
                      f"Service {name} forwards to port {tp}, but nothing listens there on the pod",
                      evidence, f"kubectl -n {ns} patch svc {name} --type=json -p '"
                                + json.dumps([{"op": "replace", "path": f"/spec/ports/{ports.index(sp)}/targetPort",
                                               "value": fix_port}]) + "'",
                      {"type": "patch_target_port", "ns": ns, "svc": name, "index": ports.index(sp),
                       "value": fix_port, "previous": tp})
        elif state != "open" and wrong_port:
            self.find("port_mismatch", "medium",
                      f"Service {name} targetPort {tp} is not a port the pod declares "
                      + ("(no active test available)" if state is None else f"(active test inconclusive: {state})"),
                      evidence, f"set targetPort of Service {name} to {declared[0]}")
        elif state in ("refused", "timeout"):
            # Port is right but traffic doesn't get through: not a set-1 problem.
            self.other_evidence += evidence + self.policies_selecting(pod)
            self.check("ports", None, f"targetPort {tp} is the pod's declared port, but TCP is {state}: "
                                      "traffic is blocked or dropped on the way (not a set-1 problem)")
        else:
            self.check("ports", True, "; ".join(evidence))

    # --- main ---

    def run(self):
        start = time.monotonic()
        result = {"target": None, "symptom": None}

        client = self.k.get("-n", self.ns, "get", "deploy", self.client_deploy)
        if not client:
            return self.finish(start, result, error=f"Deployment {self.client_deploy} not found in {self.ns}")
        env = {e["name"]: e.get("value") for c in client["spec"]["template"]["spec"]["containers"]
               for e in c.get("env", [])}
        host, port = env.get("DB_HOST") or "db-service", int(env.get("DB_PORT") or 5432)
        self.client_port = port
        result["target"] = f"{host}:{port}"

        sel = client["spec"]["selector"].get("matchLabels", {})
        self.client_labels = client["spec"]["template"].get("metadata", {}).get("labels") or sel
        nodes = [p["spec"].get("nodeName") for p in self.k.items("-n", self.ns, "get", "pods")
                 if matches(sel, p["metadata"].get("labels", {})) and p["status"].get("phase") == "Running"]
        if self.active and nodes:
            diag = [p for p in self.k.items("-n", DIAG_NS, "get", "pods", "-l", "app=diag-pod")
                    if p["spec"].get("nodeName") == nodes[0] and p["status"].get("phase") == "Running"]
            self.diag_pod = diag[0]["metadata"]["name"] if diag else None
        if self.active and not self.diag_pod:
            self.check("active_tests", None, "no diag-pod on the backend's node: config checks only "
                                             "(kubectl apply -f k8s/diag.yaml)")

        result["symptom"] = self.probe_app()
        self.check_coredns()

        parsed = parse_host(host, self.ns)
        if parsed is None:
            self.check("target", None, f"DB_HOST={host} is an IP or external name: outside set 1")
        else:
            svc = self.check_service_exists(*parsed, host)
            if svc:
                selected = self.check_selector(svc)
                if selected:
                    ready = self.check_endpoints(svc, selected)
                    if ready:
                        self.check_ports(svc, ready, port)
        return self.finish(start, result)

    def finish(self, start, result, error=None):
        symptom = result.get("symptom")
        if error:
            primary = {"label": "other", "confidence": "low", "summary": error, "evidence": [], "fix": None}
        elif self.findings:
            primary = self.findings[0]
        elif symptom and not symptom["ok"]:
            hint = symptom.get("hint")
            why = {"timeout": "timeout with a correct Service -> packets are silently dropped: "
                              "firewall DROP (e.g. VXLAN UDP 8472), MTU or routing (later sets)",
                   "refused": "refused although Service, endpoints and port are correct -> something "
                              "actively rejects the traffic: a NetworkPolicy (k3s/kube-router REJECTs "
                              "denied packets) or a firewall REJECT rule (later sets)"}.get(hint)
            primary = {"label": "other", "confidence": "medium",
                       "summary": "the backend cannot reach the database, but every set-1 check passed: "
                                  "the problem is in another layer",
                       "evidence": [f"app error: {' '.join(symptom['error'].split())[:200]}", f"error type: {hint}"]
                                   + ([why] if why else []) + self.other_evidence,
                       "fix": None}
        else:
            primary = {"label": "healthy", "confidence": "high" if symptom else "low",
                       "summary": "app works and all set-1 checks passed" if symptom
                                  else "all set-1 checks passed (app not probed: no diag-pod)",
                       "evidence": [], "fix": None}
        primary = {**primary, "problem": LABELS[primary["label"]]}
        return {**primary, "target": result.get("target"), "symptom": symptom,
                "all_findings": self.findings, "checks": self.checks,
                "elapsed_s": round(time.monotonic() - start, 2)}


def print_report(r):
    prob = f" (problem {r['problem']})" if r["problem"] else ""
    print(f"\n  DIAGNOSIS: {r['label']}{prob}   confidence: {r['confidence']}")
    print(f"  {r['summary']}\n")
    s = r.get("symptom")
    print(f"  target   : backend -> {r.get('target')}")
    print("  symptom  : " + ("not probed" if not s else "app OK" if s["ok"]
                            else f"FAIL [{s.get('hint')}] {' '.join(s['error'].split())[:110]}"))
    if r["evidence"]:
        print("\n  evidence:")
        for e in r["evidence"]:
            print(f"    - {e}")
    if r.get("fix"):
        print(f"\n  suggested fix:\n    {r['fix']}")
    if len(r["all_findings"]) > 1:
        print("\n  other findings: " + ", ".join(f["label"] for f in r["all_findings"][1:]))
    print("\n  checks:")
    for c in r["checks"]:
        mark = {True: "ok ", False: "BAD", None: " - "}[c["ok"]]
        print(f"    [{mark}] {c['check']}: {c['detail']}")
    for f in r["all_findings"]:
        print(f"    [BAD] {f['label']}: {f['summary']}")
    print(f"\n  ({r['elapsed_s']} s)\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kubectl", default="kubectl")
    ap.add_argument("--namespace", default="default")
    ap.add_argument("--client-deploy", default="backend")
    ap.add_argument("--client-service", default="backend-service")
    ap.add_argument("--no-active", action="store_true", help="config checks only, no tests from diag-pod")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    args = ap.parse_args()

    d = Diagnoser(Kube(args.kubectl), args.namespace, args.client_deploy, args.client_service,
                  active=not args.no_active)
    result = d.run()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_report(result)


if __name__ == "__main__":
    main()
