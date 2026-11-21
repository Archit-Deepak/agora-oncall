"""Loop people into the incident by voice: "loop in the carrier team and the account manager".

The roster is fictional people with real roles. Each has an email alias; if a Linear member with
that email exists, they're subscribed on the ticket for real (Linear notifies them), otherwise the
ticket still carries their role label, a comment addressed to them and a "looped in" note.
"""
from __future__ import annotations

import asyncio

from app.models import ActionRecord

ROSTER = [
    {"key": "carrier", "name": "Rahul Menon", "role": "Carrier relations", "label": "carrier-relations",
     "email": "architagarkhed30+carrier@gmail.com", "color": "#e5484d", "owners": {"carrier"},
     "words": ["carrier", "rahul", "noc of", "tata team", "carrier relations", "carrier team"],
     "ask": "please raise it with the carrier's NOC with the evidence below and get a reference number."},
    {"key": "am", "name": "Neha Kapoor", "role": "Account manager", "label": "account-mgmt",
     "email": "architagarkhed30+am@gmail.com", "color": "#f5a524", "owners": {"customer"},
     "words": ["account manager", "neha", "account team", "customer success", "csm", "their am", "the am "],
     "ask": "please give the customer a heads-up with the status and the next update time."},
    {"key": "noc", "name": "NOC on-call", "role": "Network operations", "label": "noc",
     "email": "architagarkhed30+noc@gmail.com", "color": "#0090ff", "owners": {"plivo"},
     "words": ["noc", "network ops", "network operations", "ops team", "on-call", "on call", "sre"],
     "ask": "please keep an eye on the trunk and carrier metrics until this is closed."},
]


def pick(text: str, owner: str | None) -> list[dict]:
    t = f" {(text or '').lower()} "
    if any(w in t for w in ("everyone", "everybody", "all of them", "whole team", "all teams")):
        return list(ROSTER)
    chosen = [p for p in ROSTER if any(w in t for w in p["words"])]
    if chosen:
        return chosen
    # "loop in the right people" → by who owns the problem; always add NOC for visibility
    by_owner = [p for p in ROSTER if owner in p["owners"]]
    return by_owner + [p for p in ROSTER if p["key"] == "noc" and p not in by_owner]


def names(people: list[dict]) -> str:
    parts = [f"{p['name']} from {p['role'].lower()}" if p["key"] != "noc" else "the NOC on-call" for p in people]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


async def loop_in(orch, people: list[dict], said: str = "") -> None:
    """Make sure a ticket exists, then loop the people in on it (Linear) and pop it on the page."""
    if orch.ticket_ref() is None:
        await orch.file_ticket()
    t = None
    for _ in range(30):  # the ticket may still be filing (deliberate first failure + retry)
        t = orch.ticket_ref()
        if t is not None and t.status == "done":
            break
        await asyncio.sleep(0.5)
    cf = orch.store.get()
    fix = next((f for f in reversed(cf.fixes) if f.get("status") in ("verified", "applied", "proposed")), {})
    evidence = fix.get("evidence") or "still being investigated"
    acct = orch.account(cf.account_id)
    note = ("**Looping in:** " + ", ".join(f"{p['name']} ({p['role']})" for p in people) + "\n\n"
            + "\n".join(f"- **{p['name']}**: {p['ask']}" for p in people)
            + f"\n\n**Account:** {acct.name if acct else cf.account_id}\n**Evidence:** {evidence}\n"
            + f"**Fix status:** {fix.get('fix', 'not decided')} ({fix.get('status', '—')})\n"
            + (f"\n_Requested by the on-call engineer on the call: “{said}”_" if said else ""))
    ok, err, real = True, None, []
    if t is not None and t.status == "done" and t.payload.get("ref") and hasattr(orch.ticket, "loop_in"):
        try:
            res = await orch.ticket.loop_in(t.payload["ref"], people, note)
            real = res.get("subscribed") or []
        except Exception as e:  # noqa: BLE001
            ok, err = False, str(e)[:200]
    else:
        ok, err = False, "no ticket to loop people into"
    tid = t.external_id if t is not None else None
    await orch.store.update(lambda c: c.actions.append(ActionRecord(
        kind="loop_in", status="done" if ok else "failed", error=err, external_id=tid,
        payload={"people": [p["name"] for p in people], "roles": [p["role"] for p in people], "notified": real})))
    body = (", ".join(f"{p['name']} · {p['role']}" for p in people)
            + (f"\nNotified in Linear: {', '.join(real)}" if real else "\nAdded to the ticket with role labels and a briefing comment"))
    await orch._notify_page("ticket" if ok else "error",
                            f"Looped in on {tid}" if ok and tid else "Loop-in failed", body if ok else (err or ""),
                            t.external_url if ok and t is not None else None)
