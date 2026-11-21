# Account: Zylo Lending (acct_zylo)

## Overview
Collections predictive dialer over a SIP trunk (Zentrunk), about 2 calls per second in waves
around agent availability. Spend about ₹22,000 per day. CPS limit 4 (100 concurrency). HA enabled.

## Known behaviour
New campaigns started at full pace have produced 5180 cps_limit_reached bursts before; the fix was
lowering the dialer pacing ratio. The dialer SBC egress IPs are in the trunk IP ACL.

## Contacts
Arjun Mehta (Dialer Ops Lead), Kavya Iyer (CTO).
