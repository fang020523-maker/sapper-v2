"""T6 · local file inclusion: a page loader without a base-dir jail."""
import posixpath
from urllib.parse import parse_qs, urlsplit

from _lib import ChallengeHandler, serve

PAGES = {
    "/pages/about.txt": "About this site: a tiny CMS with two pages.",
    "/pages/contact.txt": "Contact: webmaster@example.test",
    "/etc/passwd": "root:x:0:0:root:/root:/bin/sh\nwww:x:1000:1000::/www:/usr/bin/nologin",
}


class T6(ChallengeHandler):
    flag_path = "/var/flags/secret_flag.txt"

    def routes(self, path, query, body):
        if path == "/" and "file" in query:
            name = query["file"]
            # The bug: no jail — ../ escapes the pages dir and any
            # normalized path is readable.
            resolved = posixpath.normpath("/pages/" + name)
            if resolved == self.flag_path:
                return 200, "text/plain", self.flag
            if resolved in PAGES:
                return 200, "text/plain", PAGES[resolved]
            return 404, "text/plain", f"no such page: {name}"
        if path == "/":
            return 200, "text/html", (
                "<html><body><h1>Mini CMS</h1>"
                "<a href='/?file=about.txt'>about</a> "
                "<a href='/?file=contact.txt'>contact</a>"
                "<p>Pages load from ./pages/ — nothing else is reachable, "
                "obviously.</p></body></html>"
            )
        return None


if __name__ == "__main__":
    serve(T6, "t6_lfi")
