# Silent calls (answered but no audio)

## Signal
Krisp reports the maximum noise energy of the callee leg for every answered call. A max noise
energy of 5 or less on an answered call means the callee leg carried no audio: a silent call.
Normal calls show energy around 40-90. Silent calls are answered (3000/3010 normal_hangup), so
they never count as failures; dashboards that only watch the failure rate miss them. Typical
pattern: short durations (8-20 s) because the callee hears nothing and hangs up.

## Baseline
About 1% of answered calls are silent for ordinary reasons (voicemail cut, muted handset). Above
~15% of answered calls is an incident.

## Localise
Compare silent rate per carrier x destination network inside the same account. A one-way or no
audio problem on one carrier's interconnect towards one destination network shows up as one
route near 100% silent while every other route stays near 1%.

## Fix
Reroute that destination network via another carrier (reroute_destination) and raise a ticket
with the affected carrier. Recovery: new answered calls on the destination show normal energy.
