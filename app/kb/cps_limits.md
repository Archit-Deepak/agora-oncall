# CPS limits and 5180 cps_limit_reached

## What 5180 means
5180 cps_limit_reached (SIP 503, hangup source Zentrunk) means the account sent more call
attempts in one second than its calls-per-second (CPS) limit. The extra attempts are rejected
immediately; they never reach a carrier.

## SIP trunks reject, Voice API queues
On SIP trunks (Zentrunk) calls above the CPS limit are rejected with 5180. On the Voice API and
audio streaming, calls above the limit are queued and placed a little later instead of failing.
A bursty predictive dialer on a SIP trunk therefore shows waves of 5180 at the start of each burst,
while the average CPS can look well under the limit. Look at per-second peaks, not averages.

## CPS and concurrency in India
In India CPS is sold through concurrency: 25 concurrent channels = 1 CPS. Raising CPS means buying
concurrency (about ₹1,200 per month per extra CPS).

## Options
1. Pace the dialer (customer action, free): cap attempts at the CPS limit and spread them evenly.
2. Buy concurrency (raise_cps): +25 concurrency per +1 CPS, ~₹1,200/month per CPS, to cover the peak.
3. Move bursty campaigns to the Voice API, where over-limit calls queue instead of failing.
