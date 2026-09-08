#!/bin/sh
# Boot mdweave in a container: fetch the documents, then serve them.
#
# Required environment:
#   MDWEAVE_PASSWORD      the password every request must present
#   KNOWLEDGE_BASE_REPO   host/path of the content repo, no scheme
#                         e.g. github.com/mengdaliu98/knowledge_base.git
#   GITHUB_TOKEN          a token that can read, and push to, that repo
#
# Optional:
#   MDWEAVE_USER          username for the password prompt (default: mdweave)
#   MDWEAVE_CONTENTS      where to keep the clone (default: /data/knowledge_base)
#   GIT_AUTHOR_NAME       what checkpoint commits are attributed to
#   GIT_AUTHOR_EMAIL
#   PORT                  injected by Railway

set -eu

fail() {
  echo "mdweave-start: $1" >&2
  exit 1
}

[ -n "${MDWEAVE_PASSWORD:-}" ] || fail "MDWEAVE_PASSWORD is not set.
  This container is reachable from the internet and every write endpoint is
  open without one, including the git push behind the Checkpoint button."
[ -n "${KNOWLEDGE_BASE_REPO:-}" ] || fail "KNOWLEDGE_BASE_REPO is not set"
[ -n "${GITHUB_TOKEN:-}" ] || fail "GITHUB_TOKEN is not set"

CONTENTS="${MDWEAVE_CONTENTS:-/data/knowledge_base}"
REMOTE="https://x-access-token:${GITHUB_TOKEN}@${KNOWLEDGE_BASE_REPO}"

if [ -d "$CONTENTS/.git" ]; then
  # A volume is mounted and already has the clone. Take whatever has landed on
  # the remote since, but never clobber an edit that has not been checkpointed.
  echo "mdweave-start: reusing the checkout at $CONTENTS"
  git -C "$CONTENTS" remote set-url origin "$REMOTE"

  # Generated pages are not work. They are rewritten a few seconds from now by
  # the build below, and leaving them modified is enough on its own to make
  # `pull --ff-only` refuse -- which strands the container on an old commit
  # while the prose it should be showing sits on the remote. Discarding them
  # cannot lose anything; a modified file under markdown_inputs still can, and
  # still blocks the pull, which is the behaviour worth keeping.
  git -C "$CONTENTS" checkout -- html_outputs 2>/dev/null || true
  git -C "$CONTENTS" clean -qfd html_outputs 2>/dev/null || true

  git -C "$CONTENTS" pull --ff-only || \
    echo "mdweave-start: pull skipped (local changes under markdown_inputs)" >&2
else
  echo "mdweave-start: cloning into $CONTENTS"
  mkdir -p "$(dirname "$CONTENTS")"
  git clone --depth 1 "$REMOTE" "$CONTENTS" \
    || fail "clone failed -- check KNOWLEDGE_BASE_REPO and GITHUB_TOKEN"
fi

# Checkpoint runs `git commit`, which refuses to guess an identity.
git -C "$CONTENTS" config user.name "${GIT_AUTHOR_NAME:-mdweave}"
git -C "$CONTENTS" config user.email "${GIT_AUTHOR_EMAIL:-mdweave@localhost}"

# The clone is shallow, so a push needs the branch to be tracked properly.
git -C "$CONTENTS" config push.default current

mkdir -p "$CONTENTS/markdown_inputs" "$CONTENTS/html_outputs"

# serve, not start: a container wants one process in the foreground, not a
# daemon that forks away and lets the entrypoint exit.
exec mdweave serve \
  --host 0.0.0.0 \
  --port "${PORT:-8765}" \
  -i "$CONTENTS/markdown_inputs" \
  -o "$CONTENTS/html_outputs"
