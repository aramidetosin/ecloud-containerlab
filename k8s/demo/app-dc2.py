#!/usr/bin/env python3
# dc-demo : anycast / region-picker / cross-region-consumer demo for the ecloud two-DC fabric.
# Single image, deployed in both DCs. All dynamic identity comes from the downward API.
import json, os, socket, subprocess, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

DC        = os.environ.get("DC", "DC?")
PEER_DC   = os.environ.get("PEER_DC", "DC?")
NODE      = os.environ.get("NODE_NAME", "unknown-node")
POD       = os.environ.get("POD_NAME", "unknown-pod")
POD_IP    = os.environ.get("POD_IP", "0.0.0.0")
PEER_PORT = os.environ.get("PEER_PORT", "30080")
PEER_NODES = [n.strip() for n in os.environ.get("PEER_NODES", "").split(",") if n.strip()]

# --- theme per DC (color coded) ---
THEME = {
    "DC1": {"primary": "#1565c0", "accent": "#0d47a1", "tint": "#e3f2fd", "kind": "orders"},
    "DC2": {"primary": "#ef6c00", "accent": "#e65100", "tint": "#fff3e0", "kind": "inventory"},
}
T = THEME.get(DC, {"primary": "#455a64", "accent": "#263238", "tint": "#eceff1", "kind": "data"})

# --- fabric topology (known addressing) for the path map ---
# transit device path node->...->node, per scope
LOCAL_PATH = [
    {"dev": NODE, "ip": POD_IP, "role": "source pod / node"},
    {"dev": f"{DC} leaf (anycast GW)", "ip": "10.16x.y.2", "role": "L3VNI symmetric IRB"},
    {"dev": f"{DC} leaf", "ip": "-", "role": "dest ToR"},
    {"dev": "dest pod", "ip": "-", "role": "destination"},
]
def xregion_path(peer_via="-", peer_node="-", peer_pod="-", peer_podip="-"):
    a, b = (DC, PEER_DC)
    return [
        {"dev": f"{a} pod", "ip": POD_IP, "role": f"source  ({POD})"},
        {"dev": f"{a} k8s-leaf", "ip": "10.167.10.2/10.168.10.2", "role": "VXLAN decap -> route"},
        {"dev": f"{a} border", "ip": "swp3.100", "role": "L3 transit to backbone"},
        {"dev": "br-agg (backbone)", "ip": "10.201.0.0", "role": "L3 routed transit  (NO firewall)"},
        {"dev": f"{b} border", "ip": "swp5.100", "role": "L3 transit into DC"},
        {"dev": f"{b} k8s-leaf", "ip": "-", "role": "VXLAN encap"},
        {"dev": f"{b} node", "ip": peer_via, "role": f"{peer_node}  ·  NodePort :30080 (last routed hop)"},
        {"dev": f"{b} POD", "ip": peer_podip, "role": f"{peer_pod}  ·  kube-proxy -> pod (local, not routed x-region)"},
    ]

def north_south_path(client_ip):
    a = DC
    leaf = "10.167.10.2" if DC == "DC1" else "10.168.10.2"
    return [
        {"dev": "client (you)", "ip": client_ip, "role": "your browser"},
        {"dev": "PaloAlto A/A firewall", "ip": "fw-pri / fw-sec", "role": "north-south  WITH firewall  (DNAT + policy)"},
        {"dev": "br-agg (backbone)", "ip": "10.201.0.0", "role": "L3 routed transit"},
        {"dev": f"{a} border", "ip": "swp3/swp4.100", "role": "L3 into the DC"},
        {"dev": f"{a} k8s-leaf", "ip": leaf, "role": "VXLAN encap"},
        {"dev": f"{a} node", "ip": "Cilium gw", "role": f"{NODE}  (= hop 1 of the trace)"},
        {"dev": f"{a} POD", "ip": POD_IP, "role": f"{POD}  ·  this app (trace source)"},
    ]

def local_dataset():
    kind = T["kind"]
    return {
        "dc": DC, "kind": kind, "generatedBy": {"node": NODE, "pod": POD, "podIP": POD_IP},
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "records": [{"id": f"{DC}-{kind[:3].upper()}-{i:03d}", "region": DC, "value": (i * 37) % 1000} for i in range(1, 6)],
    }

