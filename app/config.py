import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


AGORA_APP_ID = env("AGORA_APP_ID")
AGORA_APP_CERTIFICATE = env("AGORA_APP_CERTIFICATE")
AGORA_CUSTOMER_ID = env("AGORA_CUSTOMER_ID")
AGORA_CUSTOMER_SECRET = env("AGORA_CUSTOMER_SECRET")

MURF_API_KEY = env("MURF_API_KEY")
MURF_VOICE_ID = env("MURF_VOICE_ID", "en-US-daniel")
MURF_MODEL = env("MURF_MODEL", "falcon-2.1")

GROQ_API_KEY = env("GROQ_API_KEY")
GROQ_MODEL = env("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_FALLBACK_MODEL = env("GROQ_FALLBACK_MODEL", "openai/gpt-oss-20b")

# Azure OpenAI (student credits): investigator primary + voice backup. Empty → Groq only.
AZURE_OPENAI_ENDPOINT = env("AZURE_OPENAI_ENDPOINT").rstrip("/")
AZURE_OPENAI_KEY = env("AZURE_OPENAI_KEY")
AZURE_OPENAI_DEPLOYMENT = env("AZURE_OPENAI_DEPLOYMENT")            # e.g. gpt-5-mini (reasoning)
AZURE_OPENAI_DEPLOYMENT_MINI = env("AZURE_OPENAI_DEPLOYMENT_MINI")  # e.g. gpt-4.1-mini (fast)

ENGINEER_NAME = env("ENGINEER_NAME", "there")
PUBLIC_BASE_URL = env("PUBLIC_BASE_URL").rstrip("/")
PROXY_SHARED_SECRET = env("PROXY_SHARED_SECRET")

AGENT_UID = 1001
USER_UID = 1002
