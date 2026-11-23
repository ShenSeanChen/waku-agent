---
name: weekly-brief
description: Weekly or daily brief: what's on my week or my day, what to focus on, catch me up, morning briefing, day look-ahead.
---

## How to brief the user

1. Call `read_apple_calendar` (7 days) to get their real schedule — including
   events that arrived by email invite.
2. Call `read_apple_mail` (48 hours) to see what's landed in their inbox. Keep
   the `message://` link for anything worth surfacing.
3. Check memory (the retrieval gate handles this) for their people, projects,
   and preferences so the brief is personal.

Then write a **focus-first** briefing, not a data dump:

- Open with the 1-3 things that actually matter this week (deadlines, key
  meetings, anything time-sensitive from mail).
- Group the rest by day. Note who each meeting is with and why it matters if
  you know them from memory.
- For emails that need action, give a one-line "why" and paste the
  `message://…` link so they can jump straight to it in Mail.
- End with a short "suggested focus for today."

Keep it skimmable — short lines, no filler. If Calendar or Mail is unavailable
(permission not granted), say so plainly and brief on what you can.
