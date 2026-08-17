# Host services (run on the containerlab host, not inside a node)

## es-bond-watchdog

The k8s hosts attach to their leaf pair with EVPN-MH ES (ESI) bonds. Cumulus VX
in containerlab intermittently leaves one ES bond stuck carrier-down (`operstate
down` while its sibling bonds on the same leaf are up). That is a flap, not normal
boot/LACP convergence (where *all* the leaf's bonds are down together while it
reconverges). A stuck ES bond disconnects a host; on the DC1 3-node etcd cluster
that can cost quorum. The only reliable recovery for a stuck VX ES bond is a leaf
reboot.

`es-bond-watchdog.sh` polls the 10 k8s-serving leaves (via `docker exec` +
`sshpass ssh cumulus@127.0.0.1`). For each leaf, if a bond is down while at least
one sibling bond is up (the flap signature) and that state persists past a grace
window, it reboots that leaf. A per-leaf cooldown prevents reboot loops, and the
all-bonds-down case (normal boot convergence) is deliberately ignored.

State lives in `/run/es-bond-watchdog` so it resets on host reboot.

### Install

```bash
sudo install -m 755 es-bond-watchdog.sh /usr/local/sbin/es-bond-watchdog.sh
sudo cp es-bond-watchdog.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now es-bond-watchdog.service
```

Tunables at the top of the script: `GRACE` (default 90s), `COOLDOWN` (480s).
