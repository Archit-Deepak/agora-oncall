"""Read-only: list Linear teams (id, key, name) and optionally write LINEAR_TEAM_ID to .env.

Usage:
    python3 scripts/linear_teams.py            # list teams
    python3 scripts/linear_teams.py VPS        # list, and save the team with key VPS to .env
"""
import json
import pathlib
import sys
import urllib.request

ENV = pathlib.Path(__file__).resolve().parent.parent / ".env"


def read_env():
    out = {}
    for line in ENV.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def main():
    key = read_env().get("LINEAR_API_KEY", "")
    if not key:
        sys.exit("LINEAR_API_KEY is empty in .env")
    req = urllib.request.Request(
        "https://api.linear.app/graphql",
        data=json.dumps({"query": "{ teams { nodes { id key name } } }"}).encode(),
        headers={"Content-Type": "application/json", "Authorization": key},
    )
    try:
        data = json.load(urllib.request.urlopen(req, timeout=15))
    except urllib.error.HTTPError as e:
        sys.exit(f"Linear returned HTTP {e.code}: {e.read().decode()[:200]}")
    teams = data["data"]["teams"]["nodes"]
    for t in teams:
        print(f"{t['key']:6} {t['name']:30} {t['id']}")
    if len(sys.argv) > 1:
        match = [t for t in teams if t["key"].lower() == sys.argv[1].lower()]
        if not match:
            sys.exit(f"No team with key {sys.argv[1]}")
        lines = ENV.read_text().splitlines()
        lines = [f"LINEAR_TEAM_ID={match[0]['id']}" if l.startswith("LINEAR_TEAM_ID=") else l for l in lines]
        ENV.write_text("\n".join(lines) + "\n")
        print(f"Saved LINEAR_TEAM_ID for {match[0]['name']} to .env")


if __name__ == "__main__":
    main()
