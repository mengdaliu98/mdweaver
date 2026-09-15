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

There is nothing to configure on Railway for it. On the machine that has
the checkouts and the Claude sessions:

```bash
mdweave agent --remote https://<app>.up.railway.app
```

It prints what it is talking to and then says nothing until a button is
pressed. `--once` takes a single job and exits.

**What this actually grants.** A job runs `claude` on that machine with
`--permission-mode bypassPermissions` — it can do anything you can do there.
Queueing one is gated by `MDWEAVE_PASSWORD` and nothing else, so:

> **With a runner connected, the page password is a remote-code-execution
> credential.** Anyone who can read your notes can run commands on the
> devserver. Set it to something long, and do not reuse it.

`--permission-mode acceptEdits` on the runner narrows that if you would rather
the sessions could write prose and not run commands.

The one thing holding this up is that every request body must declare
`Content-Type: application/json`. Browsers attach Basic credentials to
cross-site requests, so without that check a page you merely visited could
queue a job. A form cannot send that content type and a script that tries
forces a preflight this server does not answer. Do not remove it to be
helpful; there is no second credential behind it any more.

The runner's endpoints — claim, events, done, reconcile — take no credential.
The accepted cost is that a stranger who found the URL could claim your jobs
and read the instruction text. Do not put anything confidential in a prompt.

**Nothing about this is required.** Start no runner and the button never
appears, because the page asks whether one is connected before it offers it.

## Operating the runner

The runner is the one piece that does not live on Railway, so it is the one
piece that has to be brought back by hand — unless something does it for you.
`deploy/mdweave-agent.service` is a systemd *user* unit. Link it rather than
copy it, so the repository stays the one copy and a change reaches every
machine on the next `git pull`:

```bash
mkdir -p ~/.config/systemd/user ~/.local/var
ln -sfn "$PWD/deploy/mdweave-agent.service" ~/.config/systemd/user/mdweave-agent.service
systemctl --user daemon-reload
systemctl --user enable --now mdweave-agent
loginctl enable-linger "$USER"      # so it survives logout and starts at boot
```

`loginctl enable-linger` is the load-bearing line for "survives a restart".
Without it a user unit does not start until you log in, which on a box you
reach over SSH after a drain is not the same thing as starting at boot.

The unit names no machine: `%h` is systemd's specifier for the user's home, so
it finds `mdweave` wherever the README's setup put it. Only the link is
per-machine, and making it is the install.

After that a drain, a reboot or a crash brings it back on its own; `Restart=always`
with a ten second pause covers the case where the container is mid-deploy and
briefly refusing. Editing the unit means editing it in the repo and then
`systemctl --user daemon-reload`.

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

## Rotating the password

```bash
railway variables --set MDWEAVE_PASSWORD="$(openssl rand -hex 16)"   # or the dashboard
```

One password, so one thing to rotate. Railway redeploys and browsers are
prompted again on their next request; the runner never had a copy of it and
does not care. Worth doing if you ever typed it somewhere you do not control,
because with a runner connected it is the credential that runs commands.

## Notes

- The clone is `--depth 1`. Checkpoint pushes the current branch, which is what
  a shallow clone can do; it cannot rewrite history, which is fine here.
- The token ends up in the clone's remote URL, inside the container. That is
  acceptable for an ephemeral filesystem and not acceptable on a volume you
  intend to keep — use a credential helper if you mount one.
- Rebuilding the whole document set happens on every save, which is a second or
  two locally and will be slower on a small container.
