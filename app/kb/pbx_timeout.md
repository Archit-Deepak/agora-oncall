# 4170 request_timeout_customer (inbound to customer PBX)

## Meaning
4170 request_timeout_customer (SIP 408, hangup source Zentrunk) on inbound calls means Zentrunk
sent the INVITE to the customer's PBX / SIP endpoint and got no response before the timer
expired (32 s Timer B, no 100 Trying). The caller hears ringing and then the call drops.

## Owner
The customer. The PBX is unreachable from Plivo: PBX down, firewall or security group blocking
Plivo's signalling IPs, wrong public IP/port on the trunk URI, or a NAT change. Plivo cannot fix
this from its side; rerouting carriers, HA numbers or CPS changes do not help.

## What to check
Check reachability of the PBX from Plivo, SIP OPTIONS keepalive replies (edge log shows OPTIONS
timeouts when the PBX is unreachable), firewall allow-lists for Plivo's signalling IPs, and recent
changes on the customer network.

## Transport field is not evidence
The transport shown on 4170 rows (udp) is just what the INVITE was sent over; a timeout looks the
same on any transport. Do not conclude a TLS/UDP mismatch from the transport field alone.

## Action
Notify the customer (notify_customer) with the evidence: count of 4170s, PBX IP:port, OPTIONS
timeouts. Recovery happens when the customer restores reachability.
