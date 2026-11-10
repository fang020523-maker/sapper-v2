"""T4 · SQL injection: a string-concatenated login bypass."""
from _lib import ChallengeHandler, serve

# The "database": one admin row. The bug is the login query concatenates
# user input; ' OR 1=1 -- makes it match the admin without a password.
ADMIN = {"user": "admin", "password": "n0t-th3-fl4g"}


def login_query_result(user: str, password: str) -> bool:
    # Models a query: SELECT * FROM users WHERE user='{u}' AND pass='{p}'
    # Faithful enough: the classic payload comments out the password check.
    payload_user = user
    return payload_user == "admin' OR 1=1 --" or (
        payload_user == ADMIN["user"] and password == ADMIN["password"]
    )


class T4(ChallengeHandler):
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
            if login_query_result(body.get("user", ""), body.get("password", "")):
                return 200, "text/html", (
                    f"<html><body><h2>Welcome, admin</h2>"
                    f"<p>flag: {self.flag}</p></body></html>"
                )
            return 200, "text/html", "<html><body>login failed</body></html>"
        return None


if __name__ == "__main__":
    serve(T4, "t4_sqli")
