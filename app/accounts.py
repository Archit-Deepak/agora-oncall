"""Fleet accounts — the fictional India customers the on-call agent watches (V2_SPEC "Accounts").

`default_accounts(anchor)` returns fresh, mutable profiles every time (fixes mutate them).
Times in change_log / past_incidents are relative to `anchor` so the demo always looks
"recent". All names, numbers, IPs and people are fictional.
"""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

INDIA_MEDIA_REGIONS = {"ap-south-1", "ap-south-2"}  # Mumbai, Hyderabad


@dataclass
class AccountProfile:
    id: str
    name: str
    use_case: str
    spend_per_day_inr: int
    plan: str
    cps_limit: int
    concurrency: int
    ha_enabled: bool
    caller_ids: list[str]
    media_region: str
    ip_acl: list[str]
    trunk_id: str
    direction: str  # "outbound" | "inbound"
    calls_per_s: float  # healthy mean arrival rate
    contacts: list[dict] = field(default_factory=list)
    change_log: list[dict] = field(default_factory=list)  # {at: datetime, change: str, by: str}
    past_incidents: list[dict] = field(default_factory=list)  # {date, summary, fix}
    carriers: list[tuple[str, float]] = field(default_factory=list)  # outbound (or ingress) carrier, weight
    destinations: list[tuple[str, float]] = field(default_factory=list)  # destination network, weight
    route_overrides: dict[str, str] = field(default_factory=dict)  # to_carrier -> forced carrier
    ha_backup_number: str | None = None  # set when HA is enabled
    caller_id_override: str | None = None  # when set, every outbound call uses this caller ID
    bursty: bool = False
    pbx_host: str | None = None  # inbound accounts: where Zentrunk delivers calls
    sbc_ip: str | None = None  # customer's SBC egress IP (outbound trunks)
    notes: str = ""

    def to_dict(self, now: datetime | None = None, change_window_h: float | None = None) -> dict:
        d = asdict(self)
        cl = self.change_log
        if now is not None and change_window_h is not None:
            lo = now - timedelta(hours=change_window_h)
            cl = [c for c in cl if lo <= c["at"] <= now]
        d["change_log"] = [{**c, "at": c["at"].isoformat(timespec="seconds")} for c in cl]
        d["carriers"] = [{"carrier": c, "weight": w} for c, w in self.carriers]
        d["destinations"] = [{"network": n, "weight": w} for n, w in self.destinations]
        return d


def spend_at_risk_per_hour(profile: AccountProfile, bad_share: float) -> float:
    """₹/hour at risk = spend/day / 86400 × failure-or-silent share × 3600."""
    return round(profile.spend_per_day_inr / 86400 * max(0.0, min(1.0, bad_share)) * 3600, 0)


