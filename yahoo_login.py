"""
One-time Yahoo login. Run this ONCE on your own computer:

    python yahoo_login.py

It opens a browser to Yahoo, you approve the app, paste the code back here,
and it prints the refresh token to store as the YAHOO_REFRESH_TOKEN secret.
Needs YAHOO_CLIENT_ID and YAHOO_CLIENT_SECRET in your .env (or environment).
"""

import json
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from yahoo_oauth import OAuth2

client_id = os.environ.get("YAHOO_CLIENT_ID", "").strip()
client_secret = os.environ.get("YAHOO_CLIENT_SECRET", "").strip()
if not client_id or not client_secret:
    raise SystemExit("Set YAHOO_CLIENT_ID and YAHOO_CLIENT_SECRET in .env first.")

TOKEN_FILE = "oauth2.json"

# Start with only the app credentials -> the library sees no token and
# launches the interactive browser flow.
with open(TOKEN_FILE, "w") as f:
    json.dump({"consumer_key": client_id, "consumer_secret": client_secret}, f)

oauth = OAuth2(None, None, from_file=TOKEN_FILE)  # <- browser opens here

with open(TOKEN_FILE) as f:
    saved = json.load(f)

print("\nSUCCESS. Add this as the GitHub secret YAHOO_REFRESH_TOKEN:\n")
print(saved["refresh_token"])
print("\n(oauth2.json is in .gitignore — do not commit it.)")
