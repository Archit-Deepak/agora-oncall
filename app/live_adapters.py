"""Real adapters (Phase C): Agora phone calls, Linear tickets, Telegram missed-call alerts.

Same interfaces as app/adapters.py, so the orchestrator doesn't change when these
are swapped in. Each one fails loudly (raises) rather than silently, so the
orchestrator's retry / dashboard can show what happened.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import httpx

from app import agora, config
from app.models import CallAttempt, CaseFile

log = logging.getLogger("live")


# ── Voice: Agora v2 telephony → Plivo Zentrunk (US caller ID) → engineer's phone ──
class AgoraPhoneCaller:
    route = "agora_phone"

    def __init__(self, to_number: str | None = None, from_number: str | None = None):
        self.to_number = to_number or config.env("ENGINEER_PHONE")
        self.from_number = from_number or config.env("PLIVO_NUMBER")

    async def place_call(self, attempt: CallAttempt, greeting: str) -> str:
        channel = f"inc-{attempt.attempt_id}"
        res = await agora.place_phone_call(channel, greeting, self.to_number, self.from_number)
        agent_id = res.get("agent_id")
        log.warning("agora /call %s -> %s %s", attempt.attempt_id, res.get("status_code"),
                    agent_id or str(res)[:300])
        if not agent_id:
            raise RuntimeError(f"agora /call failed: {str(res)[:300]}")
        return agent_id

    async def hangup(self, attempt: CallAttempt) -> None:
        if attempt.agora_agent_id:
            res = await agora.stop_agent(attempt.agora_agent_id)
            log.warning("agora leave %s -> %s", attempt.agora_agent_id, res.get("status_code"))

    async def speak(self, attempt: CallAttempt, text: str, priority: str = "APPEND") -> None:
        if attempt.agora_agent_id:
            res = await agora.speak(attempt.agora_agent_id, text, priority)
            log.warning("agora speak -> %s", res.get("status_code"))


# ── Voice: same agent, but the engineer talks in a browser window ────────────
class AgoraBrowserCaller:
    """Agora /join into an RTC channel. The page joins as USER_UID with a mic.

    No SIP and no Plivo. place_call returns once the agent is in the channel;
    the orchestrator still waits for the engineer to speak before marking the
    call answered, same as the phone path.
    """

    route = "agora_rtc"
    IDLE_S = 90

    def __init__(self) -> None:
        self.channel: str | None = None

    def join_info(self) -> dict | None:
        """Short-lived credentials for the browser. The app certificate stays on the server."""
        if not self.channel:
            return None
        return {
            "app_id": config.AGORA_APP_ID,
            "channel": self.channel,
            "uid": config.USER_UID,
            "token": agora.rtc_token(self.channel, config.USER_UID),
        }

    async def place_call(self, attempt: CallAttempt, greeting: str) -> str:
        channel = f"brw-{attempt.attempt_id}"
        res = await agora.start_agent(channel, greeting, idle_timeout=self.IDLE_S)
        agent_id = res.get("agent_id")
        log.warning("agora /join %s -> %s %s", attempt.attempt_id, res.get("status_code"),
                    agent_id or str(res)[:300])
        if not agent_id:
            raise RuntimeError(f"agora /join failed: {str(res)[:300]}")
        self.channel = channel
        return agent_id

    async def hangup(self, attempt: CallAttempt) -> None:
        self.channel = None
        if attempt.agora_agent_id:
            res = await agora.stop_agent(attempt.agora_agent_id)
            log.warning("agora leave %s -> %s", attempt.agora_agent_id, res.get("status_code"))

    async def speak(self, attempt: CallAttempt, text: str, priority: str = "APPEND") -> None:
        if attempt.agora_agent_id:
            res = await agora.speak(attempt.agora_agent_id, text, priority)
            log.warning("agora speak -> %s", res.get("status_code"))


# ── Tickets: Linear GraphQL ──────────────────────────────────────────────────
LINEAR_URL = "https://api.linear.app/graphql"
_PRIORITY = {"Urgent": 1, "High": 2, "Medium": 3, "Low": 4}


class LinearTicketSink:
    def __init__(self, api_key: str | None = None, team_id: str | None = None):
        self.api_key = api_key or config.env("LINEAR_API_KEY")
        self.team_id = team_id or config.env("LINEAR_TEAM_ID")

    async def _gql(self, query: str, variables: dict) -> dict:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(LINEAR_URL, json={"query": query, "variables": variables},
                             headers={"Authorization": self.api_key, "Content-Type": "application/json"})
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or data.get("errors"):
            raise RuntimeError(f"linear HTTP {r.status_code}: {str(data.get('errors') or r.text)[:200]}")
        return data["data"]

    async def _started_state(self) -> str | None:
        """The team's "In Progress" state, so incident tickets land on the Active board (not Backlog)."""
        if getattr(self, "_state_id", None) is None:
            try:
                d = await self._gql("query($id:String!){ team(id:$id){ states { nodes { id type name } } } }",
                                    {"id": self.team_id})
                nodes = d["team"]["states"]["nodes"]
                st = next((n for n in nodes if n["type"] == "started"), None) or \
                    next((n for n in nodes if n["type"] == "unstarted"), None)
                self._state_id = st["id"] if st else ""
            except Exception:  # noqa: BLE001 - fall back to the team default state
                self._state_id = ""
        return self._state_id or None

    async def _label_id(self, name: str, color: str = "#5e6ad2") -> str | None:
        cache = getattr(self, "_labels", None)
        if cache is None:
            d = await self._gql("query($id:String!){ team(id:$id){ labels { nodes { id name } } } }", {"id": self.team_id})
            cache = self._labels = {n["name"].lower(): n["id"] for n in d["team"]["labels"]["nodes"]}
        if name.lower() in cache:
            return cache[name.lower()]
        d = await self._gql("""mutation($input: IssueLabelCreateInput!){ issueLabelCreate(input:$input){ issueLabel { id name } } }""",
                            {"input": {"name": name, "teamId": self.team_id, "color": color}})
        cache[name.lower()] = d["issueLabelCreate"]["issueLabel"]["id"]
        return cache[name.lower()]

    async def loop_in(self, ref: dict, people: list[dict], note: str) -> dict:
        """Loop people into the ticket: role labels, a comment addressed to them, and, for anyone who
        is a real workspace member (matched by email), subscribe them so Linear notifies them."""
        issue_id = ref.get("uuid") or ref["id"]
        d = await self._gql("query($id:String!){ issue(id:$id){ id labels { nodes { id } } subscribers { nodes { id } } } }",
                            {"id": issue_id})
        issue = d["issue"]
        labels = {n["id"] for n in issue["labels"]["nodes"]}
        subs = {n["id"] for n in issue["subscribers"]["nodes"]}
        real = []
        for p in people:
            lid = await self._label_id(p["label"], p.get("color", "#5e6ad2"))
            if lid:
                labels.add(lid)
            if p.get("email"):
                u = await self._gql("query($e:String!){ users(filter:{ email:{ eq:$e } }){ nodes { id displayName } } }",
                                    {"e": p["email"]})
                nodes = u["users"]["nodes"]
                if nodes:
                    subs.add(nodes[0]["id"])
                    real.append(p["name"])
        await self._gql("mutation($id:String!,$input: IssueUpdateInput!){ issueUpdate(id:$id, input:$input){ success } }",
                        {"id": issue["id"], "input": {"labelIds": sorted(labels), "subscriberIds": sorted(subs)}})
        await self._gql("""mutation($input: CommentCreateInput!) { commentCreate(input: $input) { success } }""",
                        {"input": {"issueId": issue["id"], "body": note}})
        return {"subscribed": real}

    async def create(self, payload: dict) -> dict:
        q = """mutation($input: IssueCreateInput!) {
                 issueCreate(input: $input) { success issue { id identifier url } } }"""
        inp = {"teamId": self.team_id, "title": payload["title"],
               "description": payload.get("description", ""),
               "priority": _PRIORITY.get(payload.get("priority", "Urgent"), 1)}
        state = await self._started_state()
        if state:
            inp["stateId"] = state
        issue = (await self._gql(q, {"input": inp}))["issueCreate"]["issue"]
        return {"id": issue["identifier"], "url": issue["url"], "uuid": issue["id"]}

    async def append(self, ref: dict, text: str) -> None:
        q = """mutation($input: CommentCreateInput!) { commentCreate(input: $input) { success } }"""
        await self._gql(q, {"input": {"issueId": ref.get("uuid") or ref["id"], "body": text}})


