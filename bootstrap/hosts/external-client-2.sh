#!/bin/sh
# external-client-2: single data NIC on the 'internet' segment (10.80.15.0/24), lab DNS via the FW-published VIP.
set -e
ip addr show eth1 | grep -q '10.80.15.100' && exit 0
ip link set eth1 up
ip addr add 10.80.15.100/24 dev eth1
while ip route show default dev eth0 2>/dev/null | grep -q .; do ip route del default dev eth0 2>/dev/null || break; done
ip route replace default via 10.80.15.1 dev eth1
printf 'nameserver 10.80.15.41\nsearch ecloud.lab\n' > /etc/resolv.conf
for a in user admin; do id $a >/dev/null 2>&1 || adduser -D -s /bin/sh $a 2>/dev/null; addgroup $a wheel 2>/dev/null; done; echo "user:Test123" | chpasswd 2>/dev/null; echo "root:Test123" | chpasswd 2>/dev/null; echo "admin:admin" | chpasswd 2>/dev/null; pgrep -x dropbear >/dev/null 2>&1 || dropbear -R -p 22 >/dev/null 2>&1
[ -f /bootstrap/authorized_keys ] && for h in /root /home/admin /home/user; do [ -d "$h" ] || continue; mkdir -p "$h/.ssh"; cp /bootstrap/authorized_keys "$h/.ssh/authorized_keys"; chmod 700 "$h/.ssh"; chmod 600 "$h/.ssh/authorized_keys"; u=$(basename "$h"); [ "$u" = root ] || chown -R "$u" "$h/.ssh" 2>/dev/null; done
echo "external-client-2: eth1 10.80.15.100/24 via 10.80.15.1"
