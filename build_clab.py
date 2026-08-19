#!/usr/bin/env python3
"""Generate ecloud.clab.yml from the authoritative EVE link map (captured live 2026-08-17).
Every link below is a real EVE p2p network; nothing is inferred."""
import os

# mgmt /24 for every node (static, no DHCP drift). Picked automatically so a plain `python3 build_clab.py`
# does the right thing on any host, no per-host override to remember:
#   1. CLAB_MGMT_PREFIX env, if set (explicit always wins)
#   2. .mgmt-prefix file next to this script, if present (per-host pin, gitignored)
#   3. auto: the first candidate /24 that does NOT already belong to a local interface on this host.
#      On an EVE-NG box 172.29.129.0/24 IS the nat0 cloud bridge, so the default there is taken.
# A routing-table collision (VPN/Tailscale subnet route) is NOT a local interface and is left to the
# ip-rule fix in the README; only a genuinely local /24 makes us move.
def _pick_mgmt():
    env = os.environ.get("CLAB_MGMT_PREFIX")
    if env: return env, "env"
    here = os.path.dirname(os.path.abspath(__file__))
    pin = os.path.join(here, ".mgmt-prefix")
    if os.path.isfile(pin):
        v = open(pin).read().strip()
        if v: return v, ".mgmt-prefix"
    taken, ours = set(), set()
    try:
        import subprocess
        for ln in subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True).stdout.splitlines():
            p = ln.split()
            if len(p) >= 4 and "/" in p[3]:
                o = p[3].split("/")[0].split(".")
                if len(o) != 4: continue
                pfx = ".".join(o[:3])
                # a docker bridge (br-<id>) holding a candidate is the ALREADY-DEPLOYED lab's own
                # mgmt network: reuse it so a regen never moves a running lab to a new subnet.
                (ours if p[1].startswith("br-") else taken).add(pfx)
    except Exception:
        pass
    cands = ("172.29.129", "172.29.140", "172.29.141", "172.29.142", "172.29.150")
    for cand in cands:
        if cand in ours: return cand, "auto (reusing the deployed lab's mgmt bridge)"
    for cand in cands:
        if cand not in taken: return cand, ("default" if cand == "172.29.129" else "auto (172.29.129 is local on this host)")
    return "172.29.129", "fallback"
MGMT, MGMT_HOW = _pick_mgmt()

CVX_IMG = "vrnetlab/nvidia_cumulus-vx:5.12.0"
PAN_IMG = "vrnetlab/paloalto_pa-vm:12.1.2"
# pinned by digest: the floating :latest resolved to different ages on different hosts (dnsmasq 2.90 vs 2.92)
HOST_IMG = "ghcr.io/srl-labs/network-multitool@sha256:5013e4b58ec2d72affad2c3adc4ee807157efe452d31022e87317d00e94620f4"

# ---------- nodes ----------
switches = {
 # DC1 (10.0.0.x VTEPs)
 "spine-1":"dc1","spine-2":"dc1",
 "leaf-k8s-master-1":"dc1","leaf-k8s-master-2":"dc1",
 "leaf-k8s-worker-1":"dc1","leaf-k8s-worker-2":"dc1",
 "leaf-service-1":"dc1","leaf-service-2":"dc1",
 "leaf-border-1":"dc1","leaf-border-2":"dc1",
 # DC2 (10.2.0.x VTEPs)
 "dc2-spine-1":"dc2","dc2-spine-2":"dc2",
 "dc2-k8s-leaf-1":"dc2","dc2-k8s-leaf-2":"dc2",
 "dc2-svc-leaf-1":"dc2","dc2-svc-leaf-2":"dc2",
 "dc2-border-1":"dc2","dc2-border-2":"dc2",
 # backbone
 "br-agg-sw-1":"aggr","br-agg-sw-2":"aggr",
}
firewalls = ["fw-pri","fw-sec"]
hosts = ["k8s-master-1","k8s-master-2","k8s-master-3","k8s-woker-1","k8s-woker-2","k8s-woker-3",
         "service-node-dns-ntp","client-1","external-client-2","gobgp-1","gobgp-2",
         "dc2-k8s-master-1","dc2-k8s-worker-1","dc2-k8s-worker-2","dc2-k8s-worker-3","dc2-svc-ntp-dns"]

