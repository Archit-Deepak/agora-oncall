# Runbook: carrier rejecting outbound calls

## Detection
Failure rate on outbound calls climbs well above baseline, concentrated on a single
carrier route, with SIP 503 responses. Other carriers stay near baseline.

## Confirm
Break failures down per carrier. If one carrier's failure rate is far above the others
and the others are healthy, the carrier route is the cause.

## Fix
Reroute affected traffic to a healthy carrier (Y or Z). Open a ticket, notify the
customer that their calls are being rerouted and no action is needed on their side, and
monitor until the failure rate returns to baseline.
