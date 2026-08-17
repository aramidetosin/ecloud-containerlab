#!/usr/bin/env bash
# Build the two vrnetlab images from your qcow2s, and stage the GoBGP binaries.
# Run once on the containerlab host (~10-15 min).
#   VR  = your vrnetlab checkout (with the launcher patches applied, see README)
#   IMG = directory holding the two qcow2s you supply (NOT distributed with this repo):
#         cumulus-linux-5.12.0-vx-amd64-qemu.qcow2  and  PA-VM-KVM-12.1.2.qcow2
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
VR="${VR:-/root/containerlab/vrnetlab}"
IMG="${IMG:-$HERE/images}"
echo "=== Cumulus VX 5.12.0 -> vrnetlab/nvidia_cumulus-vx:5.12.0 ==="
cp -n "$IMG/cumulus-linux-5.12.0-vx-amd64-qemu.qcow2" "$VR/nvidia/cumulus-vx/"
( cd "$VR/nvidia/cumulus-vx" && make )
echo "=== PA-VM 12.1.2 -> vrnetlab/paloalto_pa-vm:12.1.2 ==="
cp -n "$IMG/PA-VM-KVM-12.1.2.qcow2" "$VR/paloalto/pan/"
( cd "$VR/paloalto/pan" && make )
echo "=== GoBGP binaries -> bootstrap/hosts/gobgp/ (airgap: served to the controller nodes via the bind mount) ==="
GB="$HERE/bootstrap/hosts/gobgp"
GBVER=3.30.0
if [ ! -x "$GB/gobgpd" ]; then
  mkdir -p "$GB"
  curl -fsSL -o /tmp/gobgp.tgz "https://github.com/osrg/gobgp/releases/download/v${GBVER}/gobgp_${GBVER}_linux_amd64.tar.gz"
  tar xzf /tmp/gobgp.tgz -C "$GB" gobgp gobgpd && chmod +x "$GB/gobgp" "$GB/gobgpd"
fi
echo "=== result ==="
docker images | grep -iE "cumulus|pa-vm|vr-pan"
ls -la "$GB"/gobgp "$GB"/gobgpd 2>/dev/null
