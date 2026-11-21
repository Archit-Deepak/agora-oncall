# Trunk IP ACL: INVITEs dropped before a CDR exists

## Meaning
A Zentrunk termination (outbound) trunk only accepts INVITEs from the source IPs in its IP ACL.
An INVITE from any other IP is rejected at the edge ("source IP not in trunk IP ACL") before a call
is created, so there is no CDR, no hangup cause and no failure: the account's call volume just
drops while the failure rate looks fine.

## Signature
Call volume falls far below the expected rate (volume ratio under 0.3) with no rise in failures.
The edge log shows many INVITE rejections from one new IP, usually right after the customer moved
their SBC, dialer or NAT gateway to a new egress IP. A few rejections from random IPs are normal
background (SIP scanners) and are not the cause.

## Fix
Confirm the new IP belongs to the customer (change log, contact), then add it to the trunk IP ACL
(add_ip_to_acl). Volume returns within seconds. Remove the old IP once the migration is complete.
