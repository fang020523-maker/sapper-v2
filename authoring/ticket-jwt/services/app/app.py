"""TidyTicket — a small internal ticket board.

Staff sign in with their team password and get a token; the ticket API
answers requests that carry a valid token for that team.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from flask import Flask, jsonify, request

app = Flask(__name__)

ANSWER_PATH = "/flag"
SECRET = os.environ.get("TICKET_SIGNING_KEY", "tidy-default-signing-key")

TEAMS = {
    # The demo account is printed on the sign-in page, as on many an
    # internal tool: enough to look around, not enough to see ops.
    "demo": "demo",
    "operations": os.environ.get("OPERATIONS_PASSWORD", "op-4fd2d1"),
}


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(part: str) -> bytes:
    pad = "=" * (-len(part) % 4)
    return base64.urlsafe_b64decode(part + pad)


def issue_token(team: str) -> str:
    header = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64url(
        json.dumps({"team": team, "iat": int(time.time())}).encode()
    )
    signature = hmac.new(SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{b64url(signature)}"


def check_token(token: str) -> str | None:
    """Return the team the token names, or None.

    Tokens arrive as header.payload.signature. Some service meshes send
    unsigned tokens (payload only, no signature segment); those are
    accepted for the mesh's own health traffic — trust inside the
    perimeter.
    """
    parts = token.split(".")
    if len(parts) == 2:
        header_raw, payload_raw = parts
        signature = b""
    elif len(parts) == 3:
        header_raw, payload_raw, sig_raw = parts
        signature = b64url_decode(sig_raw)
    else:
        return None
    try:
        header = json.loads(b64url_decode(header_raw))
        payload = json.loads(b64url_decode(payload_raw))
    except (ValueError, json.JSONDecodeError):
        return None
    if header.get("alg") not in ("HS256", "none"):
        return None
    if header.get("alg") == "HS256":
        expected = hmac.new(
            SECRET.encode(), f"{header_raw}.{payload_raw}".encode(), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, signature):
            return None
    team = payload.get("team")
    return team if team in TEAMS else None


def ticket_row(ticket_id: int) -> dict | None:
    if ticket_id == 1:
        return {"id": 1, "team": "demo", "title": "Welcome", "body": "Board orientation notes."}
    if ticket_id == 2:
        return {
            "id": 2,
            "team": "demo",
            "title": "Printer names",
            "body": "Rename PRINTER-02 to something findable.",
        }
    if ticket_id == 3:
        with open(ANSWER_PATH) as fh:
            return {
                "id": 3,
                "team": "operations",
                "title": "Handover note",
                "body": fh.read().strip(),
            }
    return None


@app.get("/healthz")
def healthz():
    return "ok", 200, {"Content-Type": "text/plain"}


@app.get("/")
def index():
    return (
        "<h1>TidyTicket</h1>"
        "<p>Internal ticket board. Sign-in is team-based.</p>"
        "<p>Demo access: team <code>demo</code>, password <code>demo</code>.</p>"
        '<form method="post" action="/login">'
        '<input name="team" placeholder="team"> '
        '<input name="password" type="password" placeholder="password"> '
        "<button>Sign in</button></form>"
    )


@app.post("/login")
def login():
    team = request.form.get("team", "")
    password = request.form.get("password", "")
    if TEAMS.get(team) != password:
        return jsonify({"error": "bad team or password"}), 401
    return jsonify({"token": issue_token(team)})


@app.get("/api/tickets")
def tickets():
    team = check_token(request.headers.get("Authorization", "").removeprefix("Bearer ").strip())
    if team is None:
        return jsonify({"error": "token required"}), 401
    visible = [1, 2] if team == "demo" else [3]
    return jsonify([ticket_row(i) for i in visible])


@app.get("/api/tickets/<int:ticket_id>")
def ticket(ticket_id: int):
    team = check_token(request.headers.get("Authorization", "").removeprefix("Bearer ").strip())
    if team is None:
        return jsonify({"error": "token required"}), 401
    row = ticket_row(ticket_id)
    if row is None:
        return jsonify({"error": "no such ticket"}), 404
    if row["team"] != team:
        return jsonify({"error": "not your team's ticket"}), 403
    return jsonify(row)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