def whoami():
    return {"dc": DC, "node": NODE, "pod": POD, "podIP": POD_IP, "ts": time.strftime("%H:%M:%S")}

def traceroute(target):
    try:
        out = subprocess.run(["traceroute", "-n", "-q", "1", "-w", "1", "-m", "10", target],
                             capture_output=True, text=True, timeout=25)
        return out.stdout or out.stderr
    except FileNotFoundError:
        return "traceroute binary not present"
    except Exception as e:
        return f"traceroute error: {e}"

def trace_from_pod(target):
    """Traceroute from THIS pod, with the pod prepended as the source (traceroute never lists its own source;
    hop 1 is the node's Cilium gateway, so without this the pod is invisible)."""
    tr = traceroute(target)
    src = f" 0  {POD_IP:<15}  {POD}  [this pod = source]"
    lines = tr.split("\n")
    if lines and lines[0].lstrip().startswith("traceroute"):
        note = lines[0] + "  (hop 1 = node Cilium gw)"
        return note + "\n" + src + "\n" + "\n".join(lines[1:])
    return src + "\n" + tr

def fetch_peer(path):
    """Fetch a path from the peer DC over the backbone; try each peer node IP:NodePort."""
    last = None
    for ip in PEER_NODES:
        url = f"http://{ip}:{PEER_PORT}{path}"
        try:
            t0 = time.time()
            with urllib.request.urlopen(url, timeout=4) as r:
                body = json.loads(r.read().decode())
                return {"ok": True, "via": ip, "latency_ms": round((time.time() - t0) * 1000, 1), "data": body}
        except Exception as e:
            last = f"{ip}: {e}"
    return {"ok": False, "error": last or "no peer nodes configured"}