# EVE name -> clab name (EVE names had inconsistent prefixes)
alias = {"DC-2-spine-1":"dc2-spine-1","DC-2-spine-2":"dc2-spine-2",
         "DC-2-k8s-leaf-1":"dc2-k8s-leaf-1","DC-2-k8s-leaf-2":"dc2-k8s-leaf-2",
         "DC-2-k8s-svc-1":"dc2-svc-leaf-1","DC-2-k8s-svc-2":"dc2-svc-leaf-2",
         "DC-2-k8s-border-1":"dc2-border-1","DC-2-k8s-border-2":"dc2-border-2",
         "DC2-k8s-master-1":"dc2-k8s-master-1","DC2-k8s-worker-1":"dc2-k8s-worker-1",
         "DC2-k8s-worker-2":"dc2-k8s-worker-2","DC2-k8s-worker-3":"dc2-k8s-worker-3",
         "DC2-svc-ntp-dns":"dc2-svc-ntp-dns",
         "PaloAlto-pri-active-active-1":"fw-pri","PaloAlto-sec-active-active-2":"fw-sec",
         "GOBGP-controller-1":"gobgp-1","GOBGP-controller-2":"gobgp-2",
         "client":"client-1"}
# host NIC name -> clab eth index (hosts: data0->eth1, data1->eth2; PAN eth1/N->ethN)
def ifname(node, ifn):
    if node in firewalls:
        return "eth"+ifn.split("/")[1]          # eth1/1 -> eth1
    if node in hosts:
        return {"data0":"eth1","data1":"eth2","data":"eth1"}[ifn]
    return ifn                                  # swpN stays swpN

