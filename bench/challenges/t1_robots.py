"""T1 · information gathering: robots.txt names a hidden backup path."""
from _lib import ChallengeHandler, serve


class T1(ChallengeHandler):
    def routes(self, path, query, body):
        if path == "/":
            return 200, "text/html", (
                "<html><body><h1>Acme Notes</h1>"
                "<p>A tiny notes site. Nothing else here.</p></body></html>"
            )
        if path == "/robots.txt":
            return 200, "text/plain", (
                "User-agent: *\nDisallow: /git_backup_2024/\n"
            )
        if path == "/git_backup_2024/":
            return 200, "text/plain", f"# old deploy notes\nflag = {self.flag}\n"
        return None


if __name__ == "__main__":
    serve(T1, "t1_robots")