def _accounts(anchor: datetime) -> list[AccountProfile]:
    d = lambda **kw: anchor - timedelta(**kw)  # noqa: E731
    return [
        AccountProfile(
            id="acct_northwind", name="Northwind Health",
            use_case="Appointment-reminder calls to patients, outbound via Zentrunk",
            spend_per_day_inr=48_000, plan="Enterprise (Zentrunk India, committed spend)",
            cps_limit=10, concurrency=250, ha_enabled=False,
            caller_ids=["+918040011201", "+918040011202", "+918040011203"],
            media_region="ap-south-1", ip_acl=["34.93.14.16/28", "35.200.40.5/32"],
            trunk_id="21893461102953", direction="outbound", calls_per_s=3.0,
            sbc_ip="34.93.14.20",
            contacts=[{"name": "Priya Raman", "role": "VP Engineering", "email": "priya.raman@northwind-health.example"},
                      {"name": "Northwind NOC", "role": "24x7 NOC", "email": "noc@northwind-health.example"}],
            change_log=[
                {"at": d(days=6, hours=3), "change": "Added caller ID +918040011203 to the number pool", "by": "customer"},
                {"at": d(hours=9, minutes=12), "change": "Rotated API auth token (scheduled quarterly rotation)", "by": "customer"},
                {"at": d(hours=2, minutes=40), "change": "Reminder campaign schedule moved from 10:00 to 09:30 IST", "by": "customer"},
            ],
            past_incidents=[
                {"date": (anchor - timedelta(days=41)).date().isoformat(),
                 "summary": "Short spike of 4340 temporary_unavailable on Airtel during a carrier maintenance window (12 min).",
                 "fix": "Self-recovered; no action."},
                {"date": (anchor - timedelta(days=88)).date().isoformat(),
                 "summary": "Reminder calls failing with 401 after an expired API token.",
                 "fix": "Customer rotated the token; added rotation reminder."},
            ],
            carriers=[("TATA", 0.42), ("Airtel", 0.34), ("Jio", 0.24)],
            destinations=[("Jio", 0.33), ("Airtel", 0.27), ("Vi-Delhi", 0.28), ("BSNL", 0.12)],
            notes="Largest account. HA numbers were offered in the last QBR but not enabled.",
        ),
        AccountProfile(
            id="acct_zylo", name="Zylo Lending",
            use_case="Collections predictive dialer, outbound over a SIP trunk",
            spend_per_day_inr=22_000, plan="Growth (SIP trunk India, pay-as-you-go)",
            cps_limit=4, concurrency=100, ha_enabled=True,
            caller_ids=["+912261800410", "+912261800411", "+912261800412", "+912261800413"],
            media_region="ap-south-1", ip_acl=["34.93.88.10/32", "34.93.88.11/32"],
            trunk_id="30551974428117", direction="outbound", calls_per_s=2.0, bursty=True,
            sbc_ip="34.93.88.10", ha_backup_number="+912261800499",
            contacts=[{"name": "Arjun Mehta", "role": "Dialer Ops Lead", "email": "arjun.mehta@zylolending.example"},
                      {"name": "Kavya Iyer", "role": "CTO", "email": "kavya@zylolending.example"}],
            change_log=[
                {"at": d(days=3, hours=1), "change": "Dialer campaign 'Q4-recovery-bucket-2' created", "by": "customer"},
                {"at": d(hours=5, minutes=5), "change": "Dialer agents roster increased from 60 to 80 seats", "by": "customer"},
            ],
            past_incidents=[
                {"date": (anchor - timedelta(days=19)).date().isoformat(),
                 "summary": "Burst of 5180 cps_limit_reached when a new campaign started at full pace.",
                 "fix": "Customer reduced dialer pacing ratio from 3.0 to 1.8."},
            ],
            carriers=[("TATA", 0.30), ("Airtel", 0.40), ("Jio", 0.30)],
            destinations=[("Jio", 0.40), ("Airtel", 0.28), ("Vi-Delhi", 0.20), ("BSNL", 0.12)],
            notes="Predictive dialer; traffic comes in waves around agent availability.",
        ),
        AccountProfile(
            id="acct_medley", name="Medley Voice AI",
            use_case="AI voice agents (collections + support) over audio streaming / Zentrunk, Krisp NC on",
            spend_per_day_inr=35_000, plan="Enterprise (Voice API + audio streaming, Krisp)",
            cps_limit=8, concurrency=200, ha_enabled=True,
            caller_ids=["+912240088120", "+912240088121", "+14155550173"],
            media_region="ap-south-1", ip_acl=["34.100.12.32/29"],
            trunk_id="47120938456621", direction="outbound", calls_per_s=2.0,
            sbc_ip="34.100.12.34", ha_backup_number="+912240088199",
            contacts=[{"name": "Rohan Kapoor", "role": "Head of Platform", "email": "rohan@medleyvoice.example"},
                      {"name": "Sana Qureshi", "role": "Solutions Engineer", "email": "sana@medleyvoice.example"}],
            change_log=[
                {"at": d(days=2, hours=4), "change": "Agent prompt v38 rolled out (collections persona)", "by": "customer"},
                {"at": d(hours=7, minutes=30), "change": "Krisp noise-cancellation level changed from 'standard' to 'high'", "by": "customer"},
            ],
            past_incidents=[
                {"date": (anchor - timedelta(days=27)).date().isoformat(),
                 "summary": "Bot heard nothing on ~5% of calls; traced to the customer's websocket server dropping stream frames.",
                 "fix": "Customer scaled the streaming workers."},
            ],
            carriers=[("TATA", 0.45), ("Airtel", 0.30), ("Jio", 0.25)],
            destinations=[("Vi-Delhi", 0.40), ("Jio", 0.22), ("Airtel", 0.15), ("BSNL", 0.11),
                          ("US-Mobile", 0.08), ("UK-Mobile", 0.04)],
            notes="Delhi-NCR-heavy book; international calls use the +1 caller ID.",
        ),
        AccountProfile(
            id="acct_brightline", name="Brightline BPO",
            use_case="Inbound support lines delivered to the customer's PBX over an inbound Zentrunk trunk",
            spend_per_day_inr=9_000, plan="Standard (inbound trunk India)",
            cps_limit=5, concurrency=60, ha_enabled=False,
            caller_ids=["+911140065500", "+911140065501", "+918068190020"],
            media_region="ap-south-1", ip_acl=[],
            trunk_id="58004417700230", direction="inbound", calls_per_s=1.2,
            pbx_host="sip:pbx1.brightline-bpo.example:5060 (203.0.113.24)",
            contacts=[{"name": "Meera Nair", "role": "IT Manager", "email": "meera.nair@brightline-bpo.example"}],
            change_log=[
                {"at": d(days=12), "change": "Added DID +918068190020 for the Bengaluru queue", "by": "customer"},
                {"at": d(hours=20), "change": "Inbound trunk primary URI confirmed as pbx1.brightline-bpo.example;transport=udp", "by": "customer"},
            ],
            past_incidents=[
                {"date": (anchor - timedelta(days=55)).date().isoformat(),
                 "summary": "All inbound calls failed 4170 for 25 min after the customer's PBX firewall update.",
                 "fix": "Customer re-allowed Plivo's signalling IPs on their firewall."},
            ],
            carriers=[("Airtel", 0.38), ("Jio", 0.34), ("TATA", 0.16), ("Vi", 0.12)],
            destinations=[("customer_pbx", 1.0)],
            notes="Low spend; Telegram notification tier.",
        ),
    ]


def default_accounts(anchor: datetime | None = None) -> dict[str, AccountProfile]:
    anchor = anchor or datetime.now()
    return {a.id: a for a in _accounts(anchor)}


def clone(profile: AccountProfile) -> AccountProfile:
    return copy.deepcopy(profile)
