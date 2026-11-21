# 4590 domestic_anchored_terms_not_met (domestic anchoring, India)

## Meaning
Indian regulation requires India-to-India calls (Indian caller ID calling an Indian number) to
have their media anchored in India. If the call's media or voice-agent infrastructure runs
outside India (for example us-east-1), Plivo rejects the call with 4590
domestic_anchored_terms_not_met (SIP 403, hangup source Plivo).

## Signature
Only calls from +91 caller IDs to +91 destinations fail; calls to non-Indian destinations from the
same account keep working. It usually starts right after a deploy that moved media, streaming
servers or AI agent workers to a non-India region; check the account change log.

## Fix
Either move media / agent infrastructure back to an India region (ap-south-1 Mumbai or ap-south-2
Hyderabad: move_media_to_india), or use a non-Indian caller ID (switch_caller_id) for these calls.
Moving media back to India is the long-term fix; the caller-ID switch is a stop-gap.
