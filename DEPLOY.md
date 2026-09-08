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
| `PORT` | no | Railway injects this |

The token wants the narrowest scope that still pushes: a fine-grained personal
access token limited to the one repository, with **Contents: read and write**.

## Deploying

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

## Notes

- The clone is `--depth 1`. Checkpoint pushes the current branch, which is what
  a shallow clone can do; it cannot rewrite history, which is fine here.
- The token ends up in the clone's remote URL, inside the container. That is
  acceptable for an ephemeral filesystem and not acceptable on a volume you
  intend to keep — use a credential helper if you mount one.
- Rebuilding the whole document set happens on every save, which is a second or
  two locally and will be slower on a small container.
