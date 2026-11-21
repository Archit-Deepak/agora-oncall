# Hangup causes and SIP codes

## Carrier rejection (SIP 503)
A 503 Service Unavailable from an upstream carrier means the carrier is refusing to
accept the call. When one route shows a spike of 503s while other routes stay healthy,
the carrier is the cause, not the customer's numbers or auth. Mitigation: reroute
traffic off the affected carrier.

## Invalid destination (SIP 484)
A 484 Address Incomplete, or an invalid E.164 destination, points to number formatting
on the customer's side, not a carrier problem. Rule this out by confirming the failed
numbers are valid E.164 and succeeded recently on another route.

## Auth failure (SIP 401 / 407)
A 401 or 407 means the API request or SIP leg failed to authenticate. If every request
is authenticating and there are zero 401/407 responses, auth and config are not the cause.

## Normal clearing (3000)
Normal hangup. Not a failure; excluded from failure-rate math.
