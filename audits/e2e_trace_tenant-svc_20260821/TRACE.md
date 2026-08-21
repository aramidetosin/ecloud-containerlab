# End-to-end data-plane trace: DC1 service node to DC2 service node (tenant-svc)

Audit date: 2026-08-21. Lab: `ecloud-containerlab` deployed from commit `25c22b5` ("Enable eBGP ECMP on border leaves; add fabric audits"), cold-deployed from a fresh clone on the office EVE host (deploy 11:12:57 UTC, 22/22 health checks passed 11:23:01 UTC). Same session as the tenant-k8s trace in `audits/e2e_trace_tenant-k8s_20260821/`; this one proves the tenant-svc half of the border ECMP fix in the data plane. Every device identity and every ECMP claim below was verified on the box at the time of the trace; nothing is inferred from the topology file.

Source: `service-node-dns-ntp` (DC1), `bond0` = 10.167.30.10/24, default via 10.167.30.1, bond mode 802.3ad, transmit hash policy layer3+4.
Destination: `dc2-svc-ntp-dns` (DC2), `bond0` = 10.168.30.10/24 (MAC 50:00:00:23:00:01), default via 10.168.30.1.
VRF on every routed hop: `tenant-svc`.

Raw captures for everything quoted here are in `raw/` next to this file.

## 1. The live traceroute, decoded

