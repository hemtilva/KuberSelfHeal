export const meta = {
  name: 'review-set2-network',
  description: 'Adversarial review of problem set 2 (cross-node network) detection, healing and fault injection',
  phases: [
    { title: 'Review', detail: 'detection logic, healing safety, fault/reset scripts' },
    { title: 'Verify', detail: 'one skeptic per finding' },
  ],
}

const ROOT = 'D:/IIT/SEM-V/CN/KuberSelfHeal'
const RULES = `HARD RULES: Read-only. Use only Read/Grep/Glob on files under ${ROOT}. Do NOT run ssh, kubectl, python or any command; do NOT edit files. A live evaluation is running on the cluster.

Context: CN course project, 3 Raspberry Pis on a phone Wi-Fi hotspot, k3s v1.36 with Flannel VXLAN (flannel.1, UDP 8472) over wlan0, iptables nf_tables backend, kube-router NetworkPolicy. Backend on user1, Postgres on user2, CoreDNS currently on user1. Problem set 2 = cross-node network faults. ${ROOT}/agent/network.py detects them (called from ${ROOT}/agent/diagnose.py only when all set-1 checks pass); ${ROOT}/agent/heal.py applies "node_cmd" actions through privileged hostNetwork diag-host pods (${ROOT}/k8s/diag.yaml, image nicolaka/netshoot:v0.13); ${ROOT}/faults/fault.sh injects fw-vxlan / mtu / route / loss / delay and reset_network undoes them. Baseline in ${ROOT}/baseline/results/*/summary.json (pod->pod 12.7 ms, 0% loss, MTU 1450; parsers in ${ROOT}/baseline/collect.py). ALREADY VERIFIED LIVE on the Pis: each of the 5 faults is diagnosed with the right label and healed (app OK again); healthy cluster -> healthy; Flannel does NOT restore a deleted route by itself.`

const FINDINGS = { type: 'object', properties: { findings: { type: 'array', items: { type: 'object', properties: {
  file: { type: 'string' }, line: { type: 'integer' }, title: { type: 'string' },
  failure_scenario: { type: 'string' }, severity: { type: 'string', enum: ['high', 'medium', 'low'] }, suggested_fix: { type: 'string' } },
  required: ['file', 'line', 'title', 'failure_scenario', 'severity', 'suggested_fix'] } } }, required: ['findings'] }
const VERDICT = { type: 'object', properties: { real: { type: 'boolean' }, reasoning: { type: 'string' }, corrected_fix: { type: 'string' } }, required: ['real', 'reasoning'] }

const AREAS = [
  { key: 'detect', prompt: `Review DETECTION in ${ROOT}/agent/network.py (and how diagnose.py calls it). Look for wrong labels or false alarms on a real hotspot: thresholds vs Wi-Fi jitter (single 30-ping sample, LOSS_MIN_PCT, LATENCY_EXTRA_MS, baseline loading picks newest dir by name), ordering (loss checked before MTU, MTU DF-ping retries under mild loss), the route check via 'ip route get' (both directions; client_pod_ip from diag-pod), the firewall scan (only filter table INPUT? -S lists all chains; REJECT vs DROP; rules in raw/OUTPUT/FORWARD), the netem regex against real 'tc qdisc show' output ("qdisc netem 8001: dev flannel.1 root refcnt 2 limit 1000 loss 40% seed 1318..."), iperf3 stall handling, behaviour when CoreDNS sits on the far node, and when the backend and DB are on the same node. Report only concrete, realistic bugs.` },
  { key: 'heal', prompt: `Review HEALING SAFETY for set 2: node_cmd actions built in ${ROOT}/agent/network.py and executed by ${ROOT}/agent/heal.py (apply_node_cmd, inverse, verify). Can it make the network worse or lock the operator out (SSH over wlan0, k3s API 6443)? Consider: deleting a firewall rule that is legitimately someone's (any 8472 DROP is deleted), undo commands that are not the exact inverse (iptables -D vs -I position; tc 'seed' param in the re-add; 'ip route del' undo of a 'replace' that overwrote a different existing route), partial failure mid-way through several steps (no rollback of the steps already applied), the MSS clamp staying forever after the fault is gone, verification declaring 'healthy' while the MTU fault persists (intended: mitigated), and re-running heal when a clamp already exists (duplicate rules). Report only concrete problems.` },
  { key: 'faults', prompt: `Review ${ROOT}/faults/fault.sh set-2 injection and reset_network for correctness and SAFETY on the Pis: quoting through 'kubectl exec ... sh -c' (nested quotes in UNDO_* and auto_revert), whether every inject is fully undone by reset (including the healer's ksh-heal-mss clamp and routes restored with the right gateway form), idempotency, the pkill pattern, auto_revert surviving/firing correctly (nohup inside a hostPID privileged pod), set -e interactions (a failing on_node aborting reset halfway), whether any command could touch wlan0/SSH or the control-plane node, and the workers() label selector. Also check eval/run_set1.py and eval/run_heal.py --set handling. Report only concrete bugs.` },
]

phase('Review')
const results = await pipeline(
  AREAS,
  a => agent(`${RULES}\n\n${a.prompt}`, { label: `review:${a.key}`, phase: 'Review', schema: FINDINGS }),
  (r, a) => parallel((r?.findings || []).map(f => () =>
    agent(`${RULES}\n\nA reviewer claims the following. REFUTE it by reading the actual code. Default to real=false if it cannot actually happen with this code on this cluster, is already handled, contradicts the live-verified behaviour listed above, or is only hypothetical hardening without a concrete failure. If real, give the minimal correct fix.\n\nFile: ${f.file}:${f.line}\nTitle: ${f.title}\nScenario: ${f.failure_scenario}\nProposed fix: ${f.suggested_fix}`,
      { label: `verify:${a.key}:${f.line}`, phase: 'Verify', schema: VERDICT })
      .then(v => ({ area: a.key, ...f, verdict: v })))),
)
const all = results.flat().filter(Boolean)
log(`${all.length} findings; ${all.filter(f => f.verdict?.real).length} confirmed`)
return {
  confirmed: all.filter(f => f.verdict?.real).map(f => ({ area: f.area, file: f.file, line: f.line, severity: f.severity, title: f.title, scenario: f.failure_scenario, fix: f.verdict.corrected_fix || f.suggested_fix })),
  rejected: all.filter(f => !f.verdict?.real).map(f => ({ area: f.area, line: f.line, title: f.title })),
}
