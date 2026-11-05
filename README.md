# KuberSelfHeal

**Diagnosing and self-healing cross-node pod connectivity failures on a Raspberry Pi Kubernetes cluster.**

A backend pod on one node reads from a PostgreSQL pod on another node of a
3× Raspberry Pi 5 k3s cluster connected over a phone Wi-Fi hotspot. When that
connection breaks, our agent finds which layer is broken, labels the cause with
evidence, applies a fix only when it is safe and unambiguous, checks that the
application answers again (HTTP 200), and undoes the change if it did not help.
It covers ten faults in two groups; **anything else is labelled `other`** and
handed to a human instead of being guessed at.

| Group | Layer | Faults the agent detects and heals |
|---|---|---|
| 1 | Kubernetes service path (service discovery & DNS) | CoreDNS down, wrong Service name, selector mismatch, no ready endpoints, port mismatch |
| 2 | Cross-node network | VXLAN blocked by a firewall rule, missing route, MTU black hole, packet loss, high latency |

## Team

| Name | Roll No. |
|---|---|
| Hem Tilva | 24110374 |
| Vansh Barfiwala | 24110380 |
| Pratham Choksi | 24110275 |
| Shivangi Thaker | 24110371 |
| Jils Shah | 24110148 |

## Repository layout

```
README.md   this file
/code       all source code (agent, healer, test app, Kubernetes manifests, fault
            injection, baseline, evaluation scripts and recorded results)
/report     final report (PDF; the .docx and the scripts that build it are next to it)
/ppt        presentation slides
/ai         all AI prompts and conversations (raw transcripts)
```

## Results on the Pi cluster

| Run | Result |
|---|---|
| Detection, group 1 (8 scenarios × 3, incl. no-fault and an uncovered NetworkPolicy) | 24/24 correct labels |
| Detection, group 2 (6 scenarios × 3, incl. no-fault) | 18/18 correct labels |
| Healing, both groups | 30 of 33 injected faults healed; 0 changes in 9 no-fault / uncovered-fault runs |

The three healing misses are the readiness fault in the first version; its fix
has not yet been re-run on the cluster. Details and raw data:
[code/eval/results/](code/eval/results/) and the report.

## How to run

The full setup (cluster on a hotspot, deploying the app, baseline, fault
injection, evaluation) is in [code/README.md](code/README.md). In short, on the
control-plane Pi:

```bash
cd code
kubectl apply -f k8s/app.yaml
python3 baseline/collect.py                          # deploys the toolbox pods, records the baseline
python3 agent/diagnose.py                            # diagnose the current state
python3 agent/heal.py --dry-run                      # show what it would fix
python3 eval/run_diagnose.py --group all --rounds 3  # score detection
python3 eval/run_heal.py --group all --rounds 3      # score healing
```

## AI usage

We used AI (Claude Code with Claude Opus 5.5) to write the code, review it,
and draft the report. The raw conversations are in [ai/](ai/).
