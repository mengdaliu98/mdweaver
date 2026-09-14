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
| `MDWEAVE_RUNNER_TOKEN` | no | the runner's credential; unset = no bridge at all |
| `MDWEAVE_OPERATOR_KEY` | no | yours; the page asks for it before queueing a job |
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

Off unless `MDWEAVE_RUNNER_TOKEN` is set. With it set, the container grows a
queue: the page can put a job in, and a `mdweave agent` process elsewhere can
take it out. See the README for what it does and why it polls.

```bash
# The CLI is one way; the dashboard and the API are others. `openssl rand`
# draws from the OS's cryptographic random source -- do not invent these.
OPERATOR=$(openssl rand -hex 16); echo "type this into the page: $OPERATOR"
railway variables --set MDWEAVE_RUNNER_TOKEN="$(openssl rand -hex 32)" \
                  --set MDWEAVE_OPERATOR_KEY="$OPERATOR"
```

Then, on the machine that has the checkouts and the Claude sessions:

```bash
mdweave agent --remote https://<app>.up.railway.app --token <the same token>
```

It prints what it is talking to and then says nothing until a button is
pressed. `--once` takes a single job and exits.

**What this actually grants.** A job runs `claude` on that machine with
`--permission-mode bypassPermissions` — it can do anything you can do there.
So the two credentials above are deliberately not the password that reads the
notes, and they are deliberately not each other:

- `MDWEAVE_RUNNER_TOKEN` is held only by the runner. It cannot queue work; it
  can only collect it and report back.
- `MDWEAVE_OPERATOR_KEY` is what a browser must present to queue anything.
  It is asked for once and kept in `sessionStorage`, so it does not outlive
  the tab.

Neither is checked on the job's progress stream. `EventSource` cannot send a
request header, and the alternatives are a secret in a URL or a cookie session
this server does not have — so watching a job you already queued is treated as
reading, which the page's own password already gates. What the key protects is
the POST that starts something.

Set `--permission-mode acceptEdits` on the runner if that trade is not one you
want; the sessions can still write prose and can no longer run commands.

**Nothing about this is required.** Leave `MDWEAVE_RUNNER_TOKEN` unset and the
button never appears, the endpoints answer 503, and the deployment is exactly
what it was.

## Notes

- The clone is `--depth 1`. Checkpoint pushes the current branch, which is what
  a shallow clone can do; it cannot rewrite history, which is fine here.
- The token ends up in the clone's remote URL, inside the container. That is
  acceptable for an ephemeral filesystem and not acceptable on a volume you
  intend to keep — use a credential helper if you mount one.
- Rebuilding the whole document set happens on every save, which is a second or
  two locally and will be slower on a small container.
