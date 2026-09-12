---
name: your-skill-name
description: One sentence saying what this skill does AND when to use it — the loader matches user messages against these words, so use the words specific to this job (the nouns people say), not question words like why, what, should or for.
---

<!--
To contribute: copy this file to skills/community/<your-skill-name>/SKILL.md
and open a PR. CI checks the frontmatter (name + description required — the
official Anthropic Agent Skills format). A skill loads when a message shares 2+ words (3+ letters) with its name and
description, so every word you put there is a trigger. CI checks it stays quiet
on everyday messages (evals/deterministic/test_skill_triggers.py). Keep the body under ~60 lines:
skills are loaded into the prompt only when they match, but shorter is better.
-->

## Instructions

Step-by-step guidance for the model. Be concrete: name the tools to call
(`create_event`, `save_note`, `send_message`), the defaults to assume, and
the tone to take.

## Edge cases

| Situation | Do |
|---|---|
| Something ambiguous | Ask one clarifying question |
