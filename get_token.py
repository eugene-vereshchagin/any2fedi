#!/usr/bin/env python3
"""
Get an access token for a bot account on a GoToSocial / Mastodon instance.

Usage:  python3 get_token.py https://your.instance
Then open the printed URL in a browser, log in AS THE BOT ACCOUNT,
approve, paste the code back. The token is printed — put it in config.yaml.
"""
import sys
from urllib.parse import urlencode

import requests

REDIRECT = "urn:ietf:wg:oauth:2.0:oob"
SCOPES = "read write"


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    base = sys.argv[1].rstrip("/")
    app = requests.post(f"{base}/api/v1/apps", data={
        "client_name": "any2fedi", "redirect_uris": REDIRECT, "scopes": SCOPES,
    }, timeout=30)
    app.raise_for_status()
    app = app.json()
    url = f"{base}/oauth/authorize?" + urlencode({
        "client_id": app["client_id"], "redirect_uri": REDIRECT,
        "response_type": "code", "scope": SCOPES,
    })
    print("\n1. Open in a browser, log in as the BOT account, approve:\n\n   " + url + "\n")
    code = input("2. Paste the code here: ").strip()
    tok = requests.post(f"{base}/oauth/token", data={
        "client_id": app["client_id"], "client_secret": app["client_secret"],
        "redirect_uri": REDIRECT, "grant_type": "authorization_code",
        "code": code, "scope": SCOPES,
    }, timeout=30)
    tok.raise_for_status()
    t = tok.json()["access_token"]
    me = requests.get(f"{base}/api/v1/accounts/verify_credentials",
                      headers={"Authorization": f"Bearer {t}"}, timeout=30).json()
    print(f"\nToken for @{me.get('acct')}:\n\n   {t}\n")


if __name__ == "__main__":
    main()
