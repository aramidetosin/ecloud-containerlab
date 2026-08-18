# Failover drills

The same drill suite the original EVE-NG lab ran, executed against this containerlab
replica on 2026-08-17 (post rich-demo deploy, everything self-bootstrapped from this
repo at `63c1ae0`). One element fails per drill; the lab must keep serving, then return
to green on restore.

## Methodology

- **Probe**: a 1/second loop on `client-1` curling the global anycast VIP
  (`http://10.80.15.50/api/whoami`), logging the serving DC or `FAIL` per second.
  Loss numbers below are whole seconds of failed probes.
- **Watchdog paused**: `systemctl stop es-bond-watchdog` for the drill window.
  Drills 3 and 4 create the exact "one bond down, siblings up" signature the
  watchdog reboots leaves for. Re-arm it afterwards.
- **East-west check**: `curl "http://10.80.15.51/api/consume?region=DC2"` mid-drill
  proves the cross-region path (pod to peer-DC NodePort over the backbone) separately
  from the north-south path.
- Every drill ends with a restore and a green check (nodes Ready, ES bonds up,
  brain healthy, VIP steering intact).

## Results

| # | Drill | Failure injected | Loss | Behavior |
|---|-------|------------------|------|----------|
| 1 | Pod | `kubectl -n demo delete pod` (DC1) | **0 s** | remaining replicas served; deployment self-healed |
| 2 | Anycast steering | DC1 `scale deploy dc-demo --replicas=0` | **1 s** | brain saw DC1 health fail ~31 s after scale-0 (endpoints drain gradually), steered the global VIP to DC2; failback to DC1 on restore with **0 s** loss |
| 3 | Host link | one bond leg: `ip link set swp3 down` on leaf-k8s-worker-1 | **~1 s** | fast-LACP ejected the dead leg; node stayed Ready; ~2 s served from DC2 while the brain absorbed the transient |
| 4 | Leaf (EVPN-MH) | both spine uplinks down on leaf-k8s-worker-1 | **1 s** | uplink-tracking pulled all 3 Ethernet Segments; all workers single-homed to the peer leaf, stayed Ready; ~7 s served from DC2; on restore the ES bonds re-formed on their own in ~3 min (no reboot) |
| 5 | Backbone aggr | all 8 fabric ports down on br-agg-sw-1 | **3 s** | north-south reconverged on the FW hold timer (9 s); east-west cross-region consume was uninterrupted via br-agg-sw-2 (31.5 ms); all 8 BGP sessions re-established ~1 min after restore |
| 6a | FW grey failure | fw-pri data links down, HA links up | **blackhole** | see finding below; this is the drill's real result |
| 6b | FW device death | fw-pri data + HA links down | **~9 s** | fw-sec declared the peer failed and took over everything; fw-pri later rejoined with a 2 s blip |

## Findings

### The A/A grey failure (drill 6a)

Cutting only fw-pri's data links while HA stays up produces a **sustained blackhole**,
not a failover:

- fw-sec answers ARP for the DNAT VIPs (verified: after an ARP flush the client
  re-learned `10.80.15.50` at fw-sec's MAC), so packets reach fw-sec.
- But PAN Active/Active punts the session to its owner over **HA3**, and fw-pri
  happily transmits into its dead links.
- fw-pri never declares itself failed because **the PA-VM's dataplane NIC stays "up"
  inside the VM when only the container-side veth goes down**. Link monitoring is
  blind to this class of failure in a virtual harness.

Consequences:

1. **To drill FW failover in containerlab, kill the whole box** (data + HA links):
   `docker exec clab-ecloud-fw-pri sh -c 'for i in 1 2 3 5 6 7; do ip link set eth$i down; done'`
2. A config-level fix worth carrying to production thinking: PAN **path monitoring**
   pinging the aggr /31 peers (10.201.0.0 and 10.201.0.4) would detect exactly this
   condition, where link monitoring cannot.

### Other observations

- **`/proc/net/bonding` MII status is misleading in clab**: a `swp` admin-down inside
  the Cumulus VM does not drop the carrier on the host's veth, so the host bond shows
  both slaves "MII up" while one leg is dead. Fast LACP (3 s timeout) is what actually
  ejects the leg. Trust the LACP state, not MII.
- **The VX ES-bond flakiness is not universal**: drill 4's uplink restore brought all
  three ES bonds back by themselves in ~3 min, no leaf reboot needed. The watchdog
  covers the cases where they stick.
- **An aggr link flap does not trigger the RA one-way wedge**: all four unnumbered
  border sessions re-established within a minute of the ports coming back. The wedge
  documented in the README applies to container recreates only.
- The brain's per-VIP policies held throughout: `ext-client-2` stayed pinned to DC2
  (static) in every drill; `client-1` (latency) and `global` (failover) moved only
  when their policies said so.
