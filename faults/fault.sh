#!/usr/bin/env bash
# Inject / undo faults.
#   Group 1 (Kubernetes service path: discovery & DNS): kubectl changes only.
#   Group 2 (cross-node network): node-level changes made through the privileged
#     diag-host pod, ONLY on the overlay (flannel.1 / VXLAN UDP 8472), never on
#     wlan0, so SSH and the Kubernetes API keep working. Each group-2 fault also
#     schedules its own undo after AUTO_REVERT seconds (default 300) as a safety net.
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

# fault | expected agent label | what it does
FAULTS="
selector    selector_mismatch    db-service selector changed to app=databse (typo)
scale0      no_ready_endpoints   database scaled to 0 replicas
readiness   no_ready_endpoints   database readiness probe always fails
port        port_mismatch        db-service targetPort changed 5432 -> 5433
wrong-name  wrong_service_name   backend DB_HOST changed to db-svc (no such Service)
coredns     dns_coredns_down     CoreDNS scaled to 0 replicas
netpol      other                NetworkPolicy blocks backend->db (not covered: tests 'other')
fw-vxlan    overlay_blocked      [group 2] DB node drops VXLAN (UDP 8472): the pod tunnel is cut
mtu         mtu_blackhole        [group 2] overlay silently drops packets > 1200 bytes (MTU blackhole)
route       route_missing        [group 2] backend node loses its route to the DB node's pod network
loss        packet_loss          [group 2] DB node's overlay drops 40% of packets (tc netem)
delay       high_latency         [group 2] DB node's overlay adds 150 ms delay (tc netem)
"

AUTO_REVERT=${AUTO_REVERT:-300}
DIAG_NS=kuberselfheal

# Run a shell command on a node's host network, through its diag-host pod.
on_node() {
  local pod
  pod=$($K -n $DIAG_NS get pod -l app=diag-host --field-selector "spec.nodeName=$1" -o name | head -1)
  [ -n "$pod" ] || { echo "no diag-host pod on node $1 (kubectl apply -f k8s/diag.yaml)" >&2; return 1; }
  $K -n $DIAG_NS exec "$pod" -- sh -c "$2"
}
node_of()  { $K -n "$NS" get pod -l "app=$1" -o jsonpath='{.items[0].spec.nodeName}'; }
workers()  { $K get nodes -l '!node-role.kubernetes.io/control-plane' -o jsonpath='{.items[*].metadata.name}'; }
pod_cidr() { $K get node "$1" -o jsonpath='{.spec.podCIDR}'; }

# Undo commands for group-2 faults (idempotent: safe to run when nothing is there).
UNDO_FW='while iptables -D INPUT -p udp --dport 8472 -j DROP 2>/dev/null; do :; done'
UNDO_MTU='for d in -i -o; do while iptables -D FORWARD $d flannel.1 -m length --length 1201:65535 -j DROP 2>/dev/null; do :; done; done'
UNDO_TC='tc qdisc show dev flannel.1 | grep -q netem && tc qdisc del dev flannel.1 root || true'
# The healer's MSS clamp (tagged ksh-heal-mss) is only needed while the MTU fault exists.
UNDO_MSS='iptables -t mangle -S FORWARD | grep ksh-heal-mss | sed "s/^-A/-D/" | while read -r r; do eval iptables -t mangle $r; done'

# Schedule an automatic undo on the node (safety net if nobody resets).
auto_revert() {
  on_node "$1" "nohup sh -c 'sleep $AUTO_REVERT; : ksh-revert; $2' >/dev/null 2>&1 &"
}

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
    fw-vxlan)
      n=$(node_of database)
      on_node "$n" "iptables -I INPUT -p udp --dport 8472 -j DROP"
      auto_revert "$n" "$UNDO_FW" ;;
    mtu)
      n=$(node_of database)
      on_node "$n" "iptables -I FORWARD -i flannel.1 -m length --length 1201:65535 -j DROP;
                    iptables -I FORWARD -o flannel.1 -m length --length 1201:65535 -j DROP"
      auto_revert "$n" "$UNDO_MTU" ;;
    route)
      n=$(node_of backend); cidr=$(pod_cidr "$(node_of database)")
      on_node "$n" "ip route del $cidr"
      auto_revert "$n" "ip route replace $cidr via ${cidr%/*} dev flannel.1 onlink" ;;
    loss)
      n=$(node_of database)
      on_node "$n" "tc qdisc add dev flannel.1 root netem loss 40%"
      auto_revert "$n" "$UNDO_TC" ;;
    delay)
      n=$(node_of database)
      on_node "$n" "tc qdisc add dev flannel.1 root netem delay 150ms"
      auto_revert "$n" "$UNDO_TC" ;;
    *) echo "unknown fault '$1' (see: $0 list)" >&2; exit 2 ;;
  esac
}

# Undo every group-2 fault (and the healer's MSS clamp) on all workers.
reset_network() {
  local nodes n m cidr
  nodes=$(workers)
  for n in $nodes; do
    on_node "$n" "pkill -f 'ksh[-]revert' 2>/dev/null; $UNDO_FW; $UNDO_MTU; $UNDO_TC; $UNDO_MSS" || true
    for m in $nodes; do   # restore flannel's route to every other worker's pod network
      [ "$m" = "$n" ] && continue
      cidr=$(pod_cidr "$m")
      on_node "$n" "ip route replace $cidr via ${cidr%/*} dev flannel.1 onlink" || true
    done
  done
}

reset() {
  reset_network
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
