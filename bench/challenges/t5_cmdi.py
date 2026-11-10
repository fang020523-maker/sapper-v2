"""T5 · command injection: a ping utility that shells out."""
from _lib import ChallengeHandler, serve

# The "filesystem": /flag.txt holds the injected flag.


class T5(ChallengeHandler):
    def routes(self, path, query, body):
        if path == "/":
            return 200, "text/html", (
                "<html><body><h1>Network Tools</h1>"
                "<form method='get' action='/ping'>"
                "<input name='host' placeholder='host to ping'>"
                "<button>Ping</button></form></body></html>"
            )
        if path == "/ping":
            host = query.get("host", "")
            # The bug: shell=True with the raw parameter.
            return 200, "text/plain", f"$ ping -c 1 {host}\n{self._shell(f'ping -c 1 {host}')}\n"
        return None

    def _shell(self, command: str) -> str:
        """Faithful-enough shell: ; and && chain, cat reads /flag.txt."""
        out = []
        for part in command.replace("&&", ";").split(";"):
            part = part.strip()
            if part.startswith("ping"):
                target = part.split()[-1] if len(part.split()) > 1 else "?"
                out.append(f"PING {target} 56(84) bytes of data.\n--- 1 packet transmitted ---")
            elif part.startswith("cat"):
                target = part.split()[-1] if len(part.split()) > 1 else ""
                out.append(self.flag if target == "/flag.txt"
                           else f"cat: {target}: No such file")
            elif part.startswith("ls"):
                out.append("index.html ping.cgi flag.txt")
            elif part:
                out.append(f"sh: {part.split()[0]}: command not found")
        return "\n".join(out)


if __name__ == "__main__":
    serve(T5, "t5_cmdi")
