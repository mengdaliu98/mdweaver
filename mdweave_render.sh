# mdweave_render -- copy a markdown file into the knowledge base, render it,
# and open it in a browser.
#
#   source /Users/mengda/AAI/knowledge_base/toolings/mdweave_render.sh
#   mdweave_render ~/notes/some_doc.md
#
# Add that source line to ~/.zshrc to keep the command around.
#
# The page is served over http rather than opened as a file:// URL, because
# adding comments needs the local API. A background `mdweave serve` is started
# on first use and reused after that; `mdweave_stop` shuts it down.
#
# Override MDWEAVE_HOME if the knowledge base moves, MDWEAVE_PORT if 8765 is
# taken.

: "${MDWEAVE_HOME:=/Users/mengda/AAI/knowledge_base}"
: "${MDWEAVE_PORT:=8765}"

_mdweave_up() {
  curl -fsS -m 1 "http://127.0.0.1:${MDWEAVE_PORT}/api/health" >/dev/null 2>&1
}

mdweave_serve() {
  local bin="$MDWEAVE_HOME/toolings/.venv/bin/mdweave"
  local inputs="$MDWEAVE_HOME/contents/markdown_inputs"
  local outputs="$MDWEAVE_HOME/contents/html_outputs"
  local log="${TMPDIR:-/tmp}/mdweave-serve.log"

  _mdweave_up && return 0

  ( nohup "$bin" serve -i "$inputs" -o "$outputs" --port "$MDWEAVE_PORT" \
      >"$log" 2>&1 & ) >/dev/null 2>&1

  local i=0
  while [ $i -lt 20 ]; do
    _mdweave_up && return 0
    sleep 0.25
    i=$((i + 1))
  done

  echo "mdweave: server did not come up on port $MDWEAVE_PORT" >&2
  echo "  log: $log" >&2
  return 1
}

mdweave_stop() {
  if pkill -f "mdweave serve .*--port ${MDWEAVE_PORT}" 2>/dev/null; then
    echo "mdweave: stopped the server on port $MDWEAVE_PORT"
  else
    echo "mdweave: no server running on port $MDWEAVE_PORT"
  fi
}

mdweave_render() {
  local src="$1"
  if [ -z "$src" ]; then
    echo "usage: mdweave_render <path-to-markdown>" >&2
    return 2
  fi

  local bin="$MDWEAVE_HOME/toolings/.venv/bin/mdweave"
  local inputs="$MDWEAVE_HOME/contents/markdown_inputs"
  local outputs="$MDWEAVE_HOME/contents/html_outputs"

  if [ ! -f "$src" ]; then
    echo "mdweave_render: no such file: $src" >&2
    return 1
  fi
  if [ ! -x "$bin" ]; then
    echo "mdweave_render: renderer not found at $bin" >&2
    echo "  rebuild it with: cd $MDWEAVE_HOME/toolings && uv venv .venv && uv pip install --python .venv/bin/python -e ." >&2
    return 1
  fi

  # Absolute path, so a relative argument still works.
  src="$(cd "$(dirname "$src")" && pwd)/$(basename "$src")"

  local name target sidecar url
  name="$(basename "$src")"
  target="$inputs/$name"

  mkdir -p "$inputs" "$outputs" || return 1

  # Skip the copy when the file already lives in markdown_inputs, so re-running
  # on an already-imported document does not try to copy it onto itself.
  if [ "$src" != "$target" ]; then
    cp "$src" "$target" || return 1
    # Bring the annotation sidecar along if the source has one.
    sidecar="${src%.*}.ann.json"
    if [ -f "$sidecar" ]; then
      cp "$sidecar" "$inputs/$(basename "$sidecar")" || return 1
    fi
  fi

  "$bin" build "$target" -o "$outputs" || return 1
  mdweave_serve || return 1

  url="http://127.0.0.1:${MDWEAVE_PORT}/${name%.*}.html"
  echo "$url"
  open "$url"
}
