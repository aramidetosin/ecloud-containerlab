#!/bin/sh
# ecloud k8s-host entrypoint. Runs as PID 1 in a clab `linux` (privileged, cgroupns=private) node.
# Solves the "Kubernetes in a cgroup-v2 container" problem the way kind/k3d do (evacuate the root
# cgroup as PID 1 so containerd/runc can delegate controllers to the k8s.io child), sets up the
# dual-homed fabric bond from env, then launches k3s by role. All node-specific values come from
# clab `env:`.
# PID-1 robustness (learned the hard way): this container is `restart: always`, and if PID 1 ever
# exits, docker recreates the netns and the clab-wired fabric veths (eth1/eth2) are GONE for good
# (clab does not re-wire on a docker restart) -> the node is stranded until a redeploy. So PID 1 must
# be effectively unkillable by anything transient:
#   * no `set -e` -> a failed setup command can never abort the script before the supervise loop
#   * trap TERM/HUP -> a stray `docker stop`/SIGTERM can't end PID 1 (real teardown = clab destroy =
#     docker rm -f = SIGKILL, which is untrappable, so this does not impede teardown)
set +e
trap '' TERM HUP
CG=/sys/fs/cgroup
log() { echo "[k8s-entrypoint] $*"; }

# ---- 1) cgroup v2 evacuation (must happen first, as PID 1, before k3s/containerd spawn) ----
if [ -f "$CG/cgroup.controllers" ]; then
  mkdir -p "$CG/init.scope"
  # move THIS process (PID 1) and any stragglers out of the root cgroup
  echo $$ > "$CG/init.scope/cgroup.procs" 2>/dev/null || true
  for pid in $(cat "$CG/cgroup.procs" 2>/dev/null); do
    echo "$pid" > "$CG/init.scope/cgroup.procs" 2>/dev/null || true
  done
  # now the root has no member procs -> delegate every controller to children (incl. k8s.io)
  for c in $(cat "$CG/cgroup.controllers"); do
    echo "+$c" > "$CG/cgroup.subtree_control" 2>/dev/null || true
  done
  log "root procs left=$(wc -l < "$CG/cgroup.procs") subtree_control=[$(cat "$CG/cgroup.subtree_control")]"
fi

# ---- 2) mount propagation + bpffs (Cilium needs these) ----
mount --make-rshared / 2>/dev/null || true
mount -t bpf bpf /sys/fs/bpf 2>/dev/null || true
mount --make-rshared /sys/fs/bpf 2>/dev/null || true
mount -t cgroup2 none /run/cilium/cgroupv2 2>/dev/null || true

# ---- 3) fabric networking: dual-homed bond0 (data0=eth1, data1=eth2), from env ----
if [ -n "$NODE_BOND_IP" ]; then
  log "waiting for fabric interfaces eth1+eth2 ..."
  until ip link show eth1 >/dev/null 2>&1 && ip link show eth2 >/dev/null 2>&1; do sleep 1; done
  if ! ip link show bond0 >/dev/null 2>&1; then
    ip link add bond0 type bond mode 802.3ad lacp_rate fast miimon 100 xmit_hash_policy layer3+4
    [ -n "$NODE_MAC" ] && ip link set bond0 address "$NODE_MAC"
    ip link set eth1 down; ip link set eth2 down
    ip link set eth1 master bond0; ip link set eth2 master bond0
    ip link set eth1 mtu 9000; ip link set eth2 mtu 9000; ip link set bond0 mtu 9000
    ip link set eth1 up; ip link set eth2 up; ip link set bond0 up
    ip addr add "$NODE_BOND_IP" dev bond0
    if [ -n "$NODE_GW" ]; then
      # CRITICAL: docker gives eth0 (mgmt) a metric-0 default that beats a metric-50 bond0 default,
      # so cross-subnet replies would leave via mgmt and die. Drop the eth0 default and make the
      # FABRIC (bond0) the sole default. The mgmt /24 stays (directly connected) for host SSH.
      while ip route show default dev eth0 2>/dev/null | grep -q .; do ip route del default dev eth0 2>/dev/null || break; done
      ip route replace default via "$NODE_GW" dev bond0
    fi
    printf 'nameserver %s\nsearch ecloud.lab\n' "${NODE_DNS:-10.167.30.10}" > /etc/resolv.conf
    log "bond0 $NODE_BOND_IP via ${NODE_GW:-none} dns ${NODE_DNS}"
  fi
