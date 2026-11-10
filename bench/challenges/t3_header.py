"""T3 · client control: the gate only serves a very particular browser."""
from urllib.parse import urlsplit

from _lib import ChallengeHandler, serve


class T3(ChallengeHandler):
    def routes(self, path, query, body):
        if path == "/":
            ua = self.headers.get("User-Agent", "")
            if "FlagBrowser 3000" in ua:
                return 200, "text/html", (
                    f"<html><body>flag: {self.flag}</body></html>"
                )
            return 200, "text/html", (
                "<html><body><h1>Legacy Portal</h1>"
                "<p>This portal only supports the FlagBrowser 3000.</p>"
                "<p>Please upgrade your browser.</p></body></html>"
            )
        return None


if __name__ == "__main__":
    serve(T3, "t3_header")
