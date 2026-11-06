"""Obtain a Gmail refresh token for ingestion (one-time, on your own computer).

    python -m scripts.gmail_oauth --client-id ID --client-secret SECRET

Uses Google's installed-app flow with PKCE and a loopback redirect (http://127.0.0.1:<port>/), requesting
only https://www.googleapis.com/auth/gmail.readonly. It opens a browser, you sign in with the mailbox that
receives TLDR AI, and the refresh token is printed once. Put it in the server's secret settings as
GMAIL_REFRESH_TOKEN. It is not written to disk, logged, or sent anywhere except Google.

Prefer setting GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET in the environment over passing them as arguments
(arguments can show up in shell history and process lists).
"""

import argparse
import base64
import hashlib
import http.server
import os
import secrets
import sys
import threading
import webbrowser
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from app.ingestion.providers.gmail import GMAIL_SCOPE, TOKEN_URL

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"


def build_auth_url(client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    return (
        AUTH_URL
        + "?"
        + urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": GMAIL_SCOPE,
                "access_type": "offline",  # ask for a refresh token
                "prompt": "consent",  # always return one, even if consent was given before
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _wait_for_code(port: int, state: str, timeout: float = 300) -> str:
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            query = parse_qs(urlsplit(self.path).query)
            if query.get("state", [""])[0] != state:
                result["error"] = "state mismatch (possible CSRF); start again"
            elif "error" in query:
                result["error"] = query["error"][0]
            else:
                result["code"] = query.get("code", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"AI//CAMPUS: you can close this tab and return to the terminal.")

        def log_message(self, *args):  # keep the code out of the terminal
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = timeout
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout)
    server.server_close()
    if "error" in result:
        raise SystemExit(f"Authorisation failed: {result['error']}")
    if not result.get("code"):
        raise SystemExit("No authorisation code received (timed out).")
    return result["code"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--client-id", default=os.environ.get("GMAIL_CLIENT_ID"))
    parser.add_argument("--client-secret", default=os.environ.get("GMAIL_CLIENT_SECRET"))
    parser.add_argument("--port", type=int, default=8765, help="Local port for the OAuth redirect.")
    parser.add_argument("--no-browser", action="store_true", help="Print the URL instead of opening a browser.")
    args = parser.parse_args(argv)
    if not args.client_id or not args.client_secret:
        print("Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET (or pass --client-id/--client-secret).", file=sys.stderr)
        return 2

    redirect_uri = f"http://127.0.0.1:{args.port}/"
    state = secrets.token_urlsafe(24)
    verifier, challenge = pkce_pair()
    url = build_auth_url(args.client_id, redirect_uri, state, challenge)
    print("Open this URL and sign in with the mailbox that receives the newsletter:\n\n" + url + "\n")
    if not args.no_browser:
        webbrowser.open(url)
    code = _wait_for_code(args.port, state)

    response = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": args.client_id,
            "client_secret": args.client_secret,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
        timeout=20,
    )
    if response.status_code != 200:
        print(f"Token exchange failed (HTTP {response.status_code}).", file=sys.stderr)
        return 1
    payload = response.json()
    granted = set(payload.get("scope", "").split())
    if GMAIL_SCOPE not in granted:
        print(
            "Google did not grant Gmail read access (the 'Read your email' box was probably left unticked). "
            "Remove the app's access at https://myaccount.google.com/permissions and run this again, "
            "ticking that box.",
            file=sys.stderr,
        )
        return 1
    if granted - {GMAIL_SCOPE}:
        print("Warning: Google granted more scopes than requested: " + ", ".join(sorted(granted)), file=sys.stderr)
    refresh = payload.get("refresh_token")
    if not refresh:
        print(
            "Google did not return a refresh token. Remove the app's access at "
            "https://myaccount.google.com/permissions and run this again.",
            file=sys.stderr,
        )
        return 1
    print("Success. Store this as GMAIL_REFRESH_TOKEN in backend/.env or your host's secret settings.")
    print("It grants read-only access to the mailbox: treat it like a password.\n")
    print(refresh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