fi

# ---- 3b) SSH access (dropbear): admin/admin (clab VSCode default), user/Test123, root/Test123 ----
for acct in user admin; do id "$acct" >/dev/null 2>&1 || adduser -D -s /bin/sh "$acct" 2>/dev/null || true; addgroup "$acct" wheel 2>/dev/null || true; done
echo "user:Test123"  | chpasswd 2>/dev/null || true
echo "root:Test123"  | chpasswd 2>/dev/null || true
echo "admin:admin"   | chpasswd 2>/dev/null || true
pgrep -x dropbear >/dev/null 2>&1 || (mkdir -p /etc/dropbear; dropbear -R -p 22 >/dev/null 2>&1) || true
# kubectl for operators: k3s is called as kubectl via a symlink; KUBECONFIG set system-wide
ln -sf /usr/local/bin/k3s /usr/bin/kubectl 2>/dev/null || true
printf 'export KUBECONFIG=/etc/rancher/k3s/k3s.yaml\nexport PATH=$PATH:/usr/local/bin\n' > /etc/profile.d/k3s.sh 2>/dev/null || true
log "sshd (dropbear) up; login admin/admin or user|root/Test123; kubectl ready on masters"

# ---- 3c) cluster self-config: the cluster-init master installs Cilium + BGP + the dc-demo app
#         in the background once the cluster forms (idempotent; makes the k8s layer bootstrap too) ----
if [ "$K3S_ROLE" = "server-init" ] && [ -x /opt/ecloud/cluster-bootstrap.sh ]; then
  ( /opt/ecloud/cluster-bootstrap.sh "$ECLOUD_DC" "$NODE_IP" "$ECLOUD_POD_CIDR" "$ECLOUD_EXPECT_NODES" > /var/log/cluster-bootstrap.log 2>&1 ) &
  log "cluster-bootstrap spawned (dc=$ECLOUD_DC expect=$ECLOUD_EXPECT_NODES)"
fi

# ---- 4) launch k3s by role, SUPERVISED (entrypoint stays PID 1 so a k3s crash does not kill the
#         container and lose the clab-wired interfaces). Join roles wait for the server API first. ----
KA="--kubelet-arg=cgroups-per-qos=false --kubelet-arg=enforce-node-allocatable= --kubelet-arg=fail-swap-on=false"
COMMON="--flannel-backend=none --disable-network-policy --disable=traefik --disable=servicelb --disable-kube-proxy --snapshotter=native --write-kubeconfig-mode=644 $KA"
build_cmd() {
  case "$K3S_ROLE" in
    server-init) echo "server --cluster-init --token $K3S_TOKEN --node-ip $NODE_IP --advertise-address $NODE_IP --tls-san $NODE_IP $COMMON" ;;
    server-join) echo "server --server $K3S_URL --token $K3S_TOKEN --node-ip $NODE_IP --advertise-address $NODE_IP $COMMON" ;;
    agent)       echo "agent --server $K3S_URL --token $K3S_TOKEN --node-ip $NODE_IP $KA --snapshotter=native" ;;
    *)           echo "" ;;
  esac
}
CMD=$(build_cmd)
[ -z "$CMD" ] && { log "no K3S_ROLE set; idling"; exec sleep infinity; }
# join roles: wait until the server's API port is reachable, so etcd/agent join does not fail fatally
if [ "$K3S_ROLE" != "server-init" ] && [ -n "$K3S_URL" ]; then
  host=$(echo "$K3S_URL" | sed -E 's#https?://([^:/]+).*#\1#')
  log "waiting for server API $host:6443 ..."
  until nc -z -w3 "$host" 6443 2>/dev/null; do sleep 5; done
  log "server API reachable"
fi
log "supervising k3s role=$K3S_ROLE node-ip=$NODE_IP"
while true; do
  /usr/local/bin/k3s $CMD
  log "k3s exited ($?), restarting in 10s (container + interfaces stay up)"
  sleep 10
done
