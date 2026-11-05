# KuberSelfHeal: source code and setup guide

> **Run every command in this guide from this `code/` folder.** On the Pis,
> copy or clone the repository and `cd KuberSelfHeal/code` first.

Diagnosing and self-healing failed connections between pods on two Kubernetes
nodes, on a 3× Raspberry Pi 5 (8 GB) k3s cluster (1 control plane + 2 workers).

The test app is a backend pod on one worker that reads from Postgres on the
other worker, so every request crosses between the two nodes. It's based on
[prathamchoksi/CN_Project_App](https://github.com/prathamchoksi/CN_Project_App).

```
app/        Flask backend (Dockerfile, app.py)
k8s/        app.yaml (the test app), diag.yaml (network toolbox pods on every node)
prober/     Probes the backend continuously and records failures (JSONL)
baseline/   collect.py records the healthy network; results/ holds the runs
faults/     fault.sh injects / resets the faults the agent covers
agent/      diagnose.py finds and labels the cause; heal.py fixes it and verifies
agent/reroute.py  moves the backend to a node with a working path to the DB (section 8)
eval/       run_diagnose.py / run_heal.py / run_reroute.py: inject every fault N times and score the agent
```

**Scope.** The agent detects and heals the faults below, in two groups. Anything
else is labelled `other` and handed to a human, never "fixed".

| Group | Layer | Faults (label) |
|---|---|---|
| 1 | Kubernetes service path (service discovery & DNS) | `dns_coredns_down`, `wrong_service_name`, `selector_mismatch`, `no_ready_endpoints`, `port_mismatch` |
| 2 | Cross-node network | `overlay_blocked` (firewall), `route_missing`, `mtu_blackhole`, `packet_loss`, `high_latency` |

## 0. Cluster on a phone hotspot

All 3 Pis join the same phone hotspot over Wi-Fi (`wlan0`).

**Before installing k3s, on every Pi:**

```bash
ip -4 addr show wlan0                   # note the IP and the subnet
nmcli -t -f NAME connection show --active   # name of the hotspot connection
# Pi Wi-Fi power saving adds big latency spikes; turn it off permanently:
sudo nmcli connection modify "<hotspot-connection>" 802-11-wireless.powersave 2
sudo nmcli connection up "<hotspot-connection>"
```

Also add `cgroup_memory=1 cgroup_enable=memory` to the end of the line in
`/boot/firmware/cmdline.txt`, then reboot.

**Check that the Pis can reach each other.** VXLAN carries all pod traffic, so
UDP 8472 matters most:

```bash
ping -c 5 <other-pi-ip>
nc -u -l 8472                          # on one Pi
echo hello | nc -u <that-pi-ip> 8472   # on another; "hello" should appear
```

> ⚠️ **Subnet clash:** k3s uses `10.42.0.0/16` for pods and
> `10.43.0.0/16` for Services. Some Android hotspots hand out random `10.x.x.x`
> subnets. If yours falls inside either range, add
> `cluster-cidr: 10.52.0.0/16` and `service-cidr: 10.53.0.0/16` to the server
> config below, and pass the same values to `baseline/collect.py`.

**Install k3s.** Pinning the IP and interface stops k3s from picking the wrong one.

```bash
# control-plane Pi
sudo mkdir -p /etc/rancher/k3s
sudo tee /etc/rancher/k3s/config.yaml <<EOF
node-ip: <cp-ip>
flannel-iface: wlan0
tls-san: [<cp-ip>]
write-kubeconfig-mode: "0644"
EOF
curl -sfL https://get.k3s.io | sh -
sudo cat /var/lib/rancher/k3s/server/node-token

# each worker Pi
sudo mkdir -p /etc/rancher/k3s
sudo tee /etc/rancher/k3s/config.yaml <<EOF
node-ip: <this-pi-ip>
flannel-iface: wlan0
EOF
curl -sfL https://get.k3s.io | K3S_URL=https://<cp-ip>:6443 K3S_TOKEN=<token> sh -

# control-plane Pi: all three Ready, INTERNAL-IP = the hotspot IPs
kubectl get nodes -o wide
```

**Hotspot caveats:**
- **Keep the hotspot on, and turn off the phone's "turn off hotspot when idle" setting.** If it restarts, the phone
  may hand out new IPs or even a new subnet. If IPs change, update `node-ip` (and
  the agents' `K3S_URL` in `/etc/systemd/system/k3s-agent.service.env`), then run
  `sudo systemctl restart k3s` / `k3s-agent`.
- **Every image pull uses mobile data.** The diag toolbox image (`nicolaka/netshoot`)
  is a few hundred MB per node.
- **You SSH in over the same Wi-Fi you'll be breaking.** Always give node-level
  faults a timed auto-revert.

## 1. Deploy the app

```bash
# label the two worker Pis
kubectl label node <db-pi-name>      role=database
kubectl label node <backend-pi-name> role=backend

# build the image ON the backend Pi (so it's arm64) and load it into k3s
cd app
docker build -t backend-api:latest .
docker save backend-api:latest | sudo k3s ctr images import -

kubectl apply -f k8s/app.yaml
kubectl get pods -o wide
```

The backend *prefers* the `role=backend` Pi (node affinity), so normally every
request crosses nodes. It may move to the DB Pi when `agent/reroute.py` finds the
backend Pi's path to the DB broken (section 8), so **import the image on both workers**:

```bash
# on the backend Pi, after docker build:
docker save backend-api:latest | ssh <user>@<db-pi> sudo k3s ctr images import -
```

## 2. Check it works

| Check | Command | Expected |
|---|---|---|
| Pods are on different workers and Ready | `kubectl get pods -o wide` | `database` on the DB Pi, `backend` on the backend Pi, both `1/1` |
| DB Service has a ready endpoint | `kubectl get endpointslices -l kubernetes.io/service-name=db-service` | one address on port 5432 |
| Cross-node request works | `curl http://<backend-pi-ip>:30080/` | `{"value":"hello world", "elapsed_ms":..., "backend_node":...}` |
| Structured logs | `kubectl logs deploy/backend` | one JSON line per request (`db_ok` / `db_error`) |

## 3. Run the prober

Run it from the **control-plane Pi or a laptop**, not from a worker, so it sees
the app as a user would. It uses only the Python standard library.

```bash
python3 prober/prober.py --url http://<backend-pi-ip>:30080/ --out probes.jsonl
```

```
00:00:01  OK                                     41.2 ms
--- state change: OK -> FAIL backend->db [timeout]
00:00:05  FAIL backend->db [timeout]           3019.9 ms  connection to server at "db-service" ... timeout expired
```

Each line in `probes.jsonl` records `ok`, `latency_ms`, `where`, `phase`, `hint`, and `error`:

- **`where`** shows which hop failed. `prober->backend` means the NodePort or
  backend pod is unreachable. `backend->db` means the backend is up but its
  cross-node call to Postgres failed.
- **`phase`** shows whether the failure happened while connecting (network, DNS,
  or Service) or while querying (the DB itself).
- **`hint`** is a first guess at the cause, based on the error text:

| hint | Meaning | Likely causes |
|---|---|---|
| `dns` | Name didn't resolve | Wrong namespace or hostname, CoreDNS down |
| `refused` | Nothing listening, or something actively rejected the connection | Wrong `targetPort`, no endpoints, **NetworkPolicy** (k3s REJECTs denied traffic) |
| `no_route` | Rejected before connecting | No endpoints (kube-proxy REJECT), broken routing |
| `timeout` | Packets silently disappeared | Firewall DROP (VXLAN UDP 8472), MTU, overlay, packet loss |

### Smoke test: a fault the setup should survive

```bash
kubectl scale deploy database --replicas=0   # prober: FAIL backend->db
kubectl get pods                             # backend must stay 1/1 Ready
kubectl scale deploy database --replicas=1   # prober recovers to OK after ~10–20 s
```

## 4. Record the healthy baseline

Run this on the control-plane Pi, with the app deployed and the prober **not** running:

```bash
python3 baseline/collect.py
```

It deploys `k8s/diag.yaml`, which runs network toolbox pods on every node. Then it measures
the backend Pi → DB Pi path at three levels:

| Path | What it tells you |
|---|---|
| node → node | The hotspot link itself |
| pod → pod (cross-node) | The same link inside the Flannel VXLAN overlay |
| pod → pod (same node) | Control case: traffic never leaves the node |

For each path it records loss, RTT, jitter, path MTU, and iperf3 throughput. It also measures
DNS lookup time and app latency (p50/p95/p99), and collects node facts: MTUs,
VXLAN port and interface, Wi-Fi signal, conntrack usage. It warns about mistakes it can
detect on its own, like a subnet overlap or VXLAN bound to the wrong interface.

```
path                     loss    rtt avg   jitter  path MTU    throughput
node -> node             0.0%    4.5 ms   1.9 ms      1500   42.1 Mbit/s
pod -> pod (cross)       0.0%    5.9 ms   1.9 ms      1450   37.7 Mbit/s
...
```

Expect the pod path MTU to be 50 bytes smaller than the node's. That's the VXLAN
header, and it's the reference for the MTU fault (`mtu_blackhole`) later.

Results go to `baseline/results/<timestamp>/`: `summary.json` plus `raw/` with every
command and its output. **Run it at least 3 times, at different times of day and with
the phone in its usual spot.** Hotspot Wi-Fi varies, and the agent needs to know what
"normal" noise looks like. Commit the results.

## 5. Group 1: the Kubernetes service path (discovery & DNS)

The agent follows the path the backend's connection takes, and reports the
first broken link:

```
DB_HOST ──DNS──► Service ──selector──► Pods ──readiness──► Endpoints ──targetPort──► Postgres
```

| Label | How it's detected |
|---|---|
| `dns_coredns_down` | CoreDNS Deployment has 0 ready replicas, or `kube-dns` has no endpoints |
| `wrong_service_name` | The Service named by the backend's `DB_HOST` doesn't exist (suggests similar names) |
| `selector_mismatch` | The selector matches no pods but a pod with near-identical labels exists (a typo, e.g. `app=databse`), or it matches only the backend itself. If nothing similar exists, the database is missing: that is `other`, not a selector problem |
| `no_ready_endpoints` | Pods match but none are Ready (with probe events), or the workload is scaled to 0 |
| `port_mismatch` | `targetPort` is not a port the pod declares, **and** TCP to it is refused while a declared port is open |
| `other` | Anything the agent doesn't cover: the app fails but every check (group 1 and group 2) passes; or the diagnosis itself could not finish (API unreachable, `kubectl` error) |
| `healthy` | The app was seen answering (HTTP 200) and every check passes |

Most checks compare Kubernetes objects. The port check also runs an active
TCP test from the diag pod on the backend's node.

**Lesson from the first real run:** "connection refused" does *not* prove a
wrong port. k3s's NetworkPolicy controller (kube-router) **REJECTs** denied
traffic, and the client sees the same `ECONNREFUSED`. The first evaluation
labelled the `netpol` fault as `port_mismatch`. The agent now needs positive
evidence before it says `port_mismatch`: the targetPort isn't declared, or
another port on the same pod is open. If the port is right but traffic is
refused or times out, the result is `other`, and the evidence lists any
NetworkPolicies that select the DB pod.

```bash
python3 agent/diagnose.py                    # human-readable report
python3 agent/diagnose.py --json             # for scripts
```

**Inject a fault by hand** (all `kubectl`, nothing touches `wlan0`):

```bash
bash faults/fault.sh list
bash faults/fault.sh inject port
python3 agent/diagnose.py                    # -> port_mismatch
bash faults/fault.sh reset                   # undoes any fault
```

`netpol` is a fault the agent does *not* cover. It checks that the agent says
`other` rather than guessing one of its labels.

**Score the agent:**

```bash
python3 eval/run_diagnose.py --rounds 3      # group 1: ~8 faults × 3 rounds, about 20–30 min
```

Each trial injects the fault, waits, diagnoses (and times it), resets, and waits
until the cluster is healthy again. It also runs `none` trials, with no fault
injected, to count false alarms. The script prints a confusion matrix, accuracy,
per-fault results, and diagnosis time, and saves everything to
`eval/results/<timestamp>/`. That's the results table for the report.

### Results on the Pi cluster (8 scenarios × 3 rounds, random order)

| Run | Agent | Accuracy | Mean diagnosis time | Misses |
|---|---|---|---|---|
| [run 1](eval/results/20261105-005709/report.txt) | first version | 21/24 = 87.5% | 1.8 s | all 3 `netpol` → `port_mismatch` |
| [run 2](eval/results/20261105-011047/report.txt) | after the REJECT fix | **24/24 = 100%** | 2.9 s | none |

Run 2 is slower because the port check now makes extra TCP tests and lists
NetworkPolicies before it settles on a label.

Baseline for the same cluster: [baseline/results/](baseline/results/).
Node to node: 10.2 ms, MTU 1500, 14.9 Mbit/s. Pod to pod across nodes: 12.7 ms,
MTU 1450, 13.2 Mbit/s. Pod to pod on the same node: 0.06 ms.

## 6. Self-healing

Kubernetes heals *drift*: it restarts a crashed pod or recreates a deleted one.
It does **not** fix a wrong configuration. It faithfully runs 0 database replicas
or sends traffic to port 5433 because that's what it was told. `agent/heal.py`
closes that gap:

```
diagnose ──► safe, unambiguous fix? ──no──► escalate to a human (with the evidence)
                 │ yes
                 ▼
              apply ──► verify: does the app really answer (HTTP 200)? ──yes──► healed
                                     │ no, same fault after the timeout
                                     ▼
                               undo the change ──► failed (escalate)
```

| Fault | Fix applied automatically |
|---|---|
| CoreDNS scaled to 0 | `scale deploy/coredns --replicas=1` |
| wrong `DB_HOST` | `set env DB_HOST=<the one similar Service that exposes the port>` |
| selector typo | patch the selector to the closest pod labels, only if it is a clear winner, at most 2 characters off (a typo, not `database-old`), serves the Service's port, and is never the backend |
| DB scaled to 0 | scale back to the replica count last applied |
| readiness failing after a change | `rollout undo --to-revision=<previous>`, only if the current revision **never** became ready |
| wrong `targetPort` | patch it to the port seen open that is clearly the database's (same number or name as the Service port, or the only open one); otherwise escalate |
| NetworkPolicy / anything `other` | **never touched.** It may be intentional security, so it's escalated |

Safety rules: act only on a `high`-confidence diagnosis of a covered fault with an
unambiguous fix; one change at a time; "fixed" means the app answers again,
not that `kubectl` succeeded; if the run ends without a working app, undo every change it made, newest first (so the cluster ends up working or as it was); give
young pods a grace period before rolling back. Every run is appended to
`heal_log.jsonl`.

```bash
python3 agent/heal.py --dry-run     # show the plan only
python3 agent/heal.py               # heal for real
python3 eval/run_heal.py --rounds 3 # score it: heal success, time to recover
```

### Results on the Pi cluster, group 1 (8 scenarios × 3 rounds, random order)

| Run | Result | Misses |
|---|---|---|
| [healing](eval/results/heal-20261105-023713/report.txt) | 21/24 = 87.5%: 15/18 faults healed, 3/3 `netpol` escalated untouched, 3/3 healthy runs untouched, 0 rollbacks | all 3 `readiness`: escalated instead of rolled back |

The `readiness` misses led to the "never Ready since the pods started" check
for the rollback. That fix has **not** been re-run on the cluster yet.

## 7. Group 2: the cross-node network

Group 2 looks *below* Kubernetes, at the path a packet takes between the two Pis.
It runs only when every group-1 check passed ([agent/network.py](agent/network.py)):

```
backend pod (user1) ─cni0─► flannel.1 ══ VXLAN, UDP 8472, over wlan0 (hotspot) ══► flannel.1 ─cni0─► DB pod (user2)
                            └──────────────── overlay (pod → pod) ────────────────┘
                     └───────────────────────── underlay (node → node) ──────────────────────────┘
```

The key idea is to **compare layers and compare with the baseline**:

| Measurement | Healthy (baseline) | What a deviation means |
|---|---|---|
| node → node ping (underlay) | 0% loss | 100% loss: the Wi-Fi itself is down (`other`, a human must look) |
| pod → pod ping (overlay) | 0% loss, 12.7 ms | ≥90% loss while the underlay works: tunnel broken (route or firewall) |
| | | high loss / RTT while the underlay is fine: shaping on the tunnel |
| DF ping at 1450 bytes | passes | fails while small packets pass: MTU blackhole |
| bulk TCP (iperf3) | ~13 Mbit/s | stalls: the MTU problem actually hurts |

| Label | Injected (on the overlay only) | Detected by | Healed by |
|---|---|---|---|
| `overlay_blocked` | `iptables DROP udp/8472` on the DB node | overlay down + a DROP/REJECT rule on 8472 that applies to the nodes (not one limited to other addresses) | deleting that rule |
| `route_missing` | `ip route del` of the DB node's pod network on the backend node | `ip route get <db pod>` doesn't use `flannel.1` | `ip route replace … dev flannel.1 onlink` |
| `mtu_blackhole` | drop inner packets > 1200 bytes (an MTU-limited link with no ICMP) | DF-ping path MTU 1200 < 1450, bulk TCP stalls | **TCP MSS clamping** to the measured path (1160) on both nodes |
| `packet_loss` | `tc netem loss 40%` | pod loss ≫ baseline, underlay clean, netem loss rule on `flannel.1`/`cni0` | removing the netem rule (one on `wlan0` is only reported) |
| `high_latency` | `tc netem delay 150ms` | pod RTT ≫ baseline, underlay clean, netem delay rule on `flannel.1`/`cni0` | removing the netem rule (one on `wlan0` is only reported) |

Faults are injected **only on `flannel.1` / UDP 8472, never on `wlan0`**, so SSH
and the Kubernetes API keep working. Each one also schedules its own undo after
5 minutes.

```bash
bash faults/fault.sh inject mtu
python3 agent/diagnose.py         # -> mtu_blackhole
python3 agent/heal.py             # -> clamps MSS, verifies bulk TCP works again
bash faults/fault.sh reset
python3 eval/run_diagnose.py --group 2 --rounds 3   # score detection
python3 eval/run_heal.py --group 2 --rounds 3       # score healing
python3 eval/run_diagnose.py --group all --rounds 3 # both groups at once
```

### Results on the Pi cluster (5 faults + healthy × 3 rounds, random order)

| Run | Result | Time |
|---|---|---|
| [detection](eval/results/group2-20261105-030339/report.txt) | **18/18 = 100%** | mean 14.1 s per diagnosis |
| [healing](eval/results/heal-group2-20261105-031405/report.txt) | **18/18 = 100%**: 15/15 faults healed, 3/3 healthy runs untouched, 0 rollbacks | fix → app OK: mean 10.4 s (7.3 s for rule/route fixes, 23 s for the MSS clamp) |

**Findings from the real cluster:**
- **Flannel does not restore a deleted route by itself.** It was still missing after 45 s, so this is a failure Kubernetes doesn't heal.
- **On k3s, the underlay vs overlay comparison is what separates the causes.** During the loss fault, node → node stayed at 0% while pod → pod lost 30%.
- **The MTU fix is a mitigation, the way it's done in real networks.** The small-MTU path stays, but TCP now negotiates segments that fit through it.
- **Safety rules learned from review:**
  - a fix that *removed* a fault (a rule, a qdisc, a route) is never rolled back, because that would re-create the fault;
  - loss and latency are confirmed with a second sample, so a momentary hotspot stall isn't a false alarm;
  - loss or latency with **no** configuration cause is reported as the physical link and never "fixed".

## 8. Path-aware rescheduling (`agent/reroute.py`)

Sections 5–7 find the **cause** and repair it. This agent does something simpler and
cause-agnostic: it keeps the backend on a node that **can really reach the database**.

**The gap it closes.** Kubernetes places pods by CPU, memory, labels and taints; it never
checks whether the network path a pod needs works. When the backend Pi stays `Ready` but
its path to the DB breaks, the liveness probe restarts the backend **on the same node**
(still broken), and a readiness probe would only remove it from the Service. Nothing
moves it. The node stays `Ready` because the kubelet talks to the API server over `wlan0`,
not over the VXLAN overlay.

### The rules (all fixed numbers, no learning, no guessing)

| # | Rule | Why | Borrowed from |
|---|---|---|---|
| 1 | Every 5 s the diag-pod on **every node** does what the backend does: DNS lookup of `db-service` (must return its ClusterIP), TCP connect to `ClusterIP:5432`, 10 pings to the DB pod (loss ≥ 30% or RTT > 100 ms fails), 3 DF pings of 1400 bytes (MTU) | Test the real path, not a stand-in, so *any* cause that breaks it is caught | Goldpinger, Pingmesh |
| 2 | A node is **BAD** after 3 failed rounds in a row (~15 s), **GOOD** again after 12 passed rounds (~60 s). If a probe can't run, the round is *unknown* and changes nothing | Hotspot Wi-Fi drops single probes; asymmetric thresholds because moving back too early costs an outage, staying too long costs only balance (hysteresis) | k8s probe `failureThreshold`/`successThreshold`, RFC 2439 |
| 3 | A node that went BAD 3 times in 10 min is **flapping** and needs 120 clean rounds (~10 min) | Don't move pods back and forth on a flaky link | RFC 2439 route flap damping |
| 4 | **No action** if no node is GOOD, if the DB pod isn't Running+Ready, or if no GOOD node is allowed to run the backend | Can't localize or there is nowhere better to go; the problem may be the probes themselves | medik8s Node Health Check `minHealthy` |
| 5 | Taint BAD nodes `kuberselfheal.io/no-db-path=true:NoSchedule`, then delete **one** backend pod on a BAD node per round; max one eviction per node per 2 min | The Deployment recreates the pod; the taint stops the scheduler putting it back on the broken node | Draino, descheduler `RemovePodsViolatingNodeTaints` |
| 6 | Within 60 s the backend must be Ready on a GOOD node **and** the app must answer HTTP 200. Otherwise alert and stop evicting until every node is GOOD | Worst case is "a human is called", never a loop of actions | medik8s escalation |
| 7 | When a node is GOOD again its taint is removed; when no node is BAD, `rollout restart` the backend once so it returns to its preferred node | Kubernetes never rebalances by itself | medik8s removes remediation when healthy |

The log also says *where* the fault probably is (only the backend node fails → that node's
side; every node except the DB's fails → the DB node's side). Actions never depend on that
guess. With 3 nodes "node X is broken" and "both of X's links are broken" look the same,
and both lead to the same action.

Moving next to the DB never lowers availability: on the DB's node the backend needs
`A(DB node)`; elsewhere it needs `A(node) × A(link) × A(DB node)`, which is never larger.
The DB is already a single point of failure, so co-location adds no new way to fail.

### Setup (once)

```bash
kubectl apply -f k8s/app.yaml       # backend now PREFERS role=backend instead of requiring it,
                                    # never the control plane; DB tolerates the reroute taint
kubectl apply -f k8s/diag.yaml      # diag pods tolerate every taint, so probing continues
# the backend image must be on BOTH workers (section 1)
```

### Run and test it

```bash
python3 eval/test_reroute_logic.py   # offline test of the rules with a fake cluster (no Pis)
python3 agent/reroute.py --once      # one probe round from every node: all should be "pass"
python3 agent/reroute.py --dry-run -v   # full loop, prints the table, changes nothing
python3 agent/reroute.py             # live (add --kubectl "sudo k3s kubectl" if needed)
```

By hand, in a second terminal (point clients at the **DB node's** IP, see the limits below):

```bash
python3 prober/prober.py --url http://<db-pi-ip>:30080/ --out probes.jsonl   # third terminal
bash faults/fault.sh inject route    # backend Pi loses its route to the DB Pi's pods
kubectl get pods -o wide -w          # backend should move to the DB Pi within ~30-40 s
bash faults/fault.sh reset           # ~60 s later: taint removed, backend moves back
```

Scored, with the agent running, then again with it stopped (the "Kubernetes alone" baseline):

```bash
python3 eval/run_reroute.py --url http://<db-pi-ip>:30080/ --rounds 3
python3 eval/run_reroute.py --url http://<db-pi-ip>:30080/ --rounds 3 --label no-agent
```

What we **expect**, to be confirmed on the cluster:

| Fault | Expected | Reason |
|---|---|---|
| `route` (backend Pi) | rerouted | only the backend Pi fails; the DB Pi is fine |
| `mtu`, `delay` (DB Pi's `flannel.1`) | rerouted | the other nodes fail; the DB Pi reaches its own DB without the overlay |
| `loss` (DB Pi) | rerouted, maybe slower | 40% loss fails the ≥30% rule in most rounds |
| `fw-vxlan` (DB Pi) | **escalated** | CoreDNS runs on the master, so the DB Pi's DNS replies arrive over the blocked VXLAN too: no node passes, so the agent correctly does nothing |
| `none` | no action | false-alarm check |

### Limits (say these in the report)

- **It doesn't repair the cause.** It keeps the app working and reports the bad path;
  `diagnose.py` / `heal.py` explain and repair the cause.
- **DB node down, or the whole Wi-Fi down:** every node fails → alert only.
- **Clients must use the DB node's IP.** With NodePort, a request entering a node whose
  path to the backend is broken still fails (`externalTrafficPolicy: Local` is the general fix).
- **The 30% / 100 ms / 3-round numbers are chosen, not proven.** Wi-Fi losses are bursty,
  so the false-alarm rate must be measured (`none` trials, and a long run with no faults).
- **Don't run `heal.py` and `reroute.py` at the same time.** Both act on the cluster.

## Design notes

- **The backend's probes check only the process (`/healthz`), not the DB.** If
  readiness depended on the DB, a cross-node fault would remove the backend from
  its Service. The prober would then only see "backend unreachable" and lose the
  DB error that identifies the broken layer.
- **gunicorn runs threaded** (`gthread`, 4 threads), so `/healthz` still answers
  while a request waits out the 3 s DB timeout. Otherwise probes would fail
  during network faults and the pod would be restarted.
- **The Postgres readiness check uses `pg_isready -h 127.0.0.1` (TCP).** During
  first-start init, Postgres listens only on its Unix socket, so the pod stays
  NotReady until it can really accept connections.
- **Postgres liveness is deliberately lenient.** A DB restart in the middle of an
  experiment would hide the injected fault.
- **`NODE_NAME` and `POD_NAME` are injected** using the Kubernetes downward API,
  so every log line and response says which node it came from.
