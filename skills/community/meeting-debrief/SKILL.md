---
name: meeting-debrief
description: Capture what happened after a meeting or call — decisions, who owes what, follow-ups to book. Use for "debrief", "how did it go", "log my meeting", "what did I commit to", "wrap up my call with", "that meeting just ended".
---

## How to debrief

1. Find the meeting: call `list_events` and pick the one that just ended, or
   the one the user named. If the calendar has nothing, work from the name
   alone — a debrief is still worth having.
2. Ask **one** open question and stop: "How did it go?" Do not interrogate.
   Most of what matters arrives unprompted in the first answer.
3. Sort what you hear into the four buckets below and show the card. If a
   bucket is empty, drop the heading — an empty "Owed by them" is noise.
4. Then, and only then, write things down (next section). Show the card
   first so the user can correct it before it becomes memory.

## The debrief card

- **Decided** — what is now settled. One line each, past tense.
- **I owe** — the user's commitments. Include the deadline they said out loud;
  if they gave none, write "no date" rather than inventing one.
- **They owe** — the same for the other side, named per person.
- **Open** — what got raised and not resolved. This is the part that
  disappears if nobody writes it down.

## Writing it down

Work through these in order, and say what you did in one line at the end.

- `save_note` — one call per durable fact, `subject` being the person or the
  project it belongs to. Facts, not narration: "Priya owns the migration
  rollback plan" is worth keeping; "the call ran long" is not.
- `create_event` — one per commitment that has a date. Title it as the action
  ("Send Priya the rollback plan"), not as the meeting.
- `send_message` — offer, don't assume. If the user promised to send something
  to someone, ask whether to draft it. It lands in the outbox unsent either way.
- `manage_memory` — only when the meeting made an existing memory wrong.
  Search for it first to get the id, then update or delete that id.

Never say something was scheduled or saved until the tool has returned. If a
call fails, say which one and leave the rest of the card intact.

## Edge cases

| Situation | Do |
|---|---|
| "It was fine" and nothing else | Take it at face value. Log attendance, offer one nudge: "anything you owe anyone?" |
| Meeting the user cancelled or skipped | No card. Ask whether to rebook, then stop |
| A decision contradicts what memory says | Show both, ask which holds, then `manage_memory` the loser |
| Commitment with a vague date ("next week") | Put it in the card as-is; ask for a day before making an event |
| Debriefing several meetings at once | One card each, shortest first, then a single combined list of what's owed |
| The user is venting, not reporting | Let them. Pull the facts out afterwards; do not interrupt with buckets |
