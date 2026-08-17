#!/usr/bin/env bash
# Push the ecloud fabric configs into the containerlab switches, post-deploy.
# The nvidia_cumulusvx kind does not apply startup-config, so we replay each device's
# `nv set` lines from the repo snapshot over SSH, then `nv config apply -y`.
#
# Usage:  ./push_configs.sh [fabric-dir]      (default: ./configs = copy of fabric-evpn-mh/)
# Creds:  vrnetlab default cumulus / Clab123!  (mgmt IPs are STATIC, from ecloud.clab.yml)
set -u
CFG=${1:-./configs}
PW="Clab123!"
SSH="sshpass -p $PW ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=8"

# name -> mgmt IP (must match ecloud.clab.yml)
declare -A IP=(
 [spine-1]=172.29.129.11 [spine-2]=172.29.129.12
 [leaf-k8s-master-1]=172.29.129.13 [leaf-k8s-master-2]=172.29.129.14
 [leaf-k8s-worker-1]=172.29.129.15 [leaf-k8s-worker-2]=172.29.129.16
 [leaf-service-1]=172.29.129.17 [leaf-service-2]=172.29.129.18
 [leaf-border-1]=172.29.129.19 [leaf-border-2]=172.29.129.20
 [dc2-spine-1]=172.29.129.21 [dc2-spine-2]=172.29.129.22
 [dc2-k8s-leaf-1]=172.29.129.23 [dc2-k8s-leaf-2]=172.29.129.24
 [dc2-svc-leaf-1]=172.29.129.25 [dc2-svc-leaf-2]=172.29.129.26
 [dc2-border-1]=172.29.129.27 [dc2-border-2]=172.29.129.28
 [br-agg-sw-1]=172.29.129.29 [br-agg-sw-2]=172.29.129.30
)
dir_of(){ case $1 in br-agg*) echo aggr;; dc2-*) echo dc2;; *) echo dc1;; esac; }

push(){
  local n=$1 ip=${IP[$1]} f="$CFG/$(dir_of $1)/$1.nvue.txt"
  [ -f "$f" ] || { echo "  $n: NO CONFIG $f"; return 1; }
  # Filter lines that must NOT be replayed into clab:
  #  - mgmt interface / mgmt VRF addressing (clab owns eth0)
  #  - the REDACTED snmp community (would set an invalid value)
  #  - hostname is already set by vrnetlab
  #  - 'system aaa user' lines: the export shows hashed-password '*' (redacted). Replaying that
  #    literally sets the cumulus password hash to '*' = ACCOUNT LOCKED, killing SSH on every
  #    switch it is applied to. Learned the hard way 2026-08-17 (6 switches locked out).
  grep -E '^nv set' "$f" \
    | grep -vE 'interface eth0|vrf mgmt (ip|router)|snmp-server.*REDACTED|REDACTED|system aaa user' \
    > /tmp/push-$n.nv
  echo "  $n ($ip): $(wc -l < /tmp/push-$n.nv) nv-set lines"
  # replay in one shell session, then apply
  $SSH cumulus@$ip 'bash -s' <<EOF
$(cat /tmp/push-$n.nv)
nv config apply -y 2>&1 | tail -2
EOF
}

echo "=== pushing configs from $CFG ==="
# spines first (underlay), then leaves, then borders, then backbone
for n in spine-1 spine-2 dc2-spine-1 dc2-spine-2 \
         leaf-k8s-master-1 leaf-k8s-master-2 leaf-k8s-worker-1 leaf-k8s-worker-2 \
         leaf-service-1 leaf-service-2 dc2-k8s-leaf-1 dc2-k8s-leaf-2 dc2-svc-leaf-1 dc2-svc-leaf-2 \
         leaf-border-1 leaf-border-2 dc2-border-1 dc2-border-2 br-agg-sw-1 br-agg-sw-2; do
  push $n
done

echo
echo "=== VX post-apply, per the EVPN-MH lesson: ES bonds need a REBOOT to converge, then redirect-off ==="
echo "  for each access leaf:  sudo reboot; wait ~3 min; then:"
echo "  sudo vtysh -c 'configure terminal' -c 'evpn mh redirect-off' -c 'end' -c 'write memory'"
