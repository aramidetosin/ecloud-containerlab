#!/bin/sh
# Install Cilium (airgap, BGP control plane) on an ecloud k3s cluster. Run inside a master container.
# Usage: cilium-setup.sh <k8sServiceHost> <podCIDR>
set -e
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
HOST=$1; POD=$2
mount -t bpf bpf /sys/fs/bpf 2>/dev/null || true
mount --make-shared /sys/fs/bpf 2>/dev/null || true
echo "[cilium-setup] installing Cilium 1.16.5 (host=$HOST pod=$POD)"
/usr/local/bin/cilium install --version 1.16.5 \
  --set image.useDigest=false --set operator.image.useDigest=false --set envoy.enabled=false \
  --set ipam.mode=cluster-pool --set ipam.operator.clusterPoolIPv4PodCIDRList="$POD" \
  --set routingMode=tunnel --set tunnelProtocol=vxlan \
  --set kubeProxyReplacement=true --set k8sServiceHost="$HOST" --set k8sServicePort=6443 \
  --set bgpControlPlane.enabled=true --set operator.replicas=1
echo "[cilium-setup] waiting for all nodes Ready ..."
for i in $(seq 1 40); do
  notready=$(/usr/local/bin/k3s kubectl get nodes --no-headers 2>/dev/null | grep -c NotReady || true)
  [ "$notready" = "0" ] && break
  sleep 8
done
/usr/local/bin/k3s kubectl get nodes
