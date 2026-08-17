#!/bin/bash
# ES-bond flap watchdog for the ecloud containerlab leaves. VX EVPN-MH ES bonds intermittently go
# carrier-down and stay stuck (only a leaf reboot recovers them). A bond that is DOWN while sibling
# bonds on the same leaf are UP is a flap (not normal boot/LACP convergence, where all are down),
# so after a grace window we reboot that leaf. Cooldown prevents reboot loops. Runs in a loop from
# the es-bond-watchdog.service. State in /run so it resets on host reboot.
GRACE=90        # a mixed-state (some up/some down) bond must persist this long before we act
COOLDOWN=480    # don't reboot the same leaf again within this window (leaf reboot+converge ~5 min)
STATE=/run/es-bond-watchdog; mkdir -p "$STATE"
SSH='sshpass -p Clab123! ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=6 -o LogLevel=ERROR cumulus@127.0.0.1'
now=$(date +%s)
for c in $(docker ps --format '{{.Names}}' 2>/dev/null | grep -E 'clab-ecloud-(leaf-k8s|leaf-service|dc2-k8s-leaf|dc2-svc-leaf)'); do
  n=${c#clab-ecloud-}
  st=$(docker exec "$c" $SSH 'up=0;dn="";for b in bond1 bond2 bond3 bond4;do [ -e /sys/class/net/$b ]||continue;if [ "$(cat /sys/class/net/$b/operstate)" = up ];then up=$((up+1));else dn="$dn$b,";fi;done;echo "${up}|${dn}"' 2>/dev/null | tr -d '\r')
  [ -z "$st" ] && continue
  up=${st%%|*}; dn=${st#*|}
  if [ -n "$dn" ] && [ "${up:-0}" -ge 1 ]; then          # flap: leaf operational (>=1 up) but a bond is down
    [ -f "$STATE/$n.since" ] || echo "$now" > "$STATE/$n.since"
    first=$(cat "$STATE/$n.since"); age=$((now - first))
    last=$(cat "$STATE/$n.reboot" 2>/dev/null || echo 0)
    if [ "$age" -ge "$GRACE" ] && [ $((now - last)) -ge "$COOLDOWN" ]; then
      logger -t es-bond-watchdog "$n stuck ES bond(s) [$dn] for ${age}s while $up up -> rebooting leaf"
      docker exec "$c" $SSH 'sudo reboot' >/dev/null 2>&1
      echo "$now" > "$STATE/$n.reboot"; rm -f "$STATE/$n.since"
    fi
  else
    rm -f "$STATE/$n.since"                               # all up, or all down (converging) -> reset timer
  fi
done
