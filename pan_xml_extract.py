#!/usr/bin/env python3
"""Turn an op-mode `show config running` (xml output) tty capture from a PAN into a loadable
<config> XML document (what `load config from` expects), sanitized so it can bootstrap a fresh
vrnetlab PA-VM:
  - drop the mgmt IP/netmask/gw (vrnetlab owns the mgmt interface)
  - drop mgt-config users (launcher sets admin / Admin@123; phash + ssh public key are env secrets)
  - drop the fw-mgmt certificate entry (contains the private key); PAN regenerates a self-signed one
Everything else (interfaces, VR+BGP, HA incl. peer-ip, zones, NAT, security, profiles) is kept verbatim."""
import re, sys, xml.dom.minidom as M
nm = sys.argv[1]
t = open(f"/tmp/{nm}-run.raw", errors="ignore").read()
# greedy: from the FIRST <config ...> to the LAST </config> (a nested <config> lives inside <setting>)
s = t.find("<config "); e = t.rfind("</config>")
assert s >= 0 and e > s, "no <config> block"
xml = t[s:e + len("</config>")]
# sanitize
xml = re.sub(r"\s*<ip-address>172\.29\.129\.\d+</ip-address>", "", xml)
xml = re.sub(r"\s*<default-gateway>172\.29\.129\.254</default-gateway>", "", xml)
xml = re.sub(r"<mgt-config>.*?</mgt-config>", "", xml, flags=re.S)
xml = re.sub(r'<entry name="fw-mgmt">.*?</entry>', "", xml, flags=re.S)
# ...and everything that REFERENCES that cert, or validation fails with
# "MGMT-SSL -> certificate 'fw-mgmt' is not a valid reference": the ssl-tls-service-profile that
# uses it, and the mgmt-interface binding of that profile. PAN falls back to its self-signed cert.
xml = re.sub(r'\s*<entry name="MGMT-SSL">.*?</entry>', "", xml, flags=re.S)
xml = re.sub(r"\s*<ssl-tls-service-profile>MGMT-SSL</ssl-tls-service-profile>", "", xml)
xml = re.sub(r"\s*<ssl-tls-service-profile>\s*</ssl-tls-service-profile>", "", xml, flags=re.S)  # now-empty container
# the mgmt <netmask> only makes sense with the ip; drop it if it is the mgmt one (system-level, not an interface)
xml = re.sub(r"(<system>.*?)\s*<netmask>255\.255\.255\.0</netmask>", r"\1", xml, count=1, flags=re.S)
try:
    M.parseString(xml); ok = "well-formed"
except Exception as e:
    ok = "MALFORMED: " + str(e)[:100]
open(f"/tmp/{nm}-bootstrap.xml", "w").write(xml)
print(nm, len(xml), "bytes", ok,
      "| ifaces:", len(re.findall(r'<entry name="ethernet1/\d"', xml)),
      "nat:", len(re.findall(r"<nat>.*?</nat>", xml, re.S) and re.findall(r'<entry name="[^"]+">', re.search(r"<nat>(.*?)</nat>", xml, re.S).group(1))),
      "cli-dnat:", len(re.findall(r"cli[12]-dnat", xml)),
      "ha:", xml.count("<high-availability>"), "peer-as:", xml.count("<peer-as>"),
      "secrets-left:", len(re.findall(r"phash|private-key|<password>|public-key", xml)))
