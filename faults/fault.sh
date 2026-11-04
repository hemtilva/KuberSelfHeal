#!/usr/bin/env bash
# Inject / undo the faults for problem set 1 (Service discovery & DNS).
# Every fault is a kubectl change only: nothing touches wlan0, so it's safe over SSH.
#
#   faults/fault.sh list
#   faults/fault.sh inject <fault>
#   faults/fault.sh reset            # undo ANY fault: re-apply k8s/app.yaml, restore CoreDNS
#
#   KUBECTL="sudo k3s kubectl" faults/fault.sh ...   # if kubeconfig needs root
set -euo pipefail

K=${KUBECTL:-kubectl}
NS=${APP_NAMESPACE:-default}
REPO=$(cd "$(dirname "$0")/.." && pwd)

# fault | problem # | expected agent label | what it does
FAULTS="
selector    7   selector_mismatch    db-service selector changed to app=databse (typo)
scale0      8   no_ready_endpoints   database scaled to 0 replicas
readiness   8   no_ready_endpoints   database readiness probe always fails
port        9   port_mismatch        db-service targetPort changed 5432 -> 5433
wrong-name  12  wrong_service_name   backend DB_HOST changed to db-svc (no such Service)
coredns     14  dns_coredns_down     CoreDNS scaled to 0 replicas
netpol      -   other                NetworkPolicy blocks backend->db (outside set 1, tests 'other')
"

wait_rollouts() {
  $K -n "$NS" rollout status deploy/backend --timeout=300s >/dev/null
  $K -n "$NS" rollout status deploy/database --timeout=300s >/dev/null || true
}

inject() {
  case "$1" in
    selector)
      $K -n "$NS" patch svc db-service --type=merge -p '{"spec":{"selector":{"app":"databse"}}}' ;;
    scale0)
      $K -n "$NS" scale deploy/database --replicas=0
      $K -n "$NS" wait --for=delete pod -l app=database --timeout=120s || true ;;
    readiness)
      # Recreate strategy: the old (Ready) pod is deleted, the new one never becomes Ready.
      old=$($K -n "$NS" get pods -l app=database -o name)
      $K -n "$NS" patch deploy/database --type=json -p \
        '[{"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/exec/command","value":["sh","-c","exit 1"]}]'
      [ -z "$old" ] || $K -n "$NS" wait --for=delete $old --timeout=180s || true ;;
    port)
      $K -n "$NS" patch svc db-service --type=json -p \
        '[{"op":"replace","path":"/spec/ports/0/targetPort","value":5433}]' ;;
    wrong-name)
      $K -n "$NS" set env deploy/backend DB_HOST=db-svc
      $K -n "$NS" rollout status deploy/backend --timeout=300s ;;
    coredns)
      $K -n kube-system scale deploy/coredns --replicas=0
      $K -n kube-system wait --for=delete pod -l k8s-app=kube-dns --timeout=120s || true ;;
    netpol)
      $K -n "$NS" apply -f - <<EOF
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: ksh-fault-netpol
  labels: {kuberselfheal/fault: "true"}
spec:
  podSelector: {matchLabels: {app: database}}
  policyTypes: [Ingress]
  ingress:
    - from: [{podSelector: {matchLabels: {app: frontend}}}]
EOF
      ;;
    *) echo "unknown fault '$1' (see: $0 list)" >&2; exit 2 ;;
  esac
}

reset() {
  $K -n "$NS" delete networkpolicy -l kuberselfheal/fault=true --ignore-not-found >/dev/null
  $K -n kube-system scale deploy/coredns --replicas=1 >/dev/null
  $K -n "$NS" apply -f "$REPO/k8s/app.yaml" >/dev/null
  $K -n kube-system rollout status deploy/coredns --timeout=300s >/dev/null
  wait_rollouts
  echo "reset done"
}

case "${1:-}" in
  list)   echo "$FAULTS" | sed '/^$/d' ;;
  inject) [ $# -eq 2 ] || { echo "usage: $0 inject <fault>" >&2; exit 2; }
          inject "$2"; echo "injected: $2" ;;
  reset)  reset ;;
  *)      echo "usage: $0 list | inject <fault> | reset" >&2; exit 2 ;;
esac
