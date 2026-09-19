#!/bin/sh
# Keep user browsers (172.31.0.0/16, see subnet() in app.py) off private ranges
# and the GCE metadata server. Replies to connections the api/Caddy opened, and
# traffic inside a user's own network, still pass. Run by anywr-fw.service after docker.
set -e
B=172.31.0.0/16
iptables -N ANYWR 2>/dev/null || iptables -F ANYWR
iptables -A ANYWR -d $B -j RETURN
iptables -A ANYWR -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
for net in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10; do
  iptables -A ANYWR -d $net -j DROP
done
iptables -C DOCKER-USER -s $B -j ANYWR 2>/dev/null || iptables -I DOCKER-USER -s $B -j ANYWR
