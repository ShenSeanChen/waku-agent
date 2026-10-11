---
name: knowledge-drill
description: Knowledge drill: assess understanding, test proficiency, quiz concept mastery from fundamentals to advanced edge cases; drill me, diagnose gaps.
---

## How to conduct the drill

1. Determine the topic and target depth:
   - If the user specifies a difficulty (basic, intermediate, senior/advanced), start at that tier.
   - If unspecified, start at intermediate depth: ask about internal mechanics, not simple dictionary definitions.
2. Ask exactly ONE focused question per turn. Never send multiple questions at once.
3. On every user reply, assess immediately:
   - Validate what was accurate.
   - Point out specific misconceptions, hand-waving, or missing trade-offs without generic flattery.
   - Escalate if strong (probe failure modes, edge cases, distributed scaling).
   - Drop down if struggling: explain the core principle in 2 sentences, then test with a simpler scenario.
4. Conclude when the user requests a verdict or after 3-5 rounds:
   - Deliver the Diagnostic Card (format below).
   - Offer to `save_note` the identified blind spots so memory tracks what to study next.

## The diagnostic card

- **Topic & Assessed Level** — one line (e.g. Distributed Consensus: Strong Intermediate).
- **Core Strengths** — 2 bullet points on solid mental models demonstrated.
- **Blind Spots** — 2 bullet points on missing mechanisms or invalid assumptions.
- **Next Concept to Master** — 1 concrete study recommendation.

## Edge cases

| Situation | Do |
|---|---|
| User gives a very short or vague answer | Ask them to explain the mechanism: "What happens under the hood when...?" |
| User asks for the answer instead of guessing | Provide the answer clearly, then follow up with an adjacent test question |
| User specifies no topic | Ask which technical concept, tool, or architecture they want to drill |
