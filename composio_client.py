#!/usr/bin/env python3
"""The only door to Composio. Anything not on ALLOWED is refused, so this code cannot send or delete email."""
import base64
import datetime
import json
import os

import requests

import kb

URL = "https://connect.composio.dev/mcp"
ALLOWED = {
    "GMAIL_FETCH_EMAILS",
    "GITHUB_LIST_REPOSITORIES_FOR_THE_AUTHENTICATED_USER",
    "GOOGLECALENDAR_EVENTS_LIST",
    "GOOGLECALENDAR_EVENTS_GET",
    "GOOGLECALENDAR_CREATE_EVENT",
    "GOOGLECALENDAR_PATCH_EVENT",
    "GOOGLECALENDAR_DELETE_EVENT",
}
GMAIL_ACCOUNTS = {"acct1": "you@example.com", "acct2": "you2@example.com"}
CALENDAR_ACCOUNT = "calendar"


USAGE_FILE = os.path.join(kb.ROOT, "logs", "composio_usage.json")
SYNC_LIMIT = 6000    # background updates stop here each month
HARD_LIMIT = 10000   # everything stops here, well inside the free plan
COST = {"COMPOSIO_REMOTE_BASH_TOOL": 3}  # counted as a premium tool, to be safe


class NoKey(Exception):
    pass


class BudgetReached(Exception):
    pass


def usage():
    month = datetime.date.today().strftime("%Y-%m")
    try:
        with open(USAGE_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return month, data.get(month, 0), data


def sync_allowed():
    return usage()[1] < SYNC_LIMIT


def _count(name):
    month, used, data = usage()
    if used >= HARD_LIMIT:
        raise BudgetReached(f"Composio monthly budget of {HARD_LIMIT} calls reached")
    data[month] = used + COST.get(name, 1)
    with open(USAGE_FILE, "w") as f:
        json.dump(data, f)


def _rpc(name, arguments):
    kb.load_env()
    key = os.environ.get("COMPOSIO_API_KEY")
    if not key:
        raise NoKey("COMPOSIO_API_KEY is blank in .env")
    _count(name)
    response = requests.post(URL, timeout=120, headers={
        "x-consumer-api-key": key, "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
    }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    response.raise_for_status()
    response.encoding = "utf-8"
    body = response.text
    if "text/event-stream" in response.headers.get("content-type", ""):
        events = [e for e in body.split("\n\n") if "data:" in e]
        body = "\n".join(l[5:].lstrip(" ") for l in events[-1].split("\n") if l.startswith("data:"))
    message = json.loads(body)
    if "error" in message:
        raise RuntimeError(kb.mask(str(message["error"]))[:400])
    text = message["result"]["content"][0]["text"]
    try:
        return json.loads(text)
    except ValueError:
        return text


def run(slug, arguments, account=None):
    """Run one allowed tool and return its data."""
    if slug not in ALLOWED:
        raise PermissionError(f"{slug} is not allowed")
    tool = {"tool_slug": slug, "arguments": arguments}
    if account:
        tool["account"] = account
    out = _rpc("COMPOSIO_MULTI_EXECUTE_TOOL", {"tools": [tool], "sync_response_to_workbench": False})
    if not isinstance(out, dict):
        raise RuntimeError(kb.mask(str(out))[:400])
    results = (out.get("data") or {}).get("results") or []
    if not results:
        raise RuntimeError(kb.mask(json.dumps(out))[:400])
    first = results[0]
    response = first.get("response", first)
    if response.get("successful") is False or first.get("error"):
        raise RuntimeError(kb.mask(str(response.get("error") or first.get("error")))[:400])
    return response.get("data", response)


def long_message_text(arguments, account):
    """Composio stores replies it shortens. Read just the email text back from that stored file."""
    out = _rpc("COMPOSIO_MULTI_EXECUTE_TOOL", {"tools": [
        {"tool_slug": "GMAIL_FETCH_EMAILS", "arguments": arguments, "account": account}], "sync_response_to_workbench": True})
    path = out["data"]["remote_file_info"]["file_path"]
    if not path.replace("/", "").replace(".", "").replace("_", "").replace("-", "").isalnum():
        raise RuntimeError("unexpected file path")
    script = (
        "import json\n"
        "def walk(o):\n"
        "    if isinstance(o, dict):\n"
        "        if o.get('messageText'):\n"
        "            return o['messageText']\n"
        "        o = list(o.values())\n"
        "    if isinstance(o, list):\n"
        "        for v in o:\n"
        "            t = walk(v)\n"
        "            if t:\n"
        "                return t\n"
        f"print(walk(json.load(open('{path}'))) or '')\n")
    encoded = base64.b64encode(script.encode()).decode()
    result = _rpc("COMPOSIO_REMOTE_BASH_TOOL", {"command": f"echo {encoded} | base64 -d | python3"})
    return (result.get("data") or {}).get("stdout", "") if isinstance(result, dict) else ""
