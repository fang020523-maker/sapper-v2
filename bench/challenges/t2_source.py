"""T2 · source disclosure: an HTML comment leaks a debug switch."""
from _lib import ChallengeHandler, serve


class T2(ChallengeHandler):
    def routes(self, path, query, body):
        if path == "/" and query.get("debug") == "1":
            return 200, "text/plain", f"# gate source\nSECRET = '{self.flag}'\n"
        if path == "/":
            return 200, "text/html", (
                "<html><body><h1>Gate</h1>"
                "<form method='post' action='/check'><input name='code'>"
                "<button>go</button></form>"
                "<!-- dev note: remove ?debug=1 before prod -->"
                "</body></html>"
            )
        if path == "/check":
            return 200, "text/html", (
                f"<html><body>wrong code: {body.get('code', '')}</body></html>"
            )
        return None


if __name__ == "__main__":
    serve(T2, "t2_source")
