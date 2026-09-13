"""Driving Claude sessions on another machine from a button in the page.

The deployed container renders the knowledge base; the devserver holds the
Claude sessions and the checkouts they work in. Those are two machines, and the
one with the sessions has no public address -- so the container can never call
it. Every connection is therefore opened from the devserver outwards.

    browser ──POST /api/agent/jobs──▶ broker (container)
                                          ▲  │
                                          │  │ POST /api/agent/claim
                                          │  │  (held open until there is work)
                                          │  ▼
                                          │  runner (devserver)
                                          │      claude -p --resume <sid>
                                          │      edits the markdown
                                          │      git push
                                          │
                                     POST …/events, …/done, …/reconcile

`broker.py` is the container half, `daemon.py` and `runner.py` the devserver
half, and `protocol.py` the shapes they agree on. One package, deployed from
one repository, so the two ends cannot drift apart.
"""