PAGE = """<!doctype html><html><head><meta charset=utf-8><title>__DC__ demo</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
 :root{--p:__PRIMARY__;--a:__ACCENT__;--t:__TINT__}
 *{box-sizing:border-box}body{margin:0;font-family:system-ui,Segoe UI,Roboto,sans-serif;background:#f5f6f8;color:#1a2027}
 header{background:var(--p);color:#fff;padding:18px 24px;display:flex;align-items:center;gap:16px}
 .badge{font-size:30px;font-weight:800;letter-spacing:1px;background:rgba(255,255,255,.18);padding:6px 16px;border-radius:10px}
 header .sub{opacity:.9}
 .wrap{max-width:1000px;margin:20px auto;padding:0 16px;display:grid;gap:18px}
 .card{background:#fff;border:1px solid #e3e6ea;border-radius:12px;padding:18px 20px;box-shadow:0 1px 3px rgba(0,0,0,.05)}
 .card h2{margin:0 0 12px;font-size:15px;text-transform:uppercase;letter-spacing:.5px;color:var(--a)}
 .kv{display:grid;grid-template-columns:130px 1fr;gap:6px 12px;font-size:15px}
 .kv b{color:#5b6570;font-weight:600}
 .pill{display:inline-block;padding:2px 10px;border-radius:20px;background:var(--t);color:var(--a);font-weight:700;font-size:13px}
 select,button{font-size:15px;padding:8px 12px;border-radius:8px;border:1px solid #cfd4da;background:#fff}
 button{background:var(--p);color:#fff;border:0;cursor:pointer;font-weight:600}
 button:hover{background:var(--a)}
 pre{background:#0d1117;color:#c9d1d9;padding:12px;border-radius:8px;overflow:auto;font-size:12.5px;line-height:1.5;max-height:280px}
 .path{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-top:6px}
 .hop{background:var(--t);border:1px solid var(--p);border-radius:8px;padding:6px 10px;font-size:12.5px;min-width:120px}
 .hop .d{font-weight:700;color:var(--a)}.hop .i{color:#5b6570;font-family:monospace}
 .arrow{color:var(--p);font-weight:800}
 .nofw{color:#1b7f3b;font-weight:700}
 .fwbadge{color:#c0362c;font-weight:700}
 .muted{color:#8a929b;font-size:13px}
 .grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
 @media(max-width:720px){.grid2{grid-template-columns:1fr}}
</style></head><body>
<header><span class=badge>__DC__</span><div style="flex:1"><div style="font-weight:700;font-size:18px">ecloud multi-DC demo</div>
 <div class=sub>anycast VIP &middot; region picker &middot; cross-region over the L3 backbone</div></div>
 <button onclick=copyTunnel() id=tbtn title="Copy the SSH tunnel command to reach this app from a Mac (Tailscale to eve-office)" style="color:#fff;background:transparent;border:1px solid rgba(255,255,255,.5);border-radius:8px;padding:8px 14px;font-weight:600;font-size:14px;cursor:pointer">&#8942; Mac tunnel</button>
 <a href="/docs" style="color:#fff;text-decoration:none;border:1px solid rgba(255,255,255,.5);border-radius:8px;padding:8px 14px;font-weight:600;font-size:14px">Docs &amp; design &rarr;</a></header>
<div class=wrap>
 <div class=card><h2>Who served you (anycast)</h2>
   <div class=kv>
     <b>Data center</b><span><span class=pill id=w_dc>...</span></span>
     <b>Server (node)</b><span id=w_node>...</span>
     <b>Pod</b><span id=w_pod>...</span>
     <b>Pod IP</b><span id=w_pip>...</span>
     <b>Served at</b><span id=w_ts>...</span>
   </div>
   <p class=muted>Refresh / reconnect through the anycast VIP and the nearest / best-path DC answers. <button onclick=who()>refresh</button></p>
 </div>

 <div class=card><h2>How you reached this DC (client &rarr; DC, north-south)</h2>
   <p class=muted>Your request came in <b>north-south, through the firewall</b> &mdash; the mirror image of the firewall-free east-west backbone path shown below. <button onclick=clientpath()>refresh</button></p>
   <div class=kv style="margin-bottom:10px"><b>Your client IP</b><span id=cp_ip>...</span></div>
   <div class=grid2>
     <div><h2 style="font-size:13px">Path taken (transit devices)</h2><div class=path id=cp_path></div></div>
     <div><h2 style="font-size:13px">Live traceroute (this DC &rarr; you)</h2><pre id=cp_trace>-</pre></div>
   </div>
 </div>

 <div class=card><h2>Choose your region</h2>
   <p class=muted>You were routed to <b>__DC__</b>. Pick a region: the one you landed on is served <b>locally</b>; the other is consumed <b>server-side over the backbone</b> (east-west, no firewall).</p>
   <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
     <select id=region>
       <option value="DC1">DC1 region</option>
       <option value="DC2">DC2 region</option>
     </select>
     <button onclick=consume()>Consume dataset</button>
     <span class=muted id=c_meta></span>
   </div>
   <div class=grid2 style="margin-top:12px">
     <div><h2 style="font-size:13px">Dataset returned</h2><pre id=c_out>-</pre></div>
     <div><h2 style="font-size:13px">Path taken (transit devices)</h2><div class=path id=c_path></div>
        <h2 style="font-size:13px;margin-top:12px">Live traceroute</h2><pre id=c_trace>-</pre></div>
   </div>
 </div>
</div>
<script>
async function who(){let r=await fetch('/api/whoami');let j=await r.json();
 w_dc.textContent=j.dc;w_node.textContent=j.node;w_pod.textContent=j.pod;w_pip.textContent=j.podIP;w_ts.textContent=j.ts;}
function renderPathInto(el,hops){el.innerHTML='';hops.forEach(function(h,i){if(i)el.insertAdjacentHTML('beforeend','<span class=arrow>&rarr;</span>');
 var badge='';if(h.role&&h.role.indexOf('NO firewall')>=0)badge=' <span class=nofw>[backbone, no FW]</span>';
 else if(h.role&&h.role.indexOf('WITH firewall')>=0)badge=' <span class=fwbadge>[firewall]</span>';
 el.insertAdjacentHTML('beforeend','<div class=hop><div class=d>'+h.dev+'</div><div class=i>'+h.ip+'</div><div class=muted>'+(h.role||'')+badge+'</div></div>');});}
function renderPath(hops){renderPathInto(c_path,hops);}
async function clientpath(){cp_trace.textContent='...';var r=await fetch('/api/clientpath');var j=await r.json();
 cp_ip.textContent=j.clientIP;renderPathInto(cp_path,j.path);cp_trace.textContent=j.trace;}
async function consume(){let reg=region.value;c_out.textContent='...';c_trace.textContent='...';c_meta.textContent='';
 let r=await fetch('/api/consume?region='+reg);let j=await r.json();
 c_meta.textContent=j.source==='local'?'served locally in '+j.dc:('CROSS-REGION '+j.localDc+' -> '+j.peerDc+(j.peer&&j.peer.ok?'  ('+j.peer.latency_ms+' ms via '+j.peer.via+')':'  FAILED'));
 c_out.textContent=JSON.stringify(j.dataset,null,2);renderPath(j.path||[]);
 let t=await fetch('/api/trace?region='+reg);let tj=await t.json();c_trace.textContent=tj.trace;}
function copyTunnel(){var cmd='ssh -L 8850:10.80.15.50:80 -L 8851:10.80.15.51:80 -L 8852:10.80.15.52:80 eve-office';var b=document.getElementById('tbtn');var done=function(){var t=b.innerHTML;b.innerHTML='✓ copied (8850=anycast · 8851=DC1 · 8852=DC2)';setTimeout(function(){b.innerHTML=t},2600)};
 if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(cmd).then(done,function(){prompt('Copy this:',cmd)})}else{prompt('Copy this:',cmd)}}
region.value="__DC__";who();clientpath();setInterval(who,5000);
</script></body></html>"""

