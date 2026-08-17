#!/usr/bin/env bash
# Collect the airgap assets (needs internet + docker + zstd) and build ecloud-k8s-host:1.31.5.
# Run once on the containerlab host, from anywhere: assets land next to this script. The k8s
# nodes have NO internet from inside the lab (their default route points at the fabric), so
# every container image k3s/Cilium/the demo app needs is baked into this one host image.
set -e
cd "$(dirname "$0")"
K3S=v1.31.5+k3s1; CIL=1.16.5
[ -f k3s ] || curl -sfL -o k3s "https://github.com/k3s-io/k3s/releases/download/${K3S}/k3s"
[ -f k3s-airgap-images-amd64.tar ] || { curl -sfL -o a.tar.zst "https://github.com/k3s-io/k3s/releases/download/${K3S}/k3s-airgap-images-amd64.tar.zst"; zstd -d -f a.tar.zst -o k3s-airgap-images-amd64.tar; }
[ -f cilium ] || { curl -sfL -o c.tgz "https://github.com/cilium/cilium-cli/releases/download/v0.16.24/cilium-linux-amd64.tar.gz"; tar xzf c.tgz cilium; }
[ -f cilium-images.tar ] || { docker pull -q quay.io/cilium/cilium:v${CIL}; docker pull -q quay.io/cilium/operator-generic:v${CIL}; docker save quay.io/cilium/cilium:v${CIL} quay.io/cilium/operator-generic:v${CIL} -o cilium-images.tar; }
[ -f app-python.tar ] || { docker pull -q python:3.12-alpine; docker save python:3.12-alpine -o app-python.tar; }
chmod +x k3s cilium
docker build -f Dockerfile.k8s-host -t ecloud-k8s-host:1.31.5 .
