# End-to-end data-plane trace: DC1 k8s host to DC2 k8s host (tenant-k8s)

Audit date: 2026-08-21. Lab: `ecloud-containerlab` at commit `25c22b5` ("Enable eBGP ECMP on border leaves; add fabric audits"), cold-deployed from a fresh clone on the office EVE host (deploy 11:12:57 UTC, 22/22 health checks passed 11:23:01 UTC, all README smoke tests green 11:23:40 UTC). Trace collected roughly 15 minutes after deploy, on an otherwise idle lab. Every device identity and every ECMP claim below was verified on the box at the time of the trace; nothing is inferred from the topology file.

Source: `k8s-master-1` (DC1), `bond0` = 10.167.10.11/24, default via 10.167.10.1, bond mode 802.3ad, transmit hash policy layer3+4.
Destination: `dc2-k8s-master-1` (DC2), `bond0` = 10.168.10.11/24 (MAC 50:00:00:1f:00:01), default via 10.168.10.1.
VRF on every routed hop: `tenant-k8s`.

Raw captures for everything quoted here are in `raw/` next to this file.

## 1. The live traceroute, decoded

| Hop | Reported IP | Actual device | What happens there |
|---|---|---|---|
| - | - | k8s-master-1 | Default route to the anycast GW 10.167.10.1 on bond0. The 802.3ad bond hashes per flow (layer3+4) across both uplinks, so hop 1 alternates between .2 and .3: the 3-probe run landed on both, and the single-flow runs pinned to one or the other. |
| 1 | 10.167.10.2 / 10.167.10.3 | leaf-k8s-master-1 / leaf-k8s-master-2 | Anycast GW (VRR 10.167.10.1 shared; the reply comes from each leaf's real SVI address: verified `vlan110` = 10.167.10.2/24 on leaf-1, 10.167.10.3/24 on leaf-2). VRF tenant-k8s lookup for the destination host /32: `10.168.10.11/32 via 10.0.0.17 AND 10.0.0.18, vlan3001_l3 onlink` = both DC1 border loopbacks (verified lo 10.0.0.17 on leaf-border-1, 10.0.0.18 on leaf-border-2), 2-way ECMP, then VXLAN encapsulation into L3VNI 50001 (tenant-k8s's DC1 L3VNI). |
| - | (invisible) | spine-1 / spine-2 | Underlay only. Routes the outer VXLAN packet to the hashed border VTEP and never touches the inner TTL. |
| 2 | 192.0.0.8 | leaf-border-1 or leaf-border-2 | Decapsulation, then VRF tenant-k8s lookup: `10.168.10.11/32 via swp3.100 AND swp4.100`, next-hops `fe80::a8c1:abff:fe0b:f1f1` and `fe80::a8c1:abff:feb7:d029`, i.e. unnumbered eBGP to br-agg-sw-1 and br-agg-sw-2 on the VLAN 100 dot1q subinterfaces (tenant-k8s's DCI VLAN), 2-way ECMP. 192.0.0.8 is configured on no interface of either border (checked: zero matches in `ip addr` on both). It is the RFC 7600 IPv4 dummy address that the Linux kernel uses to source ICMP from an interface that has no IPv4 address. |
| 3 | 10.201.20.1 / 10.201.20.5 | br-agg-sw-1 / br-agg-sw-2 | VRF-lite DCI. VRF tenant-k8s lookup: `10.168.10.11/32 via swp5.100 AND swp6.100` (unnumbered, fe80 next-hops) = both DC2 borders, 2-way ECMP. The reply source is each agg's first VRF address, its gobgp-facing /30 (verified: 10.201.20.1/30 on br-agg-sw-1 `swp7`, 10.201.20.5/30 on br-agg-sw-2 `swp7`), not the interface the packet actually left on. That is ICMP source-address selection, nothing more. |
| 4 | 192.0.0.8 | dc2-border-1 or dc2-border-2 | Re-enters EVPN. VRF tenant-k8s lookup: `10.168.10.11/32 via 10.2.0.11 AND 10.2.0.12, vlan3101_l3 onlink` = both DC2 k8s leaf loopbacks (verified lo 10.2.0.11 on dc2-k8s-leaf-1, 10.2.0.12 on dc2-k8s-leaf-2), 2-way ECMP, then VXLAN encapsulation into L3VNI 50101 (tenant-k8s's DC2 L3VNI; 50102 is tenant-svc's). Same unnumbered dummy source as hop 2: zero matches for 192.0.0.8 on either DC2 border. |
| - | (invisible) | dc2-spine-1 / dc2-spine-2 | DC2 underlay transit. |
| 5 | 10.168.10.2 / 10.168.10.3 | dc2-k8s-leaf-1 / dc2-k8s-leaf-2 | Decapsulation. `10.168.10.0/24` is connected on `vlan210-v0` (verified `vlan210` = 10.168.10.2/24 on leaf-1, 10.168.10.3/24 on leaf-2). EVPN MAC table for VNI 10210 / VLAN 210: `50:00:00:1f:00:01` is local on `bond1`, ESI 03:44:38:39:be:ef:19:00:00:01, present as local on BOTH leaves (the destination host is dual-homed on an EVPN-MH Ethernet segment). L2 delivery out the host bond. |
| 6 | 10.168.10.11 | dc2-k8s-master-1 | Destination. Its own default route is 10.168.10.1 on bond0, so the return walks the mirror of this path. |

Arriving TTL: every echo reply reaches the source with `ttl=59`, i.e. exactly 5 routing hops on the return path (64 - 59): egress leaf, DC2 border, agg, DC1 border, ingress leaf. The forward direction is the same five.

The firewalls are not in this east-west path. At every decision point the FIB next-hops are border, agg, or leaf interfaces inside tenant-k8s (`swp3/4.100` between DC1 borders and aggs, `swp5/6.100` between aggs and DC2 borders, the `vlan3001_l3` / `vlan3101_l3` onlink VTEP routes inside each DC). fw-pri / fw-sec only appear in the north-south path (section 7).

## 2. The control plane behind it

DC2's `10.168.10.0/24` and its host `/32` routes leave DC2's EVPN domain at dc2-border-1/2, cross the DCI as plain eBGP IPv4 unicast inside tenant-k8s on the VLAN-100 subinterfaces (unnumbered sessions, IPv6 link-local next-hops), and are re-originated by the DC1 borders into L3VNI 50001 as type-5 routes. Host-granular /32s propagate end to end: the DC1 ingress leaf holds `10.168.10.11/32` itself ("Known via bgp"), not only the /24.

## 3. ECMP status, per decision point

This trace exercises the change introduced in commit `25c22b5`: `nv set vrf tenant-k8s router bgp address-family ipv4-unicast multipaths ebgp 4` (was 1) on all four border leaves, and the same for tenant-svc. Live FIB at the time of the trace:

| Decision point | Route | Next-hops | ECMP |
|---|---|---|---|
| leaf-k8s-master-1 (ingress leaf) | 10.168.10.11/32 | 10.0.0.17, 10.0.0.18 via vlan3001_l3 onlink | 2-way (both DC1 borders) |
| leaf-border-1 (DC1 border) | 10.168.10.11/32 | fe80::a8c1:abff:fe0b:f1f1 via swp3.100, fe80::a8c1:abff:feb7:d029 via swp4.100 | 2-way (both aggs): this is the row the commit fixed |
| br-agg-sw-1 (backbone) | 10.168.10.11/32 | fe80::a8c1:abff:fe15:5db7 via swp5.100, fe80::a8c1:abff:fe4b:4b47 via swp6.100 | 2-way (both DC2 borders) |
| dc2-border-1 (DC2 border) | 10.168.10.11/32 | 10.2.0.11, 10.2.0.12 via vlan3101_l3 onlink | 2-way (both DC2 k8s leaves) |
| dc2-k8s-leaf-1 (egress leaf) | 10.168.10.0/24 | directly connected, vlan210-v0 | local delivery |

Observed in the data plane across the sampled flows: both ingress leaves (.2 and .3 at hop 1), both aggs (10.201.20.1 and 10.201.20.5 at hop 3), both egress leaves (10.168.10.2 and 10.168.10.3 at hop 5). Each individual flow stays pinned to a single consistent path, so there is no packet reordering; the population of flows spreads across both members at each layer. Flow-hash ECMP working as designed.

## 4. Multi-probe trace (3 probes per hop), verbatim

```
traceroute to 10.168.10.11 (10.168.10.11), 10 hops max, 46 byte packets
 1  10.167.10.2  0.248 ms  10.167.10.3  0.209 ms  0.065 ms
 2  192.0.0.8  0.673 ms  0.457 ms  0.207 ms
 3  10.201.20.1  0.492 ms  10.201.20.5  0.378 ms  0.222 ms
 4  192.0.0.8  0.482 ms  0.412 ms  0.440 ms
 5  10.168.10.3  0.791 ms  0.624 ms  0.344 ms
 6  10.168.10.11  0.624 ms  0.409 ms  0.935 ms
```

Decoded:

```
 1  10.167.10.2 / 10.167.10.3   both DC1 leaves (leaf-k8s-master-1 / -2)
 2  192.0.0.8                   DC1 border (leaf-border-1 or -2, unnumbered)
 3  10.201.20.1 / 10.201.20.5   BOTH aggs in this probe set (br-agg-sw-1 / -2)
 4  192.0.0.8                   DC2 border (dc2-border-1 or -2)
 5  10.168.10.3                 dc2-k8s-leaf-2 (this probe set)
 6  10.168.10.11                destination, 0% loss
```

## 5. Per-flow pinning (one flow per run, different UDP destination ports)

Each run is one traceroute process with a single probe per hop (`-q1`) and a distinct UDP base port (`-p`). Note that each process also picks a fresh UDP source port, so every run is a genuinely new 5-tuple; the same `-p` value can legitimately hash to a different member in a later run.

| Flow | `-p` | Ingress leaf (hop 1) | Agg used (hop 3) | Egress leaf (hop 5) |
|---|---|---|---|---|
| 1 | 33434 | leaf-k8s-master-1 (.2) | br-agg-sw-2 (.5) | dc2-k8s-leaf-2 (.3) |
| 2 | 33500 | leaf-k8s-master-2 (.3) | br-agg-sw-1 (.1) | dc2-k8s-leaf-1 (.2) |
| 3 | 33600 | leaf-k8s-master-2 (.3) | br-agg-sw-1 (.1) | dc2-k8s-leaf-1 (.2) |
| 4 | 33700 | leaf-k8s-master-2 (.3) | br-agg-sw-1 (.1) | dc2-k8s-leaf-1 (.2) |

Verbatim:

```
--- flow port 33434 ---
 1  10.167.10.2  0.172 ms
 2  192.0.0.8  0.581 ms
 3  10.201.20.5  0.409 ms
 4  192.0.0.8  0.516 ms
 5  10.168.10.3  0.699 ms
 6  10.168.10.11  0.695 ms
--- flow port 33500 ---
 1  10.167.10.3  0.107 ms
 2  192.0.0.8  0.433 ms
 3  10.201.20.1  0.356 ms
 4  192.0.0.8  0.455 ms
 5  10.168.10.2  0.638 ms
 6  10.168.10.11  0.472 ms
--- flow port 33600 ---
 1  10.167.10.3  0.115 ms
 2  192.0.0.8  0.425 ms
 3  10.201.20.1  0.373 ms
 4  192.0.0.8  0.490 ms
 5  10.168.10.2  1.064 ms
 6  10.168.10.11  0.956 ms
--- flow port 33700 ---
 1  10.167.10.3  0.149 ms
 2  192.0.0.8  0.594 ms
 3  10.201.20.1  0.378 ms
 4  192.0.0.8  0.522 ms
 5  10.168.10.2  1.068 ms
 6  *
```

Flow 4's final probe timed out within the 1-second wait on the first run (a probe timeout, not a path failure). The re-run with a 2-second wait completed, and, being a new flow (new source port), hashed to agg-2 and egress leaf-1:

```
--- flow port 33700, re-run (-w2) ---
 1  10.167.10.3  0.116 ms
 2  192.0.0.8  0.701 ms
 3  10.201.20.5  0.604 ms
 4  192.0.0.8  0.551 ms
 5  10.168.10.2  0.997 ms
 6  10.168.10.11  0.728 ms
```

## 6. Identity and state verification, verbatim

Who owns each reported IP (`ip -br addr` on the device):

```
leaf-k8s-master-1 vlan110: 10.167.10.2/24
leaf-k8s-master-2 vlan110: 10.167.10.3/24
dc2-k8s-leaf-1 vlan210:   10.168.10.2/24
dc2-k8s-leaf-2 vlan210:   10.168.10.3/24
br-agg-sw-1: swp7 10.201.20.1/30  swp8 10.201.20.9/30   swp3.100 10.201.0.0/31 swp4.100 10.201.0.4/31  swp3.200 10.201.1.0/31 swp4.200 10.201.1.4/31
br-agg-sw-2: swp7 10.201.20.5/30  swp8 10.201.20.13/30  swp3.100 10.201.0.2/31 swp4.100 10.201.0.6/31  swp3.200 10.201.1.2/31 swp4.200 10.201.1.6/31
leaf-border-1 has 192.0.0.8 configured anywhere? 0
dc2-border-1 has 192.0.0.8 configured anywhere? 0
```

VTEP loopbacks behind the FIB next-hops (`ip -4 -br addr show lo`):

```
leaf-border-1 lo: 10.0.0.17/32
leaf-border-2 lo: 10.0.0.18/32
dc2-k8s-leaf-1 lo: 10.2.0.11/32
dc2-k8s-leaf-2 lo: 10.2.0.12/32
dc2-border-1 lo: 10.2.0.15/32
dc2-border-2 lo: 10.2.0.16/32
```

FIB at each decision point (`vtysh -c "show ip route vrf tenant-k8s 10.168.10.11"`):

```
--- leaf-k8s-master-1 ---
Routing entry for 10.168.10.11/32
  Known via "bgp", distance 20, metric 0, vrf tenant-k8s, best
  * 10.0.0.17, via vlan3001_l3 onlink, weight 1
  * 10.0.0.18, via vlan3001_l3 onlink, weight 1
--- leaf-border-1 ---
Routing entry for 10.168.10.11/32
  Known via "bgp", distance 20, metric 0, vrf tenant-k8s, best
  * fe80::a8c1:abff:fe0b:f1f1, via swp3.100, weight 1
  * fe80::a8c1:abff:feb7:d029, via swp4.100, weight 1
--- br-agg-sw-1 ---
Routing entry for 10.168.10.11/32
  Known via "bgp", distance 20, metric 0, vrf tenant-k8s, best
  * fe80::a8c1:abff:fe15:5db7, via swp5.100, weight 1
  * fe80::a8c1:abff:fe4b:4b47, via swp6.100, weight 1
--- dc2-border-1 ---
Routing entry for 10.168.10.11/32
  Known via "bgp", distance 20, metric 0, vrf tenant-k8s, best
  * 10.2.0.11, via vlan3101_l3 onlink, weight 1
  * 10.2.0.12, via vlan3101_l3 onlink, weight 1
--- dc2-k8s-leaf-1 ---
Routing entry for 10.168.10.0/24
  Known via "connected", distance 0, metric 1024, vrf tenant-k8s
  * directly connected, vlan210-v0
```

VNIs in play (`vtysh -c "show evpn vni"`):

```
DC1 leaf (leaf-k8s-master-1) tenant-k8s L3VNI: 50001
DC2 border (dc2-border-1) L3VNIs: 50101 (tenant-k8s), 50102 (tenant-svc)
DC2 k8s leaf (dc2-k8s-leaf-1): 10210 L2 tenant-k8s; 50101 L3 tenant-k8s
```

Destination MAC in the DC2 leaves' EVPN table (`vtysh -c "show evpn mac vni 10210 mac 50:00:00:1f:00:01"`):

```
--- dc2-k8s-leaf-1 ---
MAC: 50:00:00:1f:00:01
 ESI: 03:44:38:39:be:ef:19:00:00:01
 Intf: bond1(12) VLAN: 210
--- dc2-k8s-leaf-2 ---
MAC: 50:00:00:1f:00:01
 ESI: 03:44:38:39:be:ef:19:00:00:01
 Intf: bond1(13) VLAN: 210
```

Hosts:

```
k8s-master-1:     default via 10.167.10.1 dev bond0   (802.3ad, hash layer3+4)
dc2-k8s-master-1: default via 10.168.10.1 dev bond0
reply TTL at the source: 3 of 3 pings ttl=59
```

## 7. Companion paths from the same session

East-west from inside a pod (the demo app's own `/api/trace?region=DC2`, DC1 pod to the DC2 NodePort target 10.168.10.21): pod 10.244.3.124 -> Cilium node gateway -> 10.167.20.3 (leaf-k8s-worker-2 SVI) -> 192.0.0.8 (DC1 border) -> 10.201.20.1 (br-agg-sw-1) -> 192.0.0.8 (DC2 border) -> 10.168.10.3 (dc2-k8s-leaf-2 SVI) -> 10.168.10.21 (DC2 node), then local NodePort delivery to the peer pod. Eight routed hops, all sub-millisecond, same leaf / border / agg / border / leaf structure with the pod's Cilium hop in front.

North-south from the external client to the anycast VIP: traceroute shows all stars, with UDP and with ICMP probes alike, because the PA policy admits only HTTP to the DNAT VIP and the firewall does not emit TTL-exceeded for transit traffic. Proven instead from the firewall's session table while client-1 fetched the VIP: source 10.80.15.103, destination 10.80.15.50, NAT to 192.168.202.0, zone internet -> k8s, ingress ethernet1/3, egress ethernet1/1.100, application web-browsing, state ACTIVE. Raw output in `raw/ecloud-e2e-trace-20260821.txt`.

## 8. Method

Read-only throughout. Traceroutes run from inside the host containers with `traceroute -n -w1 -q3 -m10` (multi-probe) and `-q1 -p <port>` (per-flow). Device state via ssh as `admin` (`ip -br addr`, `sudo vtysh -c ...`). Reply TTL from the source's own `ping` output. No configuration was changed and no endpoints were added for this audit.
