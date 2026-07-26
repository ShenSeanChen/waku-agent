---
name: meeting-prep
description: Prep me for a meeting or call — who I'm meeting, background, talking points. Use for "prep me for", "get me ready for", "what should I know before", "who am I meeting", "brief me on my call with".
---

## How to prep

1. Find the meeting: call `list_events` and pick the one that matches the
   name or time the user gave. Note the title, time, attendees, and notes.
2. Pull what memory knows about each attendee — who they are, past
   conversations, preferences, anything promised and not yet delivered.
   This is the heart of the prep: the goal is walking in like you remember
   everything.
3. If the attendee or their company is public-facing and memory is thin,
   one `search_web` for recent news. Skip this for personal meetings —
   coffee with a friend doesn't need a briefing document.
4. Write the prep card (format below), then offer to `save_note` it so it's
   there to glance at on the way in.

## The prep card

- **When** — one line: time, duration, where/how.
- **Who** — 2-3 lines per attendee: relationship, last interaction, open
  threads (anything owed in either direction).
- **Why now** — the agenda from the event notes, plus anything memory says
  is unresolved with this person.
- **Three talking points** — concrete, memory first, web second. "Ask how
  the Berlin launch went" beats "discuss recent developments".

Keep it skimmable — it gets read in the elevator, not at a desk.

## Edge cases

| Situation | Do |
|---|---|
| No matching event on the calendar | Say so, then prep from the name alone using memory and web |
| Several events match | Prep the next upcoming one; list the others in one line |
| Memory knows nothing about the attendee | Say that plainly, lead with web results, and offer to `save_note` who they are after the meeting |
| "Prep my day" | One compact card per meeting, ordered by time, sharpest points only |
