#!/usr/bin/env python3
# ecloud anycast controller "brain" (multi-VIP). Runs on each GoBGP node (active / standby).
# Manages a LIST of VIPs. Each VIP's serving DC is chosen from live health + a per-VIP policy
# (static affinity / failover-primary / latency) plus an optional override, and GoBGP re-originates
# each VIP with next-hop = the chosen DC's regional VIP (.202.1 / .202.2). Not in the data path:
# the aggr resolves the regional next-hop recursively into the datacenter.
import json, subprocess, time, os, urllib.request

CONF = "/etc/gobgp/brain.json"
STATUS = "/run/gobgp-brain-status.json"


def load_conf():
    with open(CONF) as f:
        return json.load(f)


def http_health(vip, timeout):
    t0 = time.monotonic()
    ok = False
    try:
        r = urllib.request.urlopen("http://%s/healthz" % vip, timeout=timeout)
        ok = (getattr(r, "status", r.getcode()) == 200)
        r.read(64)
    except Exception:
        ok = False
    return ok, round((time.monotonic() - t0) * 1000.0, 1)


def gobgp(*args):
    try:
        return subprocess.run(["gobgp"] + list(args), capture_output=True, text=True, timeout=8)
    except Exception:
        return None


def set_route(prefix, nexthop, lp):
    gobgp("global", "rib", "del", prefix)
    gobgp("global", "rib", "add", prefix, "nexthop", nexthop, "origin", "igp", "local-pref", str(lp))


def route_has(prefix, nexthop):
    # True only if gobgpd currently holds our route for prefix with the desired next-hop
    # (survives gobgpd restarts: a re-learned Cilium copy carries a different next-hop).
    r = gobgp("global", "rib", prefix)
    return bool(r) and (nexthop in (r.stdout or ""))


def other(d):
    return "DC2" if d == "DC1" else "DC1"


def decide(policy, override, health, cur=None):
    dcs = ("DC1", "DC2")
    # manual override wins, but never steers to a DC that is failing its check
    if override in dcs:
        if health[override]["ok"]:
            return override, "override"
        alt = other(override)
        return (alt, "override->failover") if health[alt]["ok"] else (override, "override(down)")
    mode = policy.get("mode", "failover")
    if mode == "static":
        home = policy.get("dc", "DC1")
        if health[home]["ok"]:
            return home, "static"
        alt = other(home)
        return (alt, "static->failover") if health[alt]["ok"] else (home, "both-down")
    if mode == "latency":
        # Primary-preferring latency: prefer the policy's primary DC and only ride the other
        # DC on a genuinely wide, sustained latency gap. Two thresholds avoid flapping when
        # both DCs are near-equal (as they are here, measured from the central controllers).
        primary = policy.get("primary") or policy.get("dc") or "DC1"
        alt = other(primary)
        healthy = [d for d in dcs if health[d]["ok"]]
        if not healthy:
            return primary, "both-down"
        if primary not in healthy:
            return (alt, "latency->failover") if alt in healthy else (primary, "both-down")
        if alt not in healthy:
            return primary, "latency"
        leave = policy.get("latency_leave_ms", 1.5)    # alt must beat primary by more than this to leave
        ret = policy.get("latency_return_ms", 1.0)     # return to primary once alt's lead drops to this
        # hold band [ret, leave]: keep whichever DC is current, so noise in between never flaps.
        adv = health[primary]["rtt_ms"] - health[alt]["rtt_ms"]   # >0 means alt is faster
        if cur == alt:
            return (alt, "latency(hold-alt)") if adv > ret else (primary, "latency(return)")
        return (alt, "latency(leave)") if adv > leave else (primary, "latency")
    # failover: a primary with the other DC as backup
    primary = policy.get("primary", "DC1")
    if health[primary]["ok"]:
        return primary, "primary"
    alt = other(primary)
    return (alt, "failover") if health[alt]["ok"] else (primary, "both-down")


def write_status(d):
    try:
        os.makedirs(os.path.dirname(STATUS), exist_ok=True)
        tmp = STATUS + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, STATUS)
    except Exception:
        pass


def main():
    last = {}
    cur_target = {}
    srtt = {}
    counts = {}
    while True:
        try:
            conf = load_conf()
            dc_vips = conf["dc_vips"]
            to = conf.get("health_timeout", 1)
            lp = conf.get("base_local_pref", 500)
            gov = conf.get("override", "auto")   # global override for every VIP; "auto" = none
            alpha = conf.get("rtt_alpha", 0.35)
            health = {}
            for dc in ("DC1", "DC2"):
                ok, rtt = http_health(dc_vips[dc], to)
                if ok:
                    counts[dc] = counts.get(dc, 0) + 1
                    prev = srtt.get(dc)
                    # warmup: the first couple of samples take the raw value directly, so a
                    # slow cold-start HTTP sample cannot seed the EWMA high and stick.
                    if prev is None or counts[dc] <= 2:
                        sm = rtt
                    else:
                        sm = round(alpha * rtt + (1 - alpha) * prev, 2)
                    srtt[dc] = sm
                else:
                    sm = srtt.get(dc, rtt)
                # rtt_ms is the smoothed value the latency decision runs on; raw_ms is the last sample
                health[dc] = {"ok": ok, "rtt_ms": sm, "raw_ms": rtt}
            vip_status = []
            for m in conf["managed"]:
                prefix = m["prefix"]
                ov = gov if gov in ("DC1", "DC2") else m.get("override", "auto")
                target, reason = decide(m.get("policy", {}), ov, health, cur_target.get(prefix))
                cur_target[prefix] = target
                nexthop = dc_vips[target]
                key = (nexthop, lp)
                if last.get(prefix) != key or not route_has(prefix, nexthop):
                    set_route(prefix, nexthop, lp)
                    last[prefix] = key
                vip_status.append({"name": m.get("name"), "prefix": prefix,
                                   "target": target, "reason": reason, "nexthop": nexthop})
            write_status({"role": conf.get("role"), "base_local_pref": lp,
                          "override": gov, "health": health, "vips": vip_status,
                          "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
        except Exception as e:
            write_status({"error": str(e), "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
        try:
            time.sleep(load_conf().get("health_interval", 2))
        except Exception:
            time.sleep(2)


if __name__ == "__main__":
    main()
