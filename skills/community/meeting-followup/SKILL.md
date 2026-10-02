---
name: meeting-followup
description: Summarize a completed meeting, extract decisions and action items, and draft follow-up messages. Use for "follow up after my meeting", "summarize my call", "what do I owe them", or "write a thank-you message".
---

## How to follow up

1. Find the most recent completed matching event with `list_events`. Use the
   title, date, attendee, or time supplied by the user to choose it.
2. Read the event title, time, attendees, and notes. Treat notes as the only
   meeting transcript; do not invent discussion details that are not present.
3. Check relevant memory for each attendee, project, and unresolved promise.
   Prefer memory over web search for personal context.
4. Write a compact follow-up card with:
   - **Meeting**: when, who, and purpose
   - **Decisions**: confirmed outcomes
   - **My action items**: owner, task, and due date when known
   - **Their action items**: owner, task, and due date when known
   - **Open questions**: items needing clarification
5. Offer to save durable facts or commitments with `save_note`. Save only
   things worth remembering beyond this meeting, not temporary wording.
6. If the user asks for a thank-you or follow-up message, draft it with
   `send_message`. That tool writes a local outbox draft; report the filename
   and never imply that a message was sent.

Keep the tone warm and practical. Separate confirmed facts from suggestions,
and keep the result skimmable enough to read immediately after a call.

## Edge cases

| Situation | Do |
|---|---|
| No matching event | Say so and summarize from the user's supplied notes or name |
| Several events match | Use the most recent completed event and state which one you chose |
| Event has no notes | Say the summary is based only on title, time, attendees, and memory |
| No attendees | Summarize the meeting and skip person-specific follow-up |
| Missing owner or due date | Mark it as unassigned or unknown; do not guess |
| User asks to send immediately | Create a local draft with `send_message` and explain it still needs review |
