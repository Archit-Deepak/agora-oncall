#!/usr/bin/env bash
# Independent test of the Plivo Zentrunk outbound trunk using LiveKit Cloud SIP (no Agora involved).
# Prereq (once): lk cloud auth
# Usage: scripts/livekit_trunk_test.sh            # create trunk (if missing) and dial ENGINEER_PHONE
#        scripts/livekit_trunk_test.sh cleanup    # delete the test trunk
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a

NAME="plivo-zentrunk-test"

existing_id() {
  lk sip outbound list --json 2>/dev/null | python3 -c "
import json,sys
data=json.load(sys.stdin)
items=data.get('items', data) if isinstance(data, dict) else data
for t in items or []:
    if t.get('name')=='$NAME': print(t.get('sip_trunk_id') or t.get('sipTrunkId')); break"
}

if [[ "${1:-}" == "cleanup" ]]; then
  id=$(existing_id); [[ -n "$id" ]] && lk sip outbound delete "$id" && echo "deleted $id" || echo "nothing to delete"
  exit 0
fi

TRUNK_ID=$(existing_id || true)
if [[ -z "${TRUNK_ID}" ]]; then
  tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT
  python3 - "$tmp" <<'EOF'
import json, os, sys
json.dump({"trunk": {
    "name": "plivo-zentrunk-test",
    "address": os.environ["ZENTRUNK_TERMINATION_HOST"],
    "transport": "SIP_TRANSPORT_UDP",
    "numbers": [os.environ["PLIVO_NUMBER"]],
    "auth_username": os.environ["ZENTRUNK_USERNAME"],
    "auth_password": os.environ["ZENTRUNK_PASSWORD"],
}}, open(sys.argv[1], "w"))
EOF
  lk sip outbound create "$tmp" >/dev/null
  TRUNK_ID=$(existing_id)
  echo "created outbound trunk $TRUNK_ID"
else
  echo "using existing outbound trunk $TRUNK_ID"
fi

echo "dialing $ENGINEER_PHONE from $PLIVO_NUMBER via $ZENTRUNK_TERMINATION_HOST ..."
lk sip participant create \
  --trunk "$TRUNK_ID" \
  --call "$ENGINEER_PHONE" \
  --number "$PLIVO_NUMBER" \
  --room "trunk-test-$(date +%s)" \
  --identity "phone-test" \
  --wait
