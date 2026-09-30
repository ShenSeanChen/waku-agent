# Writing rules

These rules apply to docs, UI copy in the dashboard and the CLI, SKILL.md
bodies, PR descriptions and commit messages.

## Output format

A coding agent that writes a document outputs the document only: no preamble,
no closing summary, and no explanation of what it did or why.

## Default reader

The default reader is a developer who just found this repo, has never seen
Waku, has 30 seconds, and will build directly from what you wrote. They will
not ask follow-up questions.

## Rules

1. Every sentence needs a subject, a verb and an object. If any of the three
   is missing, the sentence does not ship.
   - Bad: "This is the first screen that shows anyone."
   - Good: "This is the first screen that shows the failure reason."

2. No "the + abstract noun". Replace *the count*, *the payoff*, *the flow* or
   *the real thing* with the concrete thing it stands for.
   - Bad: "The count is the payoff."
   - Good: "This number is what the user has been waiting for: 3 files
     produced 6 memories."

3. No fragments, and no sentences built only from phrases.
   - Bad: "Borrowed, not rebuilt."
   - Bad: "Quiet, not disabled."

4. One sentence does one job. A sentence that describes the screen does not
   also render a judgment. Judgments go in a separate, labelled block.

5. Never invert, compress or drop words to make an ending land harder. Plain
   declarative sentences are the default.

6. Every sentence answers who did what to what. If a sentence can't, don't
   write it, and don't narrate this check.

7. Put the conclusion in the first sentence. A paragraph that builds to its
   point makes the reader carry everything until the end.
   - Bad: "The person has nothing to look at while we read the three files, so
     we are proposing a screen. This screen does not exist yet."
   - Good: "This screen does not exist yet. The person has nothing to look at
     while we read the three files, so we are proposing one."

8. Write about the thing, not about the document. How a doc was assembled,
   what was borrowed, and what is not drawn yet are not what the reader came
   for. A sentence that passes every rule above and still describes the
   document gets deleted, not fixed.
   - Bad: "A dashed edge marks every control the prototype does not answer for."

9. Use the words the reader already has. Replace an internal name (a module,
   a table, a background job) with what the person actually experiences.
   - Bad: "a file took too long and the reaper stopped it"
   - Good: "a file hangs long enough that we give up on it"

10. Quote what is on the screen, and name the number. Pick one example and
    carry it through the whole document.
    - Bad: "It says how many files were queued, and stops there."
    - Good: "The person sends three files, sees '3 files queued,' and never
      hears another word."

11. A sentence about the product has to be true of the product. Check the
    claim against the code before it ships.
    - Bad: "Dead controls drop to the disabled colour." The code applied an
      opacity and a grayscale filter, and the design system forbids both.

## Naming

Use one label per state, not two. `SENT / Just sent` is one label too many:
pick one and use it everywhere.

Never name anything by a property that changes. An `orange diamond` is orange
in the dark theme and brown in the light one, so the legend entry stops being
true the moment the theme flips.

## Legends

Define only the symbols the document actually uses, and remove unused entries.

## Where the other writing rules live

Commit messages follow [conventions §5](conventions.md#5-git-commits-and-prs).
A SKILL.md description follows the skill section of
[CONTRIBUTING.md](../../CONTRIBUTING.md), because a test checks which messages
load it.
