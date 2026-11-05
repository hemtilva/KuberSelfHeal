export const meta = {
  name: 'review-self-heal',
  description: 'Adversarial safety+correctness review of the set-1 self-healer before it runs on the live Pi cluster',
  phases: [
    { title: 'Review', detail: 'safety, k3s semantics, eval scoring' },
    { title: 'Verify', detail: 'one skeptic per finding' },
  ],
}

const ROOT = 'D:/IIT/SEM-V/CN/KuberSelfHeal'
const RULES = `HARD RULES: Read-only. Use only Read/Grep/Glob on files under ${ROOT}. Do NOT run ssh, kubectl, python or any command; do NOT edit files.

Context: CN course project on 3 Raspberry Pis running k3s v1.36 (kubectl 1.36). App: backend Deployment (user1) -> Service db-service -> Postgres Deployment "database" (user2, strategy Recreate). ${ROOT}/agent/diagnose.py labels "problem set 1" faults and now attaches a structured "action" to findings; ${ROOT}/agent/heal.py applies the action, verifies the app answers again (HTTP 200 via the backend), and undoes the change if the fault persists. Faults come from ${ROOT}/faults/fault.sh; ${ROOT}/eval/run_heal.py scores healing (it imports helpers from ${ROOT}/eval/run_set1.py). Already verified with a stateful fake kubectl: each of the 6 set-1 faults heals, none/netpol make zero changes, a rollback path and a two-fault case work.`

const FINDINGS = { type: 'object', properties: { findings: { type: 'array', items: { type: 'object', properties: {
  file: { type: 'string' }, line: { type: 'integer' }, title: { type: 'string' },
  failure_scenario: { type: 'string', description: 'concrete cluster state -> wrong/harmful action, missed heal, or wrong score' },
  severity: { type: 'string', enum: ['high', 'medium', 'low'] }, suggested_fix: { type: 'string' } },
  required: ['file', 'line', 'title', 'failure_scenario', 'severity', 'suggested_fix'] } } }, required: ['findings'] }
const VERDICT = { type: 'object', properties: { real: { type: 'boolean' }, reasoning: { type: 'string' }, corrected_fix: { type: 'string' } }, required: ['real', 'reasoning'] }

const AREAS = [
  { key: 'safety', prompt: `SAFETY review of ${ROOT}/agent/heal.py and the "action" construction in ${ROOT}/agent/diagnose.py. Can the healer ever make things WORSE or touch something it shouldn't? Consider: acting on a transient state (mid-rollout, pod still starting, CoreDNS restarting), rolling back a deployment whose previous revision is also broken or is unrelated, undo-of-undo semantics of 'kubectl rollout undo', the inverse() of each action being truly the inverse, healing the wrong object (selector pointing at the client or at an unrelated pod), set_env on a deployment with several containers, the grace-period logic, max_fixes loop termination, exceptions mid-heal leaving a half-applied change without a log entry, and the 'changed'/'timeout' branches in verify(). Report only real, concrete problems.` },
  { key: 'k8s', prompt: `KUBERNETES/k3s SEMANTICS review of ${ROOT}/agent/heal.py command_for() and the actions built in ${ROOT}/agent/diagnose.py. Check each generated kubectl command is valid for kubectl 1.36 and does what is intended: json-patch 'replace /spec/selector' on a Service, 'replace /spec/ports/N/targetPort', 'kubectl set env deploy/X K=V' (which containers?), 'kubectl rollout undo deploy/X' (which revision does it go to, and how does the revision annotation behave after fault.sh's 'kubectl apply' reset and a previous undo?), 'scale' of k3s's coredns (is it an addon that k3s re-applies?), and 'rollout status' timeouts. Also check owner_deployment() and pod_age_s() against real object shapes (ownerReferences, status.startTime formats). Report only real bugs with concrete scenarios.` },
  { key: 'eval', prompt: `Review ${ROOT}/eval/run_heal.py (with the helpers it imports from ${ROOT}/eval/run_set1.py and the heal outcomes in ${ROOT}/agent/heal.py) for scoring/measurement bugs: is "correct" computed right for healed/escalated/healthy; is the independent post-heal check (was_healthy_before_reset) sound; do the reported heal_s / mttr_s numbers mean what the report says; can a trial be counted as healed when the reset (not the healer) fixed it; can the report crash; does the safety-net reset interact badly with the healer's changes (e.g. kubectl apply after rollout undo / set env)? Report only real bugs with concrete scenarios.` },
]

phase('Review')
const results = await pipeline(
  AREAS,
  a => agent(`${RULES}\n\n${a.prompt}`, { label: `review:${a.key}`, phase: 'Review', schema: FINDINGS }),
  (r, a) => parallel((r?.findings || []).map(f => () =>
    agent(`${RULES}\n\nA reviewer claims the following. REFUTE it by reading the actual code. Default to real=false if it cannot actually happen with this code on k3s v1.36, is already handled, or is only style/hypothetical hardening with no concrete failure. If real, give the minimal correct fix.\n\nFile: ${f.file}:${f.line}\nTitle: ${f.title}\nScenario: ${f.failure_scenario}\nProposed fix: ${f.suggested_fix}`,
      { label: `verify:${a.key}:${f.line}`, phase: 'Verify', schema: VERDICT })
      .then(v => ({ area: a.key, ...f, verdict: v })))),
)
const all = results.flat().filter(Boolean)
log(`${all.length} findings; ${all.filter(f => f.verdict?.real).length} confirmed`)
return {
  confirmed: all.filter(f => f.verdict?.real).map(f => ({ area: f.area, file: f.file, line: f.line, severity: f.severity, title: f.title, scenario: f.failure_scenario, fix: f.verdict.corrected_fix || f.suggested_fix })),
  rejected: all.filter(f => !f.verdict?.real).map(f => ({ area: f.area, line: f.line, title: f.title })),
}
