#!/bin/sh
# gobgp-2: GoBGP anycast controller (aggr peers 10.201.20.9/10.201.20.13, AS65400).
if ! ip addr show eth1 2>/dev/null | grep -q '10.201.20.10'; then
  ip link set eth1 up; ip link set eth2 up
  ip addr add 10.201.20.10/30 dev eth1; ip addr add 10.201.20.14/30 dev eth2
fi
# reach the DC regional VIPs (.202.1/.202.2) over the FABRIC (ECMP via both aggr), not the eth0 mgmt
# default: gobgpd learns them in BGP but does NOT program the kernel FIB, so the brain's health
# HTTP would otherwise follow the mgmt default and blackhole.
ip route replace 192.168.202.0/24 nexthop via 10.201.20.9 dev eth1 nexthop via 10.201.20.13 dev eth2 2>/dev/null \
  || ip route replace 192.168.202.0/24 via 10.201.20.9 dev eth1
for a in user admin; do id $a >/dev/null 2>&1 || adduser -D -s /bin/sh $a 2>/dev/null; addgroup $a wheel 2>/dev/null; done
echo "user:Test123" | chpasswd 2>/dev/null; echo "root:Test123" | chpasswd 2>/dev/null; echo "admin:admin" | chpasswd 2>/dev/null
pgrep -x dropbear >/dev/null 2>&1 || dropbear -R -p 22 >/dev/null 2>&1
[ -f /bootstrap/authorized_keys ] && for h in /root /home/admin /home/user; do [ -d "$h" ] || continue; mkdir -p "$h/.ssh"; cp /bootstrap/authorized_keys "$h/.ssh/authorized_keys"; chmod 700 "$h/.ssh"; chmod 600 "$h/.ssh/authorized_keys"; u=$(basename "$h"); [ "$u" = root ] || chown -R "$u" "$h/.ssh" 2>/dev/null; done
# install gobgpd + brain from the bind mount, then start (setsid survives the exec; PID1 nginx stays up)
mkdir -p /etc/gobgp /opt/gobgp-brain /run
cp -f /bootstrap/gobgp/gobgpd /bootstrap/gobgp/gobgp /usr/local/bin/ 2>/dev/null; chmod +x /usr/local/bin/gobgpd /usr/local/bin/gobgp 2>/dev/null
cp -f /bootstrap/gobgp/gobgp-2/gobgpd.conf /bootstrap/gobgp/gobgp-2/brain.json /etc/gobgp/ 2>/dev/null
cp -f /bootstrap/gobgp/controller_brain.py /bootstrap/gobgp/set_override.py /opt/gobgp-brain/ 2>/dev/null
if ! pgrep -f 'gobgpd -f' >/dev/null 2>&1; then setsid /usr/local/bin/gobgpd -f /etc/gobgp/gobgpd.conf --api-hosts 127.0.0.1:50051 </dev/null >/tmp/gobgpd.log 2>&1 & fi
sleep 3
if ! pgrep -f controller_brain >/dev/null 2>&1; then setsid /usr/bin/python3 /opt/gobgp-brain/controller_brain.py </dev/null >/tmp/gobgp-brain.log 2>&1 & fi
echo "gobgp-2: eth1 10.201.20.10/30 eth2 10.201.20.14/30; gobgpd+brain up (aggr 10.201.20.9,10.201.20.13)"
