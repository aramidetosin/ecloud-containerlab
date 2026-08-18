#!/usr/bin/env bash
# Passwordless SSH for every lab device: install the clab host's public key on all 20 Cumulus
# switches (user cumulus), all 16 linux hosts (root, admin, user), and both PA-VMs (admin,
# via the XML API + commit). Run on the containerlab host after a deploy; idempotent.
# Key: $1, or ~/.ssh/id_ed25519.pub by default (generated if missing).
set -e
KEY="${1:-$HOME/.ssh/id_ed25519.pub}"
[ -f "$KEY" ] || ssh-keygen -t ed25519 -f "${KEY%.pub}" -N "" -q
PUB=$(cat "$KEY")
# stage the key into the bootstrap so REDEPLOYS install it at first boot (hosts via the
# bind mount, switches via the patched launcher, firewalls folded into the config commit)
REPO="$(cd "$(dirname "$0")/.." && pwd)"
install -m 644 "$KEY" "$REPO/bootstrap/hosts/authorized_keys" && echo "staged into bootstrap/hosts/authorized_keys"
SO="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=6 -o LogLevel=ERROR"

echo "== switches (cumulus) =="
for c in $(docker ps --format '{{.Names}}' | grep clab-ecloud | grep -E 'spine|leaf|br-agg|border'); do
  docker exec "$c" sshpass -p 'Clab123!' ssh $SO cumulus@127.0.0.1 \
    "mkdir -p ~/.ssh && chmod 700 ~/.ssh && grep -qF '$PUB' ~/.ssh/authorized_keys 2>/dev/null || echo '$PUB' >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys" \
    2>/dev/null && echo "  ${c#clab-ecloud-} ok" || echo "  ${c#clab-ecloud-} FAILED"
  # VSCode-extension convenience: admin/admin + the same key (plain useradd, no NVUE)
  docker exec "$c" sshpass -p 'Clab123!' ssh $SO cumulus@127.0.0.1 \
    "sudo useradd -m -s /bin/bash admin 2>/dev/null; sudo usermod -p '\$6\$ecloudlab\$Uf7ZjAwEVT13Doo.zoNI7OGx0zIUzy0SkiKeiiRIuKQxAnZk7TY39el3vMb0zcxQ4YblqLJscirCFqe3HkTU./' admin; sudo usermod -aG sudo admin 2>/dev/null; sudo mkdir -p /home/admin/.ssh && sudo cp ~/.ssh/authorized_keys /home/admin/.ssh/authorized_keys && sudo chown -R admin:admin /home/admin/.ssh && sudo chmod 700 /home/admin/.ssh && sudo chmod 600 /home/admin/.ssh/authorized_keys" 2>/dev/null || true
done

echo "== linux hosts (root, admin, user) =="
for c in $(docker ps --format '{{.Names}}' | grep clab-ecloud | grep -vE 'spine|leaf|br-agg|border|fw-'); do
  docker exec "$c" sh -c "
    for pair in '/root root' '/home/admin admin' '/home/user user'; do
      set -- \$pair; h=\$1; u=\$2
      [ -d \$h ] || continue
      mkdir -p \$h/.ssh && chmod 700 \$h/.ssh
      grep -qF '$PUB' \$h/.ssh/authorized_keys 2>/dev/null || echo '$PUB' >> \$h/.ssh/authorized_keys
      chmod 600 \$h/.ssh/authorized_keys; chown -R \$u \$h/.ssh 2>/dev/null || true
    done" && echo "  ${c#clab-ecloud-} ok" || echo "  ${c#clab-ecloud-} FAILED"
done

echo "== firewalls (admin, via the API; commit runs async) =="
B64=$(printf '%s\n' "$PUB" | base64 -w0)
for f in fw-pri fw-sec; do
  IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' clab-ecloud-$f 2>/dev/null) || continue
  K=$(curl -sk "https://$IP/api/?type=keygen&user=admin&password=Admin@123" | grep -o '<key>[^<]*' | cut -c6-)
  [ -n "$K" ] || { echo "  $f FAILED (no api key)"; continue; }
  curl -sk "https://$IP/api/" --data-urlencode "type=config" --data-urlencode "action=set" \
    --data-urlencode "key=$K" \
    --data-urlencode "xpath=/config/mgt-config/users/entry[@name='admin']" \
    --data-urlencode "element=<public-key>$B64</public-key>" | grep -q 'success' || { echo "  $f FAILED (set)"; continue; }
  curl -sk "https://$IP/api/?type=commit&key=$K" --data-urlencode "cmd=<commit></commit>" >/dev/null
  echo "  $f ok (key set, commit submitted)"
done
echo "done. Test: ssh cumulus@clab-ecloud-spine-1 / ssh admin@clab-ecloud-k8s-master-1"
