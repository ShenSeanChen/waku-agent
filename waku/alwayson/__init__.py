"""The always-on parts of Waku (spec 023): what lets the agent work while
nobody is talking to it.

  events.py   the inbox: every incoming message is one row in `events`
  worker.py   the one worker that claims rows and starts a run per session
  outbox.py   every outgoing message is one row in `outbox`
  serve.py    `waku serve`: the handlers and the worker in one process
  sources/    one handler per source; each only inserts a row

A handler never calls `waku.respond()`. It writes a row and returns, so a
message that arrives while a run is busy, or just before a crash, is on disk.
"""