# ---------- the authoritative EVE p2p link map ----------
RAW = """
spine-1:swp1 leaf-k8s-master-1:swp1
spine-1:swp2 leaf-k8s-master-2:swp1
spine-1:swp3 leaf-k8s-worker-1:swp1
spine-1:swp4 leaf-k8s-worker-2:swp1
spine-1:swp5 leaf-service-1:swp1
spine-1:swp6 leaf-service-2:swp1
spine-1:swp7 leaf-border-1:swp1
spine-1:swp8 leaf-border-2:swp1
spine-2:swp1 leaf-k8s-master-1:swp2
spine-2:swp2 leaf-k8s-master-2:swp2
spine-2:swp3 leaf-k8s-worker-1:swp2
spine-2:swp4 leaf-k8s-worker-2:swp2
spine-2:swp5 leaf-service-1:swp2
spine-2:swp6 leaf-service-2:swp2
spine-2:swp7 leaf-border-1:swp2
spine-2:swp8 leaf-border-2:swp2
leaf-k8s-master-1:swp3 k8s-master-1:data0
leaf-k8s-master-2:swp3 k8s-master-1:data1
leaf-k8s-master-1:swp4 k8s-master-2:data0
leaf-k8s-master-2:swp4 k8s-master-2:data1
leaf-k8s-master-1:swp5 k8s-master-3:data0
leaf-k8s-master-2:swp5 k8s-master-3:data1
leaf-k8s-worker-1:swp3 k8s-woker-1:data0
leaf-k8s-worker-2:swp3 k8s-woker-1:data1
leaf-k8s-worker-1:swp4 k8s-woker-2:data0
leaf-k8s-worker-2:swp4 k8s-woker-2:data1
leaf-k8s-worker-1:swp5 k8s-woker-3:data0
leaf-k8s-worker-2:swp5 k8s-woker-3:data1
leaf-service-1:swp3 service-node-dns-ntp:data0
leaf-service-2:swp3 service-node-dns-ntp:data1
leaf-border-1:swp3 br-agg-sw-1:swp1
leaf-border-2:swp3 br-agg-sw-1:swp2
PaloAlto-sec-active-active-2:eth1/1 br-agg-sw-1:swp4
leaf-border-2:swp4 br-agg-sw-2:swp2
PaloAlto-pri-active-active-1:eth1/1 br-agg-sw-1:swp3
DC-2-k8s-border-2:swp3 br-agg-sw-1:swp6
leaf-border-1:swp4 br-agg-sw-2:swp1
PaloAlto-sec-active-active-2:eth1/2 br-agg-sw-2:swp4
DC-2-k8s-border-1:swp3 br-agg-sw-1:swp5
PaloAlto-pri-active-active-1:eth1/2 br-agg-sw-2:swp3
DC-2-k8s-border-1:swp4 br-agg-sw-2:swp5
PaloAlto-pri-active-active-1:eth1/5 PaloAlto-sec-active-active-2:eth1/5
PaloAlto-pri-active-active-1:eth1/6 PaloAlto-sec-active-active-2:eth1/6
PaloAlto-pri-active-active-1:eth1/7 PaloAlto-sec-active-active-2:eth1/7
DC-2-spine-1:swp1 DC-2-k8s-leaf-1:swp1
DC-2-spine-1:swp2 DC-2-k8s-leaf-2:swp1
DC-2-spine-1:swp3 DC-2-k8s-svc-1:swp1
DC-2-spine-1:swp4 DC-2-k8s-svc-2:swp1
DC-2-spine-1:swp5 DC-2-k8s-border-1:swp1
DC-2-spine-1:swp6 DC-2-k8s-border-2:swp1
DC-2-spine-2:swp1 DC-2-k8s-leaf-1:swp2
DC-2-spine-2:swp2 DC-2-k8s-leaf-2:swp2
DC-2-spine-2:swp3 DC-2-k8s-svc-1:swp2
DC-2-spine-2:swp4 DC-2-k8s-svc-2:swp2
DC-2-spine-2:swp5 DC-2-k8s-border-1:swp2
DC-2-spine-2:swp6 DC-2-k8s-border-2:swp2
DC-2-k8s-leaf-1:swp3 DC2-k8s-master-1:data0
DC-2-k8s-leaf-2:swp3 DC2-k8s-master-1:data1
DC-2-k8s-leaf-1:swp4 DC2-k8s-worker-1:data0
DC-2-k8s-leaf-2:swp4 DC2-k8s-worker-1:data1
DC-2-k8s-leaf-1:swp5 DC2-k8s-worker-2:data0
DC-2-k8s-leaf-2:swp5 DC2-k8s-worker-2:data1
DC-2-k8s-leaf-1:swp6 DC2-k8s-worker-3:data0
DC-2-k8s-leaf-2:swp6 DC2-k8s-worker-3:data1
DC-2-k8s-svc-1:swp3 DC2-svc-ntp-dns:data0
DC-2-k8s-svc-2:swp3 DC2-svc-ntp-dns:data1
DC-2-k8s-border-2:swp4 br-agg-sw-2:swp6
GOBGP-controller-1:data0 br-agg-sw-1:swp7
GOBGP-controller-1:data1 br-agg-sw-2:swp7
br-agg-sw-1:swp8 GOBGP-controller-2:data0
br-agg-sw-2:swp8 GOBGP-controller-2:data1
"""
def norm(ep):
    n,i = ep.rsplit(":",1); n = alias.get(n,n); return n, ifname(n,i)

links=[]
for line in RAW.strip().splitlines():
    a,b = line.split()
    (an,ai),(bn,bi) = norm(a),norm(b)
    links.append((an,ai,bn,bi))

