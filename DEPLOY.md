# Deploying to Railway

The image is built from this repo and holds the tool only. The documents are a
separate, private repository, cloned at boot. Git is the storage: the
**Checkpoint** button pushes a document back, and that push is what makes an
edit outlive the container.

## Before anything else

This server was written to sit on loopback on your own machine, with no
authentication, and the code said so. Two things now stand between it and an
open door on the internet:

- `MDWEAVE_PASSWORD` gates every route. There is one exception, `/api/ping`,
  which answers `{"ok": true}` and nothing else so a platform health check can
  reach it.
- Without that password, `mdweave serve` **refuses to bind** to anything but
  loopback, before it renders a thing. `MDWEAVE_ALLOW_INSECURE=1` overrides it,
  and you should not need to.

Basic auth is only as private as the transport. Railway terminates TLS on the
generated domain, so that is fine there; do not put this on plain HTTP.

## Environment

| variable | required | what it is |
| --- | --- | --- |
| `MDWEAVE_PASSWORD` | yes | the password the browser will ask for |
| `KNOWLEDGE_BASE_REPO` | yes | content repo, no scheme: `github.com/<you>/knowledge_base.git` |
| `GITHUB_TOKEN` | yes | a token that can **read and push** that repo |
| `MDWEAVE_AUTOCOMMIT` | no | seconds of quiet before committing and pushing by itself; unset = off |
| `MDWEAVE_USER` | no | username for the prompt (default `mdweave`) |
| `MDWEAVE_CONTENTS` | no | where the clone lives (default `/data/knowledge_base`) |
| `GIT_AUTHOR_NAME` | no | what checkpoint commits are attributed to |
| `GIT_AUTHOR_EMAIL` | no | " |
| `MDWEAVE_OPERATOR_KEY` | no | the page asks for it before queueing an agent job |
| `MDWEAVE_JOBS_STORE` | no | where the queue is mirrored (default `/data/agent-jobs.json`) |
| `PORT` | no | Railway injects this |

The token wants the narrowest scope that still pushes: a fine-grained personal
access token limited to the one repository, with **Contents: read and write**.

## Deploying

The `railway` command below is the Railway CLI, which is a separate install and
is not on a machine just because the site is deployed from it:

```bash
npm install -g @railway/cli && railway login && railway link
```

Everything it does can also be done in the dashboard, which is worth knowing
when you are on a box you would rather not install things on.

```bash
railway init                       # or link an existing project
railway variables --set MDWEAVE_PASSWORD=... \
                  --set KNOWLEDGE_BASE_REPO=github.com/<you>/knowledge_base.git \
                  --set GITHUB_TOKEN=...
railway up
```

`railway.json` selects the Dockerfile builder and points the health check at
`/api/ping`. `deploy/start.sh` is the entrypoint: it clones (or pulls), sets a
commit identity, and then `exec`s `mdweave serve` in the foreground — `serve`,
not `start`, because a container wants one process that stays put rather than a
daemon that forks away.

## What survives a restart

Nothing on the container's disk, unless you mount a volume at `/data`.

- **Checkpointed** — safe. It is a commit on the remote.
- **Edited but not checkpointed** — gone on the next deploy or restart.
- **Comments** — the same. A `.ann.json` sidecar is a file like any other, and
  needs a checkpoint to leave the container.

Mount a Railway volume at `/data` if that is too sharp an edge. `start.sh`
already handles the case: it reuses an existing checkout and pulls
`--ff-only`, so a local edit is never clobbered by the remote.

## Checking it

```bash
curl https://<app>.up.railway.app/api/ping                  # {"ok": true}
curl -o /dev/null -w '%{http_code}\n' https://<app>.up.railway.app/   # 401
curl -u mdweave:<password> https://<app>.up.railway.app/api/health
```

## The agent bridge

The container holds a queue: the page can put a job in, and a `mdweave agent`
process elsewhere can take it out. Nothing happens until you start a runner --
until then the queue is inert and the button never appears. See the README for
what it does and why it polls.

