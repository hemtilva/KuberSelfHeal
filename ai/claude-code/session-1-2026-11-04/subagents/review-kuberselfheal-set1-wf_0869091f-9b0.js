export const meta = {
  name: 'review-kuberselfheal-set1',
  description: 'Independent review of the set-1 agent, fault injector, eval runner and baseline for correctness bugs, then adversarial verification',
  phases: [
    { title: 'Review', detail: '3 reviewers, one per area' },
    { title: 'Verify', detail: 'one skeptic per finding tries to refute it' },
  ],
}

const ROOT = 'D:/IIT/SEM-V/CN/KuberSelfHeal'
const RULES = `HARD RULES: Read-only. Use only Read/Grep/Glob on files under ${ROOT}. Do NOT run ssh, kubectl, python, or any command that touches the network or the Raspberry Pi cluster, and do NOT edit any file. A live evaluation is running on the cluster right now.

Context: a CN course project. 3 Raspberry Pis run k3s v1.36 (Flannel VXLAN over wlan0 on a phone hotspot, kube-proxy iptables, CoreDNS, kube-router NetworkPolicy). App: Flask backend on node user1 reads Postgres on node user2 via Service db-service. The agent diagnoses "problem set 1" (Service discovery & DNS) faults and labels them; anything else is "other". Real-cluster facts already confirmed: healthy -> healthy, selector fault -> selector_mismatch, port fault -> port_mismatch all work.`

const FINDINGS = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          file: { type: 'string' },
          line: { type: 'integer' },
          title: { type: 'string' },
          failure_scenario: { type: 'string', description: 'concrete cluster state / input -> wrong label, crash, or misleading measurement' },
          severity: { type: 'string', enum: ['high', 'medium', 'low'] },
          suggested_fix: { type: 'string' },
        },
        required: ['file', 'line', 'title', 'failure_scenario', 'severity', 'suggested_fix'],
      },
    },
  },
  required: ['findings'],
}

const VERDICT = {
  type: 'object',
  properties: {
    real: { type: 'boolean' },
    reasoning: { type: 'string' },
    corrected_fix: { type: 'string' },
  },
  required: ['real', 'reasoning'],
}

const AREAS = [
  { key: 'agent', prompt: `Review ${ROOT}/agent/diagnose.py (and the hint table in ${ROOT}/prober/prober.py it imports) for CORRECTNESS bugs against real Kubernetes/k3s semantics. Focus: could any of the 7 faults in ${ROOT}/faults/fault.sh get the wrong label on a real cluster (consider timing right after injection, rollouts, terminating pods, EndpointSlice ready/serving/terminating conditions, multiple EndpointSlices, named vs numeric targetPort, kubectl exec failures, curl/nc output differences in nicolaka/netshoot v0.13, CoreDNS down making the agent's own lookups fail)? Could a healthy cluster be labelled as a fault (false alarm)? Could the netpol fault be mislabelled as a set-1 label instead of "other"? Report only real bugs with a concrete failure scenario, not style.` },
  { key: 'faults-eval', prompt: `Review ${ROOT}/faults/fault.sh and ${ROOT}/eval/run_set1.py together with ${ROOT}/k8s/app.yaml for CORRECTNESS bugs. Focus: does every inject actually produce the intended broken state on k3s (e.g. Recreate strategy + readiness patch, set env rollout, coredns scale in kube-system, NetworkPolicy enforced by kube-router)? Does reset fully restore EVERY fault (kubectl apply three-way merge after kubectl patch/scale/set env; CoreDNS replicas; leftover netpol)? Race conditions between reset and the next trial; wait_healthy logic; the settle time; trials where the label could be right/wrong for the wrong reason; confusion-matrix/accuracy computation bugs. Report only real bugs with concrete failure scenarios.` },
  { key: 'baseline', prompt: `Review ${ROOT}/baseline/collect.py and ${ROOT}/k8s/diag.yaml for bugs that would produce WRONG or MISLEADING measurements (the numbers go into a course report). Real output for reference is in ${ROOT}/baseline/results/ (summary.json and raw/*.txt) — check the parsers against those raw outputs. Focus: ping/iperf3/dig/path-MTU parsing, the binary search, units, percentile, which interface/IP is measured, whether the "pod -> pod (same)" control truly stays on-node, the CIDR-overlap and VXLAN checks, and anything the raw files show that the parser mis-reads. Report only real bugs with concrete evidence.` },
]

phase('Review')
const results = await pipeline(
  AREAS,
  a => agent(`${RULES}\n\n${a.prompt}`, { label: `review:${a.key}`, phase: 'Review', schema: FINDINGS }),
  (r, a) => parallel((r?.findings || []).map(f => () =>
    agent(`${RULES}\n\nA reviewer claims this bug. Your job is to REFUTE it by reading the actual code (and raw result files if relevant). Default to real=false if the scenario cannot actually happen on k3s v1.36 with this code, is already handled elsewhere, or is only style. If it IS real, say so and give the minimal correct fix.\n\nFile: ${f.file}:${f.line}\nTitle: ${f.title}\nScenario: ${f.failure_scenario}\nProposed fix: ${f.suggested_fix}`,
      { label: `verify:${a.key}:${f.line}`, phase: 'Verify', schema: VERDICT })
      .then(v => ({ area: a.key, ...f, verdict: v })))),
)
const all = results.flat().filter(Boolean)
log(`${all.length} findings reviewed; ${all.filter(f => f.verdict?.real).length} survived verification`)
return { confirmed: all.filter(f => f.verdict?.real), rejected: all.filter(f => !f.verdict?.real).map(f => ({ area: f.area, file: f.file, line: f.line, title: f.title, why: f.verdict?.reasoning })) }