def page():
    return (PAGE.replace("__DC__", DC).replace("__PRIMARY__", T["primary"])
            .replace("__ACCENT__", T["accent"]).replace("__TINT__", T["tint"]))

class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass
    def client_ip(self):
        for h in ("X-Forwarded-For", "X-Real-IP"):
            v = self.headers.get(h)
            if v: return v.split(",")[0].strip()
        return self.client_address[0]
    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query); path = u.path
        if path in ("/", "/index.html"):
            return self._send(200, page(), "text/html; charset=utf-8")
        if path in ("/docs", "/docs/"):
            try:
                with open("/app/docs.html") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception:
                return self._send(404, json.dumps({"error": "docs not mounted"}))
        if path == "/healthz":
            return self._send(200, "ok", "text/plain")
        if path == "/api/whoami":
            return self._send(200, json.dumps(whoami()))
        if path == "/api/dataset":
            return self._send(200, json.dumps(local_dataset()))
        if path == "/api/consume":
            reg = (q.get("region", ["auto"])[0])
            if reg in ("auto", DC):
                return self._send(200, json.dumps({"source": "local", "dc": DC, "dataset": local_dataset(), "path": LOCAL_PATH}))
            peer = fetch_peer("/api/dataset")
            d = peer.get("data") if isinstance(peer.get("data"), dict) else {}
            gb = d.get("generatedBy") if isinstance(d.get("generatedBy"), dict) else {}
            return self._send(200, json.dumps({"source": "cross-region", "localDc": DC, "peerDc": PEER_DC,
                                               "peer": peer, "dataset": peer.get("data", {"error": peer.get("error")}),
                                               "path": xregion_path(peer.get("via", "-"), gb.get("node", "-"),
                                                                    gb.get("pod", "-"), gb.get("podIP", "-"))}))
        if path == "/api/clientpath":
            cip = self.client_ip()
            return self._send(200, json.dumps({"dc": DC, "clientIP": cip,
                                               "path": north_south_path(cip), "trace": trace_from_pod(cip)}))
        if path == "/api/trace":
            reg = (q.get("region", ["auto"])[0])
            if reg in ("auto", DC):
                return self._send(200, json.dumps({"target": POD_IP, "trace": trace_from_pod(POD_IP)}))
            who = fetch_peer("/api/whoami")
            node_ip = who.get("via") if who.get("ok") else (PEER_NODES[0] if PEER_NODES else "127.0.0.1")
            tr = trace_from_pod(node_ip)
            w = who.get("data") if isinstance(who.get("data"), dict) else {}
            if w.get("pod"):
                tr += (f"\n  *  {w.get('podIP','?'):<15}  {w.get('pod')}  "
                       f"[peer POD on {w.get('node','?')} - NodePort local delivery, not a routed x-region hop]")
            return self._send(200, json.dumps({"target": node_ip, "trace": tr}))
        self._send(404, json.dumps({"error": "not found"}))

if __name__ == "__main__":
    print(f"dc-demo up: DC={DC} node={NODE} pod={POD} peers={PEER_NODES}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
