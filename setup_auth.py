#!/usr/bin/env python3
"""Run once per account, yourself, in a terminal. You sign in on the provider's own page.

  .venv/bin/python setup_auth.py gmail personal
  .venv/bin/python setup_auth.py outlook
  .venv/bin/python setup_auth.py gcal

Mail is read-only. Calendar can view and change events only. Tokens stay in secrets/.
"""
import os
import sys

import kb

SECRETS = os.path.join(kb.ROOT, "secrets")


def gmail(label):
    from google_auth_oauthlib.flow import InstalledAppFlow
    client = os.path.join(SECRETS, "google_credentials.json")
    if not os.path.exists(client):
        sys.exit("Put the OAuth client file at secrets/google_credentials.json first.")
    flow = InstalledAppFlow.from_client_secrets_file(client, ["https://www.googleapis.com/auth/gmail.readonly"])
    creds = flow.run_local_server(port=0, prompt="select_account consent")
    path = os.path.join(SECRETS, f"gmail_{label}.json")
    with open(path, "w") as f:
        f.write(creds.to_json())
    os.chmod(path, 0o600)
    print(f"Saved read-only Gmail login as '{label}'. Make sure GMAIL_ACCOUNTS in .env lists it.")


def gcal():
    from google_auth_oauthlib.flow import InstalledAppFlow
    client = os.path.join(SECRETS, "google_credentials.json")
    if not os.path.exists(client):
        sys.exit("Put the OAuth client file at secrets/google_credentials.json first.")
    flow = InstalledAppFlow.from_client_secrets_file(client, ["https://www.googleapis.com/auth/calendar.events"])
    creds = flow.run_local_server(port=0, prompt="select_account consent")
    path = os.path.join(SECRETS, "gcal.json")
    with open(path, "w") as f:
        f.write(creds.to_json())
    os.chmod(path, 0o600)
    print("Saved Google Calendar login.")


def outlook():
    import msal
    kb.load_env()
    client_id = os.environ.get("OUTLOOK_CLIENT_ID")
    if not client_id:
        sys.exit("Set OUTLOOK_CLIENT_ID in .env first.")
    cache = msal.SerializableTokenCache()
    tenant = os.environ.get("OUTLOOK_TENANT", "common")
    app = msal.PublicClientApplication(client_id, authority=f"https://login.microsoftonline.com/{tenant}", token_cache=cache)
    flow = app.initiate_device_flow(scopes=["Mail.Read"])
    if "user_code" not in flow:
        sys.exit(f"Could not start sign-in: {flow.get('error_description')}")
    print(flow["message"])
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        sys.exit(f"Sign-in failed: {result.get('error_description')}")
    path = os.path.join(SECRETS, "outlook_token.json")
    with open(path, "w") as f:
        f.write(cache.serialize())
    os.chmod(path, 0o600)
    print("Saved read-only Outlook login.")


if __name__ == "__main__":
    os.makedirs(SECRETS, exist_ok=True)
    if len(sys.argv) == 3 and sys.argv[1] == "gmail":
        gmail(sys.argv[2])
    elif len(sys.argv) == 2 and sys.argv[1] == "gcal":
        gcal()
    elif len(sys.argv) == 2 and sys.argv[1] == "outlook":
        outlook()
    else:
        sys.exit(__doc__)
