import uuid

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import agora, config, proxy

app = FastAPI(title="Incident Agent")
app.include_router(proxy.router)
app.mount("/static", StaticFiles(directory=config.ROOT / "web"), name="static")

active: dict[str, str] = {}  # channel -> agent_id


@app.get("/")
def index():
    return FileResponse(config.ROOT / "web" / "index.html")


@app.get("/state")
def state():
    return {"ok": True, "active_agents": active}


@app.post("/api/session")
def new_session():
    channel = f"inc-{uuid.uuid4().hex[:8]}"
    return {"app_id": config.AGORA_APP_ID, "channel": channel, "uid": config.USER_UID,
            "token": agora.rtc_token(channel, config.USER_UID)}


class StartReq(BaseModel):
    channel: str


@app.post("/api/agent/start")
async def start(req: StartReq):
    greeting = (f"Hi {config.ENGINEER_NAME}, this is your incident agent. "
                "If you can hear me, the browser voice path is working. Ask me anything.")
    res = await agora.start_agent(req.channel, greeting)
    if res.get("agent_id"):
        active[req.channel] = res["agent_id"]
    return res


@app.post("/api/agent/stop")
async def stop(req: StartReq):
    agent_id = active.pop(req.channel, None)
    if not agent_id:
        return {"stopped": False}
    return await agora.stop_agent(agent_id)


@app.get("/api/turns")
def turns():
    return list(proxy.turns)
