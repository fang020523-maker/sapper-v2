"""T4 · SQL injection: a string-concatenated login bypass.

A real in-memory sqlite table and a really concatenated query — any
payload that would defeat the real thing defeats this one.
"""
import sqlite3

from _lib import ChallengeHandler, serve


class T4(ChallengeHandler):
    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE users (user TEXT, pass TEXT)")
        # The flag is the admin's password column, so any bypass that
        # selects the row reveals it — a faithful jeopardy shape.
        conn.execute("INSERT INTO users VALUES ('admin', ?)", (self.flag,))
        conn.execute("INSERT INTO users VALUES ('bob', 'hunter2')")
        conn.commit()
        return conn

    def routes(self, path, query, body):
        if path == "/":
            return 200, "text/html", (
                "<html><body><h1>Member Login</h1>"
                "<form method='post' action='/login'>"
                "<input name='user' placeholder='user'>"
                "<input name='password' type='password'>"
                "<button>Login</button></form></body></html>"
            )
        if path == "/login":
            user = body.get("user", "")
            password = body.get("password", "")
            # The bug, spelled out: user input goes in unescaped.
            sql = f"SELECT user, pass FROM users WHERE user='{user}' AND pass='{password}'"
            try:
                rows = self._db().execute(sql).fetchall()
            except sqlite3.Error as exc:
                return 200, "text/html", f"<html><body>sql error: {exc}</body></html>"
            if rows:
                return 200, "text/html", (
                    f"<html><body><h2>Welcome, {rows[0][0]}</h2>"
                    f"<p>flag: {rows[0][1]}</p></body></html>"
                )
            return 200, "text/html", "<html><body>login failed</body></html>"
        return None


if __name__ == "__main__":
    serve(T4, "t4_sqli")
