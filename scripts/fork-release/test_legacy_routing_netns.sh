#!/usr/bin/env bash
# Must run under sudo unshare --net; never touches the host namespace.
set -euo pipefail
binary=$(realpath "$1")
[[ $(readlink /proc/self/ns/net) != $(readlink /proc/1/ns/net) ]]
ip link add downstream0 type dummy
ip link add upstream0 type dummy
ip link set downstream0 up
ip link set upstream0 up
ip addr add 192.0.2.1/24 dev downstream0
ip route add table 1042 default dev upstream0
ip rule add priority 20700 iif downstream0 lookup 1042
ip rule add priority 20900 iif downstream0 unreachable
ip rule add priority 17800 iif downstream0 lookup 1031
ip rule add priority 17800 iif downstream0 lookup 1031
ip rule add priority 17900 iif downstream0 lookup 1016
ip rule add priority 17980 iif downstream0 unreachable
# Sentinels must survive even when they reuse legacy priorities.
ip rule add priority 17800 iif upstream0 lookup 1099
ip rule add priority 17900 iif downstream0 fwmark 0x1234 lookup 1098
ip rule add priority 17900 from 203.0.113.0/24 iif downstream0 lookup 1097
if ip route get 1.1.1.1 from 192.0.2.2 iif downstream0; then
  echo 'FAIL: old guard did not reproduce unreachable' >&2
  exit 1
fi
"$binary"
ip route get 1.1.1.1 from 192.0.2.2 iif downstream0 | grep -q 'dev upstream0'
rules=$(ip rule show)
! grep -q '17980:' <<< "$rules"
! grep -Eq 'lookup (1031|1016)' <<< "$rules"
grep -q 'lookup 1099' <<< "$rules"
grep -q 'lookup 1098' <<< "$rules"
grep -q 'lookup 1097' <<< "$rules"
grep -q '20700:.*lookup 1042' <<< "$rules"
grep -q '20900:.*unreachable' <<< "$rules"
"$binary"
[[ $(ip rule show) == "$rules" ]]
echo 'PASS legacy routing: real netlink deletion, duplicates, unrelated-rule preservation, route recovery and idempotence'
