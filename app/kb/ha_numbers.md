# HA numbers (India): carrier failover for outbound caller IDs

## Why there is no automatic carrier failover in India
In India a phone number is tied to the carrier that issued it (telecom regulation). An outbound
call presenting a TATA-issued caller ID can only be carried by TATA, so when that carrier rejects
calls (5350 service_unavailable_by_carrier, SIP 503, hangup source Carrier) Plivo cannot simply
route the same call over Airtel or Jio. Outside India carrier failover is automatic and needs no
setup.

## What HA numbers do
High Availability (HA) rents a backup number from a different carrier for the account. On a
carrier fault the call is retried once via the fallback: the retry presents the backup number, so
the CDR from_number shows the backup caller ID and the carrier route shows the backup carrier.
Works for Voice API and Zentrunk domestic India outbound calls.

## Cost and when to propose it
The backup number is a separate rental at the standard India local number rate (one extra
number per primary). Propose enable_ha when failures concentrate on one outbound carrier (one
carrier near 100% 5350/4340 while the others are near baseline) and the account has HA disabled.
If HA is already enabled, carrier rejections are absorbed by the backup number automatically.

## How to confirm recovery
After enabling, the failure rate on the account drops back to baseline within the next minute
and new answered calls show the backup number as from_number.
