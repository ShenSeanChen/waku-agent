---
name: schedule-meeting
description: Schedule, book or set up a meeting, call, coffee, lunch, dinner, event or appointment on my calendar.
---

## How to schedule well

1. Resolve relative dates ("next Tuesday", "tomorrow morning") into ISO 8601
   using today's date from the system prompt. Morning = 09:00, afternoon =
   14:00, evening = 18:00 unless the user says otherwise.
2. Check memory context for the attendee's preferences (e.g. "prefers morning
   meetings") and apply them — mention it when you do ("since Alex prefers
   mornings, I booked 9am").
3. Call `create_event` with a short, specific title: "Coffee with Alex", not
   "Meeting".
4. If the user mentioned an agenda or context, put it in `notes`.
5. After creating, confirm in one sentence: what, when, with whom.

## Edge cases

| Situation | Do |
|---|---|
| No time given | Propose a concrete time instead of asking an open question — if memory shows the attendee's preference, lead with it ("Alex prefers mornings — Friday 9am?"). Only ask openly when memory gives you nothing |
| Past date requested | Point it out, suggest the next occurrence |
| Attendee unknown to memory | Schedule anyway; offer to `save_note` who they are |
