# KuberSelfHeal

Diagnosing why a connection between pods on two Kubernetes nodes fails, on a
3× Raspberry Pi k3s cluster (1 control plane + 2 workers).

The test app is a backend pod on one worker that reads from Postgres on the
other worker, so every request crosses between the two nodes. It's based on
[prathamchoksi/CN_Project_App](https://github.com/prathamchoksi/CN_Project_App).

```
app/        Flask backend (Dockerfile, app.py)
k8s/        app.yaml (the test app), diag.yaml (network toolbox pods on every node)
prober/     Probes the backend continuously and records failures (JSONL)
baseline/   collect.py records the healthy network; results/ holds the runs
faults/     fault.sh injects / resets the faults of the current problem set
agent/      diagnose.py: finds and labels the cause of a failed connection
eval/       run_set1.py: injects every fault N times and scores the agent
```

**Scope.** We work through the 60 problems one *set* at a time. The agent
labels anything outside the current set as `other`.

| Set | Layer | Problems | Status |
|---|---|---|---|
| 1 | Service discovery & DNS | 7, 8, 9, 12, 14 | implemented |
| 2 | Cross-node network (firewall, MTU, routing, loss) | 2, 4, 5, 28 | next |

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

> ⚠️ **Subnet clash (problem 6):** k3s uses `10.42.0.0/16` for pods and
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

The image is loaded only into the backend Pi's container runtime, so the
backend pod has to run there. Its `nodeSelector` makes sure it does.

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
| `refused` | Reached a host, nothing listening | Wrong `targetPort`, no endpoints |
| `no_route` | Rejected before connecting | No endpoints (kube-proxy REJECT), broken routing |
| `timeout` | Packets disappeared | Firewall (VXLAN UDP 8472), MTU, NetworkPolicy, overlay, packet loss |

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
header, and it's the reference for the MTU fault (problem 5) later.

Results go to `baseline/results/<timestamp>/`: `summary.json` plus `raw/` with every
command and its output. **Run it at least 3 times, at different times of day and with
the phone in its usual spot.** Hotspot Wi-Fi varies, and the agent needs to know what
"normal" noise looks like. Commit the results.

## 5. Problem set 1: Service discovery & DNS

The agent follows the path the backend's connection takes, and reports the
first broken link:

```
DB_HOST ──DNS──► Service ──selector──► Pods ──readiness──► Endpoints ──targetPort──► Postgres
   14 / 12              7                       8                         9
```

| Label | Problem | How it's detected |
|---|---|---|
| `dns_coredns_down` | 14 | CoreDNS Deployment has 0 ready replicas, or `kube-dns` has no endpoints |
| `wrong_service_name` | 12 | The Service named by the backend's `DB_HOST` doesn't exist (suggests similar names) |
| `selector_mismatch` | 7 | The selector matches 0 pods, and no workload is meant to provide them; shows the closest labels |
| `no_ready_endpoints` | 8 | Pods match but none are Ready (with probe events), or the workload is scaled to 0 |
| `port_mismatch` | 9 | TCP to `podIP:targetPort` is **refused**, while the pod's declared port is open |
| `other` | – | The app fails but every check above passes, so the cause is in another layer |
| `healthy` | – | The app works and every check passes |

Most checks compare Kubernetes objects (category A). The port check also runs
an active TCP test (category C) from the diag pod on the backend's node. A
**timeout**, as opposed to a refusal, is never labelled a port problem: it means
packets are being dropped, which belongs to set 2.

```bash
python3 agent/diagnose.py                    # human-readable report
python3 agent/diagnose.py --json             # for scripts
```

**Inject a fault by hand** (all `kubectl`, nothing touches `wlan0`):

```bash
bash faults/fault.sh list
bash faults/fault.sh inject port
python3 agent/diagnose.py                    # -> port_mismatch (problem 9)
bash faults/fault.sh reset                   # undoes any fault
```

`netpol` is a fault *outside* set 1. It checks that the agent says `other`
rather than guessing a set-1 label.

**Score the agent:**

```bash
python3 eval/run_set1.py --rounds 3          # ~8 faults × 3 rounds, about 20–30 min
```

Each trial injects the fault, waits, diagnoses (and times it), resets, and waits
until the cluster is healthy again. It also runs `none` trials, with no fault
injected, to count false alarms. The script prints a confusion matrix, accuracy,
per-fault results, and diagnosis time, and saves everything to
`eval/results/<timestamp>/`. That's the results table for the report.

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