# ---------- emit YAML ----------
out=[]
out.append("# ecloud two-DC EVPN-VXLAN POC, replicated in containerlab.")
out.append("# Generated from the EVE-NG link map captured live via the EVE API (84 networks audited;")
out.append("# 71 p2p links + the shared internet segment carried into clab; mgmt-only nets dropped).")
out.append("# Switches: Cumulus VX 5.12.0 (vrnetlab VM-in-container). Firewalls: PA-VM 12.1.2 (vrnetlab).")
out.append("# Hosts: plain linux containers, provisioned after deploy (k8s/Cilium, BIND, GoBGP, clients).")
out.append("# NOTE: stock nvidia_cumulusvx does NOT apply startup-config. OUR image carries a patched vrnetlab")
out.append("# launcher (cvx_startup_config.patch.py) that applies /config/startup-config.cfg over SSH once switchd")
out.append("# is up, so the fabric BOOTSTRAPS ITSELF on `clab deploy`. bootstrap/*.cfg = filtered nv-set lines")
out.append("# (no eth0/mgmt/hostname/REDACTED/aaa-user) from fabric-evpn-mh/. Creds cumulus/Clab123!.")
out.append("name: ecloud")
out.append("mgmt:")
out.append("  network: ecloud-mgmt")
out.append(f"  ipv4-subnet: {MGMT}.0/24     # static mgmt /24 (override: CLAB_MGMT_PREFIX)")
out.append("topology:")
out.append("  kinds:")
out.append("    nvidia_cumulusvx:")
out.append(f"      image: {CVX_IMG}")
out.append("    paloalto_panos:")
out.append(f"      image: {PAN_IMG}")
out.append("    linux:")
out.append(f"      image: {HOST_IMG}")
out.append("  nodes:")
# switches with startup configs from the repo (fabric-evpn-mh)
mgmt_ip = {}
ipn = 11
for name,dc in switches.items():
    mgmt_ip[name] = f"{MGMT}.{ipn}"; ipn += 1
    out.append(f"    {name}:")
    out.append(f"      kind: nvidia_cumulusvx")
    out.append(f"      mgmt-ipv4: {mgmt_ip[name]}")
    out.append(f"      group: {dc}")
    out.append(f"      startup-config: bootstrap/{name}.cfg   # applied at FIRST BOOT by the patched launcher")
    out.append(f"      binds:")
    out.append(f"        - bootstrap/hosts:/bootstrap-keys:ro   # optional authorized_keys, installed by the launcher")
for fw in firewalls:
    mgmt_ip[fw] = f"{MGMT}.{ipn}"; ipn += 1
    out.append(f"    {fw}:")
    out.append(f"      kind: paloalto_panos")
    out.append(f"      mgmt-ipv4: {mgmt_ip[fw]}")
    out.append(f"      group: firewall")
    # the vrnetlab PAN launcher natively applies /config/startup-config.cfg (set-format lines, then commit)
    out.append(f"      startup-config: bootstrap/{fw}.xml   # FULL live <config> XML (interfaces/VR+BGP/HA/zones/NAT/security), loaded via API")
    # containerlab's paloalto_panos kind hard-codes QEMU_CPU=qemu64 (nodes/vr_pan/vr-pan.go:66). qemu64 lacks
    # x86-64-v2 (SSE4.2/POPCNT/CX16) and PAN-OS 12 glibc dies with "CPU does not support x86-64-v2" +
    # rcu stalls. The kind merges user env OVER its defaults, so override to the host CPU model.
    out.append(f"      env:")
    out.append(f"        QEMU_CPU: host")
    # vrnetlab hard-codes ram=6144/smp=2 for PAN; PAN-OS 12.1 fails its resource checks at that size and
    # drops into the Maintenance Recovery Tool. The working EVE-NG PANs run 8 vCPU / 16 GB (verified from
    # the live qemu cmdline). Match them via vrnetlab's QEMU_SMP / QEMU_MEMORY env overrides.
    out.append(f"        QEMU_SMP: 8")
    out.append(f"        QEMU_MEMORY: 16384")
    out.append(f"      binds:")
    out.append(f"        - bootstrap/hosts:/bootstrap-keys:ro   # optional authorized_keys, folded into the config commit")
