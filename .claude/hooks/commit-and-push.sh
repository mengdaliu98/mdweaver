#!/usr/bin/env bash
# Commit everything and push, at the end of every Claude Code request.
#
# Wired up as a Stop hook in .claude/settings.json. It never blocks: whatever
# happens, Claude's turn is over, and a failure is reported as a message in
# the session rather than as an error that stops anything.
#
# The commit message is the request that produced the change -- its first
# line as the subject, the whole of it in the body -- read from the session's
# transcript. That is the one record of *why* that exists at this point.

set -uo pipefail

input=$(cat)

say() { jq -n --arg m "auto-commit: $1" '{systemMessage: $m}'; }

repo=$(git -C "${CLAUDE_PROJECT_DIR:-.}" rev-parse --show-toplevel 2>/dev/null) || exit 0
cd "$repo" || exit 0

# Nothing written this turn -- a question answered, a file read -- is nothing
# to commit, and the common case.
[ -n "$(git status --porcelain)" ] || exit 0

branch=$(git symbolic-ref --short -q HEAD) || { say "skipped, HEAD is detached"; exit 0; }

# Mid-merge or mid-rebase the index belongs to that operation; committing into
# it would conclude something a person started and has not finished.
gitdir=$(git rev-parse --git-dir)
if [ -e "$gitdir/MERGE_HEAD" ] || [ -d "$gitdir/rebase-merge" ] || [ -d "$gitdir/rebase-apply" ]; then
  say "skipped, a merge or rebase is in progress"
  exit 0
fi

# The last thing the user typed. Tool results are also "user" entries, but
# their content is a list, not a string; injected reminders are marked isMeta;
# slash commands arrive wrapped in tags.
request=""
transcript=$(jq -r '.transcript_path // empty' <<<"$input" 2>/dev/null)
if [ -n "$transcript" ] && [ -f "$transcript" ]; then
  request=$(jq -nRr '
    [inputs | fromjson?
     | select(.type == "user" and (.isMeta | not)
              and (.message.content | type) == "string")
     | .message.content
     | select(startswith("<") | not)]
    | last // ""' "$transcript" 2>/dev/null)
fi

subject=$(printf '%s' "$request" | sed -n '/[^[:space:]]/{p;q;}' | cut -c1-72)
[ -n "$subject" ] || subject="Changes from a Claude Code request"
body="Committed automatically at the end of a Claude Code request."
[ -n "$request" ] && body="$body

Request:
$(printf '%s' "$request" | cut -c1-2000)"

git add -A || { say "git add failed"; exit 0; }
if ! out=$(printf '%s\n\n%s\n' "$subject" "$body" | git commit -q -F - 2>&1); then
  say "commit failed: $out"
  exit 0
fi
revision=$(git rev-parse --short HEAD)

# Push to the branch's upstream if it has one, otherwise set one on origin.
if git rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
  pushed=$(git push -q 2>&1)
else
  if ! git remote get-url origin >/dev/null 2>&1; then
    say "committed $revision on $branch; no remote to push to"
    exit 0
  fi
  pushed=$(git push -q -u origin "$branch" 2>&1)
fi
if [ $? -ne 0 ]; then
  say "committed $revision on $branch, but the push failed: $pushed"
  exit 0
fi

say "committed and pushed $revision to $branch"