```bash
# The CLI is one way; the dashboard and the API are others. `openssl rand`
# draws from the OS's cryptographic random source -- do not invent this.
OPERATOR=$(openssl rand -hex 16); echo "type this into the page: $OPERATOR"
railway variables --set MDWEAVE_OPERATOR_KEY="$OPERATOR"
```

Then, on the machine that has the checkouts and the Claude sessions:

```bash
mdweave agent --remote https://<app>.up.railway.app
```

It prints what it is talking to and then says nothing until a button is
pressed. `--once` takes a single job and exits.

**What this actually grants.** A job runs `claude` on that machine with
`--permission-mode bypassPermissions` — it can do anything you can do there.
So `MDWEAVE_OPERATOR_KEY` is deliberately not the password that reads the
notes: it is what a browser must present to queue anything, asked for once and
kept in `sessionStorage` so it does not outlive the tab.

The runner's own endpoints — claim, events, done, reconcile — take no
credential. Collecting a job and reporting on it lead nowhere; the endpoint
that starts something is the one behind the key. The accepted cost is that a
stranger who found the URL could claim your jobs and read the instruction
text. Set `MDWEAVE_PASSWORD` to something strong and do not put anything
confidential in a prompt.

Neither is checked on the job's progress stream. `EventSource` cannot send a
request header, and the alternatives are a secret in a URL or a cookie session
this server does not have — so watching a job you already queued is treated as
reading, which the page's own password already gates. What the key protects is
the POST that starts something.

Set `--permission-mode acceptEdits` on the runner if that trade is not one you
want; the sessions can still write prose and can no longer run commands.

**Nothing about this is required.** Start no runner and the button never
appears, because the page asks whether one is connected before it offers it.

## Operating the runner

The runner is the one piece that does not live on Railway, so it is the one
piece that has to be brought back by hand — unless something does it for you.
`deploy/mdweave-agent.service` is a systemd *user* unit that does:

```bash
mkdir -p ~/.config/systemd/user ~/.local/var
cp deploy/mdweave-agent.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now mdweave-agent
loginctl enable-linger "$USER"      # so it survives logout and starts at boot
```

After that a drain, a reboot or a crash brings it back on its own; `Restart=always`
with a ten second pause covers the case where the container is mid-deploy and
briefly refusing.

```bash
systemctl --user status mdweave-agent      # is it up
systemctl --user restart mdweave-agent     # after pulling new code
journalctl --user -u mdweave-agent -f      # or: tail -f ~/.local/var/mdweave-agent.log
```

**Exactly one runner per knowledge base.** Two race for every job and whichever
claims first wins, silently — the other never sees it. Hand-started daemons
make this easy to get wrong, because stopping a terminal does not stop a
`setsid` process; the unit is the fix, since systemd will not run two copies.
If you have been starting it by hand, check before enabling the unit:

```bash
pgrep -af 'mdweave agent'
```

## Rotating the operator key

```bash
NEW=$(openssl rand -hex 16); echo "type this into the page: $NEW"
railway variables --set MDWEAVE_OPERATOR_KEY="$NEW"     # or the dashboard
```

Railway redeploys and the old key stops working at once. Nothing else has to
change: the runner never had a copy, and a browser holding the old one is
refused, prompts you for a new one, retries, and remembers it. There is no
cache to clear and no session to end.

Worth doing if you ever typed it on a machine you do not control, since that
key is what stands between a hostile webpage and a Claude session on your
devserver.

## Notes

- The clone is `--depth 1`. Checkpoint pushes the current branch, which is what
  a shallow clone can do; it cannot rewrite history, which is fine here.
- The token ends up in the clone's remote URL, inside the container. That is
  acceptable for an ephemeral filesystem and not acceptable on a volume you
  intend to keep — use a credential helper if you mount one.
- Rebuilding the whole document set happens on every save, which is a second or
  two locally and will be slower on a small container.
