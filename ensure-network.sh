#!/bin/sh
set -eu
name=firmware-build
bridge=br-fwbuild
if ! docker network inspect "$name" >/dev/null 2>&1; then
  docker network create --driver bridge \
    --opt com.docker.network.bridge.name="$bridge" "$name" >/dev/null
fi
iptables -N FWM-EGRESS 2>/dev/null || true
for range in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10 127.0.0.0/8 224.0.0.0/4 240.0.0.0/4; do
  iptables -C FWM-EGRESS -d "$range" -j DROP 2>/dev/null ||
    iptables -A FWM-EGRESS -d "$range" -j DROP
done
iptables -C FWM-EGRESS -j RETURN 2>/dev/null ||
  iptables -A FWM-EGRESS -j RETURN
iptables -C DOCKER-USER -i "$bridge" -j FWM-EGRESS 2>/dev/null ||
  iptables -I DOCKER-USER 1 -i "$bridge" -j FWM-EGRESS
iptables -C INPUT -i "$bridge" -j DROP 2>/dev/null ||
  iptables -I INPUT 1 -i "$bridge" -j DROP
# Builds use IPv4 only; block IPv6 host access and forwarding from this bridge.
ip6tables -C INPUT -i "$bridge" -j DROP 2>/dev/null ||
  ip6tables -I INPUT 1 -i "$bridge" -j DROP
ip6tables -C FORWARD -i "$bridge" -j DROP 2>/dev/null ||
  ip6tables -I FORWARD 1 -i "$bridge" -j DROP
