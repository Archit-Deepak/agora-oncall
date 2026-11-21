# Account: Northwind Health (acct_northwind)

## Overview
Appointment-reminder calls to patients across India, outbound via Zentrunk, about 3 calls per
second. Spend about ₹48,000 per day; the largest account. CPS limit 10, 250 concurrency. Carriers
TATA, Airtel and Jio; destinations mostly Jio, Airtel, Vi-Delhi and BSNL.

## Resilience
HA numbers are disabled. They were offered in the last QBR (backup number on Airtel) but not
enabled, so a TATA rejection cannot fail over automatically.

## Contacts
Priya Raman (VP Engineering), Northwind NOC (24x7). Voice incident: phone the on-call engineer.