class FaultInjectingSink:
    """Wraps a real sink and fails the first create() on purpose (FAIL_FIRST_TICKET), so the
    demo shows a spoken error + retry, while the retry genuinely hits the real system."""

    def __init__(self, inner, fail_first: bool = True):
        self.inner = inner
        self.fail_first = fail_first
        self.create_calls = 0

    async def create(self, payload: dict) -> dict:
        self.create_calls += 1
        if self.fail_first and self.create_calls == 1:
            raise RuntimeError("ticket system returned HTTP 503")
        return await self.inner.create(payload)

    async def append(self, ref: dict, text: str) -> None:
        await self.inner.append(ref, text)

    async def loop_in(self, ref: dict, people: list[dict], note: str) -> dict:
        return await self.inner.loop_in(ref, people, note)


# ── Missed call → Telegram, with a "Call me now" button (long polling) ───────
class TelegramNotifier:
    CALLBACK = "call_me_now"

    def __init__(self, token: str | None = None, chat_id: str | None = None):
        self.token = token or config.env("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or config.env("TELEGRAM_CHAT_ID")
        self.api = f"https://api.telegram.org/bot{self.token}"
        self._offset: int | None = None

    async def missed_call(self, case: CaseFile) -> None:
        a = case.alert
        rate = f"{int(round(a.failure_rate * 100))}%" if a else "?"
        lead = next((h for h in case.hypotheses if h.status in ("leading", "confirmed")), None)
        lead_txt = (f"{lead.title} ({int(round(case.top_confidence * 100))}% confident)"
                    if lead else "still investigating")
        text = (f"🚨 {case.incident_id}: {a.customer_name if a else 'Customer'} outbound calls failing\n"
                f"Failure rate: {rate} (baseline {int(round(a.baseline_rate * 100)) if a else '?'}%)\n"
                f"Leading: {lead_txt}\n\nI tried calling twice and couldn't reach you.")
        body = {"chat_id": self.chat_id, "text": text,
                "reply_markup": {"inline_keyboard": [[{"text": "📞 Call me now",
                                                       "callback_data": self.CALLBACK}]]}}
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{self.api}/sendMessage", json=body)
        if r.status_code != 200:
            raise RuntimeError(f"telegram sendMessage {r.status_code}: {r.text[:200]}")

    async def send(self, text: str, buttons: list[tuple[str, str]] | None = None) -> None:
        body = {"chat_id": self.chat_id, "text": text}
        if buttons:
            body["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in buttons]]}
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{self.api}/sendMessage", json=body)
        if r.status_code != 200:
            raise RuntimeError(f"telegram sendMessage {r.status_code}: {r.text[:200]}")

    async def poll_actions(self, handlers: dict) -> None:
        """Long-poll button taps; `handlers` maps callback_data → async fn()."""
        async with httpx.AsyncClient(timeout=40) as c:
            try:
                r = await c.get(f"{self.api}/getUpdates", params={"offset": -1, "timeout": 0})
                res = r.json().get("result", [])
                if res:
                    self._offset = res[-1]["update_id"] + 1
            except Exception:  # noqa: BLE001
                pass
            while True:
                try:
                    params = {"timeout": 30, "allowed_updates": '["callback_query"]'}
                    if self._offset is not None:
                        params["offset"] = self._offset
                    r = await c.get(f"{self.api}/getUpdates", params=params)
                    for upd in r.json().get("result", []):
                        self._offset = upd["update_id"] + 1
                        cq = upd.get("callback_query")
                        fn = handlers.get(cq.get("data")) if cq else None
                        if fn:
                            await c.post(f"{self.api}/answerCallbackQuery",
                                         json={"callback_query_id": cq["id"], "text": "On it"})
                            await fn()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.warning("telegram poll error: %r", e)
                    await asyncio.sleep(3)

    async def mild_outage(self, case: CaseFile, text: str) -> None:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{self.api}/sendMessage", json={"chat_id": self.chat_id, "text": text})
        if r.status_code != 200:
            raise RuntimeError(f"telegram sendMessage {r.status_code}: {r.text[:200]}")

    async def poll_buttons(self, on_call_me: Callable[[], Awaitable[None]]) -> None:
        """Long-poll getUpdates forever; on a 'Call me now' tap, ack it and call back."""
        async with httpx.AsyncClient(timeout=40) as c:
            # Skip anything that arrived before we started (stale taps from earlier runs).
            try:
                r = await c.get(f"{self.api}/getUpdates", params={"offset": -1, "timeout": 0})
                res = r.json().get("result", [])
                if res:
                    self._offset = res[-1]["update_id"] + 1
            except Exception:  # noqa: BLE001
                pass
            while True:
                try:
                    params = {"timeout": 30, "allowed_updates": '["callback_query"]'}
                    if self._offset is not None:
                        params["offset"] = self._offset
                    r = await c.get(f"{self.api}/getUpdates", params=params)
                    for upd in r.json().get("result", []):
                        self._offset = upd["update_id"] + 1
                        cq = upd.get("callback_query")
                        if cq and cq.get("data") == self.CALLBACK:
                            await c.post(f"{self.api}/answerCallbackQuery",
                                         json={"callback_query_id": cq["id"], "text": "Calling you now"})
                            await on_call_me()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 - keep polling through blips
                    log.warning("telegram poll error: %r", e)
                    await asyncio.sleep(3)
