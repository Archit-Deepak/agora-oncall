# Account: Brightline BPO (acct_brightline)

## Overview
Inbound support lines: callers dial Brightline DIDs and Zentrunk delivers the calls to the
customer's PBX (pbx1.brightline-bpo.example, 203.0.113.24:5060) over an inbound trunk. About 1
call per second, spend about ₹9,000 per day (low spend: Telegram notification tier).

## Known behaviour
A PBX firewall update once blocked Plivo's signalling IPs and every inbound call failed with 4170
request_timeout_customer for 25 minutes; the customer fixed their firewall.

## Contacts
Meera Nair (IT Manager).
