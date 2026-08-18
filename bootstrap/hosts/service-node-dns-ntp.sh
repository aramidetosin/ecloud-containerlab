#!/bin/sh
# service-node-dns-ntp: first-boot bond + identity + services.
if ! ip link show bond0 >/dev/null 2>&1; then
  ip link add bond0 type bond mode 802.3ad lacp_rate fast miimon 100 xmit_hash_policy layer3+4
  ip link set bond0 address 50:00:00:11:00:01
  ip link set eth1 down; ip link set eth2 down
  ip link set eth1 master bond0; ip link set eth2 master bond0
  ip link set eth1 mtu 9000; ip link set eth2 mtu 9000; ip link set bond0 mtu 9000
  ip link set eth1 up; ip link set eth2 up; ip link set bond0 up
  ip addr add 10.167.30.10/24 dev bond0
  # drop docker's metric-0 eth0 (mgmt) default so the FABRIC (bond0) is the sole default; else
  # cross-subnet replies leave via mgmt and die. mgmt /24 stays (directly connected) for host SSH.
  while ip route show default dev eth0 2>/dev/null | grep -q .; do ip route del default dev eth0 2>/dev/null || break; done
  ip route replace default via 10.167.30.1 dev bond0
  printf 'nameserver 127.0.0.1\nsearch ecloud.lab\n' > /etc/resolv.conf
fi
for a in user admin; do id $a >/dev/null 2>&1 || adduser -D -s /bin/sh $a 2>/dev/null; addgroup $a wheel 2>/dev/null; done; echo "user:Test123" | chpasswd 2>/dev/null; echo "root:Test123" | chpasswd 2>/dev/null; echo "admin:admin" | chpasswd 2>/dev/null; pgrep -x dropbear >/dev/null 2>&1 || dropbear -R -p 22 >/dev/null 2>&1
[ -f /bootstrap/authorized_keys ] && for h in /root /home/admin /home/user; do [ -d "$h" ] || continue; mkdir -p "$h/.ssh"; cp /bootstrap/authorized_keys "$h/.ssh/authorized_keys"; chmod 700 "$h/.ssh"; chmod 600 "$h/.ssh/authorized_keys"; u=$(basename "$h"); [ "$u" = root ] || chown -R "$u" "$h/.ssh" 2>/dev/null; done
# --- lab DNS (dnsmasq) + NTP (OpenNTPD), started from the bind-mounted config ---
cp -f /bootstrap/svc/dnsmasq.conf /etc/dnsmasq.conf 2>/dev/null
cp -f /bootstrap/svc/ecloud.hosts /etc/ecloud.hosts 2>/dev/null
printf 'listen on *\n' > /etc/ntpd.conf
pgrep -x dnsmasq >/dev/null 2>&1 || /usr/sbin/dnsmasq -C /etc/dnsmasq.conf 2>/dev/null
pgrep -x ntpd    >/dev/null 2>&1 || setsid /usr/sbin/ntpd -f /etc/ntpd.conf </dev/null >/tmp/ntpd.log 2>&1 &
echo "service-node-dns-ntp: bond0 10.167.30.10/24 via 10.167.30.1 dns 127.0.0.1"
