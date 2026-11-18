"""The always-on parts of Waku (spec 023): what lets the agent work while
nobody is talking to it.

  events.py      the inbox: every incoming message is one row in `events`
  worker.py      the one worker that claims rows and starts a run per session
  runs.py        every run the worker starts is one row in `runs`
  approvals.py   a tool call that needs the owner's yes waits for it
  outbox.py      every outgoing message is one row in `outbox`
  dispatcher.py  sends each row, and retries a delivery that failed
  senders/       one sender per channel; only the dispatcher calls one
  timers.py      a follow-up the agent set is one row in `timers`; the timer
                 loop writes each due row into `events`
  waiting.py     lists the timers, calls and messages that wait, and cancels
                 one
  serve.py       `waku serve`: the handlers, the worker, the timer loop and
                 the dispatcher in one process
  sources/       one handler per source; each only inserts a row

A handler never calls `waku.respond()`. It writes a row and returns, so a
message that arrives while a run is busy, or just before a crash, is on disk.
"""
