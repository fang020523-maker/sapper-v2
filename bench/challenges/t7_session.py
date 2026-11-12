"""T7 · session wall: the flag is behind a login-gated profile.

Login sets a session cookie; /profile only answers it. Without a cookie
jar an agent logs in successfully and still sees nothing — this proves
the client carries the session.
"""
import secrets

from _lib import ChallengeHandler, serve

SESSIONS: dict[str, str] = {}


class T7(ChallengeHandler):
    def routes(self, path, query, body):
        if path == "/":
            return 200, "text/html", (
                "<html><body><h1>Member Area</h1>"
                "<form method='post' action='/login'>"
                "<input name='user'><input name='password' type='password'>"
                "<button>Login</button></form>"
                "<p>Members get their profile at /profile.</p></body></html>"
            )
        if path == "/login":
            if body.get("user") == "member" and body.get("password") == "member":
                token = secrets.token_hex(8)
                SESSIONS[token] = "member"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Set-Cookie", f"session={token}; Path=/")
                self.send_header("Location", "/profile")
                body_out = b"<html><body>logging in...</body></html>"
                self.send_header("Content-Length", str(len(body_out)))
                self.end_headers()
                self.wfile.write(body_out)
                return None  # handled
            return 200, "text/html", "<html><body>login failed</body></html>"
        if path == "/profile":
            cookie = self.headers.get("Cookie", "")
            token = cookie.split("session=")[-1].split(";")[0] if "session=" in cookie else ""
            if SESSIONS.get(token) == "member":
                return 200, "text/html", (
                    f"<html><body><h2>member</h2><p>flag: {self.flag}</p></body></html>"
                )
            return 200, "text/html", "<html><body>login required (hint: member/member)</body></html>"
        return None

    def _handle(self, body):
        # Let /login short-circuit above work through the same pipeline.
        parts = __import__("urllib.parse", fromlist=["urlsplit"]).urlsplit(self.path)
        if parts.path in ("/login", "/profile"):
            result = self.routes(parts.path, {}, body)
            if result is None:
                return
            status, ctype, payload = result
            blob = payload.encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)
            return
        super()._handle(body)


if __name__ == "__main__":
    serve(T7, "t7_session")
