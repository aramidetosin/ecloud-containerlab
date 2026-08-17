#!/usr/bin/env python3
# Set an override in /etc/gobgp/brain.json (atomic).
# Usage: set_override.py <global|name|prefix> <auto|DC1|DC2>
import json, sys, os
p = "/etc/gobgp/brain.json"
d = json.load(open(p))
scope, dc = sys.argv[1], sys.argv[2]
n = 0
if scope == "global":
    d["override"] = dc; n = 1
else:
    for m in d.get("managed", []):
        if m.get("name") == scope or m.get("prefix", "").startswith(scope):
            m["override"] = dc; n += 1
if n:
    tmp = p + ".tmp"
    json.dump(d, open(tmp, "w"), indent=2)
    os.replace(tmp, p)
print("set %s override -> %s (%d matched)" % (scope, dc, n))
