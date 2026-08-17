#!/bin/sh
# Runs in the background on the cluster-init master (spawned by k8s-entrypoint.sh). Waits for the
# cluster to form, then installs Cilium (airgap, BGP control plane), applies the per-DC BGP config,
# and deploys the dc-demo app. Idempotent: safe to re-run; skips work already done.
# Args: <dc> <k8sServiceHost> <podCIDR> <expectNodes>
set -u
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
DC="$1"; HOST="$2"; POD="$3"; EXPECT="${4:-1}"
K="/usr/local/bin/k3s kubectl"
log() { echo "[cluster-bootstrap $(date +%H:%M:%S)] $*"; }

# 1) wait for the API
until $K get nodes >/dev/null 2>&1; do sleep 5; done
log "API up"
# 2) wait for the expected node count (best effort)
for i in $(seq 1 90); do
  n=$($K get nodes --no-headers 2>/dev/null | wc -l)
  [ "$n" -ge "$EXPECT" ] && { log "all $EXPECT nodes joined"; break; }
  sleep 10
done

# 3) install Cilium (once)
if ! $K get ds -n kube-system cilium >/dev/null 2>&1; then
  mount -t bpf bpf /sys/fs/bpf 2>/dev/null || true
  mount --make-shared /sys/fs/bpf 2>/dev/null || true
  log "installing Cilium 1.16.5 (host=$HOST pod=$POD)"
  /usr/local/bin/cilium install --version 1.16.5 \
    --set image.useDigest=false --set operator.image.useDigest=false --set envoy.enabled=false \
    --set ipam.mode=cluster-pool --set ipam.operator.clusterPoolIPv4PodCIDRList="$POD" \
    --set routingMode=tunnel --set tunnelProtocol=vxlan \
    --set kubeProxyReplacement=true --set k8sServiceHost="$HOST" --set k8sServicePort=6443 \
    --set bgpControlPlane.enabled=true --set operator.replicas=1 >/dev/null 2>&1
else
  log "Cilium already present"
fi
# wait for all nodes Ready
for i in $(seq 1 60); do
  [ "$($K get nodes --no-headers 2>/dev/null | grep -c ' Ready ')" -ge "$EXPECT" ] && break
  sleep 8
done

# 4) apply the per-DC BGP config + the dc-demo app + the per-client steering VIPs (idempotent).
# perclient-vips is DC-agnostic: both DCs advertise .202.3 and .202.4, the GoBGP brain steers each.
[ -f "/opt/ecloud/bgp-$DC.yaml" ]        && { $K apply -f "/opt/ecloud/bgp-$DC.yaml" >/dev/null 2>&1;        log "applied bgp-$DC"; }
# the rich dc-demo app lives in namespace demo with its code + docs in the dc-demo-app ConfigMap.
# The ConfigMap MUST go through create/replace, never apply: docs.html (>256KB) overflows the
# kubectl last-applied annotation (same rule as the original EVE runbook).
if [ -f "/opt/ecloud/demo/app-$DC.py" ]; then
  $K create namespace demo >/dev/null 2>&1
  if $K -n demo get configmap dc-demo-app >/dev/null 2>&1; then
    $K -n demo create configmap dc-demo-app --from-file=app.py="/opt/ecloud/demo/app-$DC.py" \
      --from-file=docs.html=/opt/ecloud/demo/docs.html --dry-run=client -o yaml | $K -n demo replace -f - >/dev/null 2>&1
  else
    $K -n demo create configmap dc-demo-app --from-file=app.py="/opt/ecloud/demo/app-$DC.py" \
      --from-file=docs.html=/opt/ecloud/demo/docs.html >/dev/null 2>&1
  fi
  log "dc-demo-app configmap in place"
fi
[ -f "/opt/ecloud/dc-demo-$DC.yaml" ]    && { $K apply -f "/opt/ecloud/dc-demo-$DC.yaml" >/dev/null 2>&1;    log "applied dc-demo-$DC"; }
[ -f "/opt/ecloud/perclient-vips.yaml" ] && { $K apply -f "/opt/ecloud/perclient-vips.yaml" >/dev/null 2>&1; log "applied perclient-vips"; }
log "cluster bootstrap complete"
$K get nodes