# ---- hosts: every one bootstraps at first start via exec of its own script (bind-mounted) ----
import importlib.util, sys as _sys
_spec = importlib.util.spec_from_file_location("hosts", os.path.join(os.path.dirname(__file__), "hosts.py"))
H = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(H)
def host_node(h, grp, kind="linux", extra=None):
    out.append(f"    {h}:")
    out.append(f"      kind: {kind}")
    if kind == "linux":
        out.append(f"      mgmt-ipv4: {mgmt_ip[h]}")
    out.append(f"      group: {grp}")
    if extra:
        for e in extra: out.append(f"      {e}")
    out.append(f"      binds:")
    out.append(f"        - bootstrap/hosts:/bootstrap:ro")
    out.append(f"      exec:")
    out.append(f"        - sh /bootstrap/{h}.sh")
def k8s_node(h, grp):
    # k8s nodes run the custom ecloud-k8s-host image; its PID-1 entrypoint does the cgroup-v2
    # evacuation, builds bond0 from env, and launches k3s by role. No bind/exec networking.
    env = H.k8s_env(h)
    out.append(f"    {h}:")
    out.append(f"      kind: linux")
    out.append(f"      image: ecloud-k8s-host:1.31.5")
    out.append(f"      mgmt-ipv4: {mgmt_ip[h]}")
    out.append(f"      group: {grp}")
    out.append(f"      binds:")
    out.append(f"        - bootstrap/hosts:/bootstrap:ro        # optional authorized_keys for the entrypoint")
    out.append(f"      env:")
    for k,v in env.items():
        out.append(f"        {k}: \"{v}\"")
for h in hosts:
    mgmt_ip[h] = f"{MGMT}.{ipn}"; ipn += 1
    if h in H.K8S_NODES:    k8s_node(h, "dc2-k8s" if h.startswith("dc2") else "dc1-k8s")
    elif h in H.CLIENTS:    host_node(h, "clients")
    elif h in H.GOBGP:      host_node(h, "controllers")
    elif h in H.BONDED:     host_node(h, "dc2-hosts" if h.startswith("dc2") else "dc1-hosts")
    else:                   host_node(h, "hosts")
# the shared 'internet' segment (EVE net54): both PAN eth1/3 + both clients. clab has no multi-point link,
# so model it as a bridge node.
out.append("    internet:")
out.append("      kind: bridge     # = EVE net54 (10.80.15.0/24): PAN eth1/3 x2 + client-1 + external-client-2")
out.append("  links:")
for an,ai,bn,bi in links:
    out.append(f"    - endpoints: [\"{an}:{ai}\", \"{bn}:{bi}\"]")
# shared segment members onto the bridge
for n,i in [("fw-pri","eth3"),("fw-sec","eth3"),("client-1","eth1"),("external-client-2","eth1")]:
    out.append(f"    - endpoints: [\"{n}:{i}\", \"internet:{n}\"]")

y="\n".join(out)+"\n"
open(os.path.join(os.path.dirname(__file__),"ecloud.clab.yml"),"w").write(y)
print(f"mgmt: {MGMT}.0/24  [{MGMT_HOW}]")
print(f"nodes: {len(switches)} switches + {len(firewalls)} fw + {len(hosts)} hosts + 1 bridge = {len(switches)+len(firewalls)+len(hosts)+1}")
print(f"links: {len(links)} p2p + 4 bridge = {len(links)+4}")
print("mgmt map:"); [print(f"  {k:24} {v}") for k,v in mgmt_ip.items()]