| Hop | Reported IP | Actual device | What happens there |
|---|---|---|---|
| - | - | service-node-dns-ntp | Default route to the anycast GW 10.167.30.1 on bond0. The 802.3ad bond hashes per flow (layer3+4) across both uplinks, so hop 1 alternates between .2 and .3: the 3-probe run landed on both, and the single-flow runs pinned to one or the other. |
| 1 | 10.167.30.2 / 10.167.30.3 | leaf-service-1 / leaf-service-2 | Anycast GW (VRR 10.167.30.1 shared; the reply comes from each leaf's real SVI: verified `vlan130` = 10.167.30.2/24 on leaf-service-1, 10.167.30.3/24 on leaf-service-2). VRF tenant-svc lookup for the destination host /32: `10.168.30.10/32 via 10.0.0.17 AND 10.0.0.18, vlan3002_l3 onlink` = both DC1 border loopbacks (verified lo 10.0.0.17 on leaf-border-1, 10.0.0.18 on leaf-border-2), 2-way ECMP, then VXLAN encapsulation into L3VNI 50002 (tenant-svc's own L3VNI, distinct from tenant-k8s's 50001). |
| - | (invisible) | spine-1 / spine-2 | Underlay only. Routes the outer VXLAN packet to the hashed border VTEP and never touches the inner TTL. |
| 2 | 192.0.0.8 | leaf-border-1 or leaf-border-2 | Decapsulation, then VRF tenant-svc lookup: `10.168.30.10/32 via swp3.200 AND swp4.200`, next-hops `fe80::a8c1:abff:fe0b:f1f1` and `fe80::a8c1:abff:feb7:d029`, i.e. unnumbered eBGP to br-agg-sw-1 and br-agg-sw-2 on the VLAN 200 dot1q subinterfaces (tenant-svc's DCI VLAN; tenant-k8s uses .100 on the same physical links), 2-way ECMP. 192.0.0.8 is configured on no interface of either border (checked: zero matches in `ip addr` on both). It is the RFC 7600 IPv4 dummy address the Linux kernel uses to source ICMP from an interface that has no IPv4 address: the .200 subinterfaces carry only IPv6 link-locals. |
| 3 | 10.201.1.0 / 10.201.1.2 | br-agg-sw-1 / br-agg-sw-2 | VRF-lite DCI. VRF tenant-svc lookup: `10.168.30.10/32 via swp5.200 AND swp6.200` (unnumbered, fe80 next-hops) = both DC2 borders, 2-way ECMP. The reply source is each agg's first tenant-svc VRF address, which is its firewall-transit /31 (verified: 10.201.1.0/31 on br-agg-sw-1 `swp3.200`, 10.201.1.2/31 on br-agg-sw-2 `swp3.200`), not the interface the packet left on. ICMP source-address selection; in tenant-k8s the same hop answered from the gobgp-facing /30 instead, for the same reason. |
| 4 | 192.0.0.8 | dc2-border-1 or dc2-border-2 | Re-enters EVPN. VRF tenant-svc lookup: `10.168.30.10/32 via 10.2.0.13 AND 10.2.0.14, vlan3102_l3 onlink` = both DC2 svc leaf loopbacks (verified lo 10.2.0.13 on dc2-svc-leaf-1, 10.2.0.14 on dc2-svc-leaf-2), 2-way ECMP, then VXLAN encapsulation into L3VNI 50102 (tenant-svc's DC2 L3VNI; 50101 is tenant-k8s's). Same unnumbered dummy source as hop 2: zero matches for 192.0.0.8 on either DC2 border. |
| - | (invisible) | dc2-spine-1 / dc2-spine-2 | DC2 underlay transit. |
| 5 | 10.168.30.3 / 10.168.30.2 | dc2-svc-leaf-2 / dc2-svc-leaf-1 | Decapsulation. `10.168.30.0/24` is connected on `vlan230-v0` (verified `vlan230` = 10.168.30.2/24 on dc2-svc-leaf-1, 10.168.30.3/24 on dc2-svc-leaf-2). EVPN MAC table for VNI 10230 / VLAN 230: `50:00:00:23:00:01` is local on `bond1`, ESI 03:44:38:39:be:ef:1a:00:00:01, present as local on BOTH leaves (the destination is dual-homed on an EVPN-MH Ethernet segment). L2 delivery out the host bond. |
| 6 | 10.168.30.10 | dc2-svc-ntp-dns | Destination. Its own default is 10.168.30.1 on bond0, so the return walks the mirror of this path. |

Arriving TTL: every echo reply reaches the source with `ttl=59`, i.e. exactly 5 routing hops on the return path (64 - 59), identical to the tenant-k8s trace: egress leaf, DC2 border, agg, DC1 border, ingress leaf. tenant-svc rides the very same physical path through the same devices, just in its own parallel slice: own L2 VNIs (10130 / 10230), own L3VNIs (50002 / 50102), own DCI VLAN (200), own SVIs.

The firewalls are not in this east-west path. Even though hop 3's reply addresses come from the aggs' firewall-facing /31s, every decision point's FIB next-hops are border, agg, or leaf interfaces inside tenant-svc (`swp3/4.200` between DC1 borders and aggs, `swp5/6.200` between aggs and DC2 borders, the `vlan3002_l3` / `vlan3102_l3` onlink VTEP routes inside each DC). The packets never traverse fw-pri or fw-sec; that is purely ICMP source-address selection.

## 2. The control plane behind it

Same re-origination story as tenant-k8s, one VRF over: DC2's `10.168.30.0/24` and its host `/32` routes leave DC2's EVPN domain at dc2-border-1/2, cross the DCI as plain eBGP IPv4 unicast inside tenant-svc on the VLAN-200 subinterfaces (unnumbered sessions, IPv6 link-local next-hops), and are re-originated by the DC1 borders into L3VNI 50002 as type-5 routes. Host-granular /32s propagate end to end: the DC1 ingress leaf holds `10.168.30.10/32` itself ("Known via bgp").

## 3. ECMP status, per decision point

Commit `25c22b5` raised `multipaths ebgp` from 1 to 4 for tenant-svc on all four borders, exactly as it did for tenant-k8s. Live FIB at the time of the trace:

| Decision point | Route | Next-hops | ECMP |
|---|---|---|---|
| leaf-service-1 (ingress leaf) | 10.168.30.10/32 | 10.0.0.17, 10.0.0.18 via vlan3002_l3 onlink | 2-way (both DC1 borders) |
| leaf-border-1 (DC1 border) | 10.168.30.10/32 | fe80::a8c1:abff:fe0b:f1f1 via swp3.200, fe80::a8c1:abff:feb7:d029 via swp4.200 | 2-way (both aggs): the row the commit fixed |
| br-agg-sw-1 (backbone) | 10.168.30.10/32 | fe80::a8c1:abff:fe15:5db7 via swp5.200, fe80::a8c1:abff:fe4b:4b47 via swp6.200 | 2-way (both DC2 borders) |
| dc2-border-1 (DC2 border) | 10.168.30.10/32 | 10.2.0.13, 10.2.0.14 via vlan3102_l3 onlink | 2-way (both DC2 svc leaves) |
| dc2-svc-leaf-1 (egress leaf) | 10.168.30.0/24 | directly connected, vlan230-v0 | local delivery |

Observed in the data plane across the sampled flows: both ingress leaves (.2 and .3 at hop 1), both aggs (10.201.1.0 and 10.201.1.2 at hop 3), both egress leaves (10.168.30.2 and 10.168.30.3 at hop 5). ECMP is fully symmetric with tenant-k8s: before the fix, this tenant had the same `maximum-paths 1` pinning on the borders that k8s did. Each flow stays pinned to one consistent path, so there is no reordering.

## 4. Multi-probe trace (3 probes per hop), verbatim

```
traceroute to 10.168.30.10 (10.168.30.10), 10 hops max, 46 byte packets
 1  10.167.30.2  0.227 ms  10.167.30.3  0.211 ms  0.056 ms
 2  192.0.0.8  0.632 ms  0.410 ms  0.248 ms
 3  10.201.1.0  0.352 ms  0.215 ms  0.176 ms
 4  192.0.0.8  0.511 ms  0.251 ms  0.242 ms
 5  10.168.30.3  0.804 ms  10.168.30.2  0.866 ms  0.445 ms
 6  10.168.30.10  0.490 ms  0.581 ms  0.969 ms
```

Decoded:

```
 1  10.167.30.2 / 10.167.30.3   both DC1 svc leaves (leaf-service-1 / -2)
 2  192.0.0.8                   DC1 border (leaf-border-1 or -2, unnumbered)
 3  10.201.1.0                  br-agg-sw-1 (this probe set; the flow runs below also hit agg-2)
 4  192.0.0.8                   DC2 border (dc2-border-1 or -2)
 5  10.168.30.3 / 10.168.30.2   BOTH DC2 svc leaves (dc2-svc-leaf-2 / -1)
 6  10.168.30.10                destination, 0% loss
```

## 5. Per-flow pinning (one flow per run, different UDP destination ports)

Each run is one traceroute process with a single probe per hop (`-q1`) and a distinct UDP base port (`-p`). Each process also picks a fresh UDP source port, so every run is a genuinely new 5-tuple; the same `-p` value can legitimately hash to a different member in a later run.

| Flow | `-p` | Ingress leaf (hop 1) | Agg used (hop 3) | Egress leaf (hop 5) |
|---|---|---|---|---|
| 1 | 33434 | leaf-service-2 (.3) | br-agg-sw-1 (10.201.1.0) | dc2-svc-leaf-2 (.3) |
| 2 | 33500 | leaf-service-1 (.2) | br-agg-sw-2 (10.201.1.2) | dc2-svc-leaf-2 (.3) |
| 3 | 33600 | leaf-service-2 (.3) | br-agg-sw-1 (10.201.1.0) | dc2-svc-leaf-2 (.3) |
| 4 | 33700 | leaf-service-1 (.2) | br-agg-sw-1 (10.201.1.0) | dc2-svc-leaf-1 (.2) |

Verbatim:

```
--- flow port 33434 ---
 1  10.167.30.3  0.115 ms
 2  192.0.0.8  0.535 ms
 3  10.201.1.0  0.307 ms
 4  192.0.0.8  0.683 ms
 5  10.168.30.3  0.621 ms
 6  10.168.30.10  0.771 ms
--- flow port 33500 ---
 1  10.167.30.2  0.173 ms
 2  192.0.0.8  0.512 ms
 3  10.201.1.2  0.572 ms
 4  192.0.0.8  0.572 ms
 5  10.168.30.3  0.659 ms
 6  10.168.30.10  0.504 ms
--- flow port 33600 ---
 1  10.167.30.3  0.138 ms
 2  192.0.0.8  0.635 ms
 3  10.201.1.0  0.449 ms
 4  192.0.0.8  0.539 ms
 5  10.168.30.3  0.662 ms
 6  10.168.30.10  0.713 ms
--- flow port 33700 ---
 1  10.167.30.2  0.144 ms
 2  192.0.0.8  0.604 ms
 3  10.201.1.0  0.551 ms
 4  192.0.0.8  0.656 ms
 5  10.168.30.2  0.767 ms
 6  *
```

Flow 4's final probe timed out within the 2-second wait on this run (a probe timeout at the destination, not a path failure; the same flow's earlier hops and every other flow completed). The re-run completed and, being a new flow (new source port), hashed to agg-2 and egress leaf-1:

```
--- flow port 33700, re-run (-w2) ---
 1  10.167.30.3  0.208 ms
 2  192.0.0.8  0.763 ms
 3  10.201.1.2  0.527 ms
 4  192.0.0.8  0.680 ms
 5  10.168.30.2  1.045 ms
 6  10.168.30.10  0.881 ms
```

## 6. Identity and state verification, verbatim

Who owns each reported IP (`ip -br addr` on the device):

```
leaf-service-1 vlan130: 10.167.30.2/24
leaf-service-2 vlan130: 10.167.30.3/24
dc2-svc-leaf-1 vlan230: 10.168.30.2/24
dc2-svc-leaf-2 vlan230: 10.168.30.3/24
br-agg-sw-1: swp7 10.201.20.1/30  swp8 10.201.20.9/30   swp3.100 10.201.0.0/31 swp4.100 10.201.0.4/31  swp3.200 10.201.1.0/31 swp4.200 10.201.1.4/31
br-agg-sw-2: swp7 10.201.20.5/30  swp8 10.201.20.13/30  swp3.100 10.201.0.2/31 swp4.100 10.201.0.6/31  swp3.200 10.201.1.2/31 swp4.200 10.201.1.6/31
192.0.0.8 configured on leaf-border-1 / leaf-border-2 / dc2-border-1 / dc2-border-2: 0 / 0 / 0 / 0
leaf-border-1 DCI subinterfaces carry only IPv6 link-locals:
  swp3.100 fe80::a8c1:abff:fec0:8738/64   swp4.100 fe80::a8c1:abff:feb4:eeac/64
  swp3.200 fe80::a8c1:abff:fec0:8738/64   swp4.200 fe80::a8c1:abff:feb4:eeac/64
```

VTEP loopbacks behind the FIB next-hops (`ip -4 -br addr show lo`):

```
leaf-border-1 lo: 10.0.0.17/32
leaf-border-2 lo: 10.0.0.18/32
dc2-border-1 lo: 10.2.0.15/32
dc2-border-2 lo: 10.2.0.16/32
dc2-svc-leaf-1 lo: 10.2.0.13/32
dc2-svc-leaf-2 lo: 10.2.0.14/32
```

FIB at each decision point (`vtysh -c "show ip route vrf tenant-svc 10.168.30.10"`):

```
--- leaf-service-1 ---
Routing entry for 10.168.30.10/32
  Known via "bgp", distance 20, metric 0, vrf tenant-svc, best
  * 10.0.0.17, via vlan3002_l3 onlink, weight 1
  * 10.0.0.18, via vlan3002_l3 onlink, weight 1
--- leaf-border-1 ---
Routing entry for 10.168.30.10/32
  Known via "bgp", distance 20, metric 0, vrf tenant-svc, best
  * fe80::a8c1:abff:fe0b:f1f1, via swp3.200, weight 1
  * fe80::a8c1:abff:feb7:d029, via swp4.200, weight 1
--- br-agg-sw-1 ---
Routing entry for 10.168.30.10/32
  Known via "bgp", distance 20, metric 0, vrf tenant-svc, best
  * fe80::a8c1:abff:fe15:5db7, via swp5.200, weight 1
  * fe80::a8c1:abff:fe4b:4b47, via swp6.200, weight 1
--- dc2-border-1 ---
Routing entry for 10.168.30.10/32
  Known via "bgp", distance 20, metric 0, vrf tenant-svc, best
  * 10.2.0.13, via vlan3102_l3 onlink, weight 1
  * 10.2.0.14, via vlan3102_l3 onlink, weight 1
--- dc2-svc-leaf-1 ---
Routing entry for 10.168.30.0/24
  Known via "connected", distance 0, metric 1024, vrf tenant-svc
  * directly connected, vlan230-v0
```

VNIs in play (`vtysh -c "show evpn vni"`):

```
leaf-service-1:  10130 L2 tenant-svc; 50002 L3 tenant-svc
dc2-border-1 L3: 50102 tenant-svc; 50101 tenant-k8s
dc2-svc-leaf-1:  10230 L2 tenant-svc; 50102 L3 tenant-svc
```

Destination MAC in the DC2 svc leaves' EVPN table (`vtysh -c "show evpn mac vni 10230 mac 50:00:00:23:00:01"`):

```
--- dc2-svc-leaf-1 ---
MAC: 50:00:00:23:00:01
 ESI: 03:44:38:39:be:ef:1a:00:00:01
 Intf: bond1(8) VLAN: 230
--- dc2-svc-leaf-2 ---
MAC: 50:00:00:23:00:01
 ESI: 03:44:38:39:be:ef:1a:00:00:01
 Intf: bond1(8) VLAN: 230
```

Hosts:

```
service-node-dns-ntp: default via 10.167.30.1 dev bond0   (802.3ad, hash layer3+4)
dc2-svc-ntp-dns:      default via 10.168.30.1 dev bond0
reply TTL at the source: 3 of 3 pings ttl=59
```

## 7. Method

Read-only throughout. Traceroutes run from inside the host containers with `traceroute -n -w1 -q3 -m10` (multi-probe) and `-w2 -q1 -p <port>` (per-flow). Device state via ssh as `admin` (`ip -br addr`, `sudo vtysh -c ...`). Reply TTL from the source's own `ping` output. No configuration was changed and no endpoints were added for this audit.
