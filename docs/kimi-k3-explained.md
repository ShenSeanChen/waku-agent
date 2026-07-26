# Kimi K3, explained for a stats brain

A companion to the whiteboard in [`docs/whiteboards/k3-architecture.excalidraw`](whiteboards/k3-architecture.excalidraw).
Written for someone who knows statistics well but hasn't touched deep learning in a
while. Every concept is anchored to something you already use — weighted averages,
online estimators, mixture models, boosting, memoization — so the jargon stops being
jargon.

**The one-paragraph mental model.** Attention is a weighted average over words; KDA
makes it a *running* average (linear cost, not n-squared); AttnRes lets layers reuse
earlier layers like boosting stages reaching back; MoE is a sparse mixture model that
only evaluates a few components per input; serving is pure ops — split reading from
writing and memoize the context so repeat loops are cheap; and the price tag is just
those savings passed through.

---

## The whiteboard, section by section

### 1. The MoE — a 2.8T brain, only ~50B awake at once

**Base idea.** Instead of one giant network, MoE (Mixture of Experts) holds many small
"expert" sub-networks (896 of them) plus a *router* that, for each word, picks the few
most relevant experts (16). Only those fire.

**Stats analogy.** This is a **mixture model with a gating function** — the same
Jacobs & Jordan mixture-of-experts idea from the early '90s: a gate assigns each input
to components, and each component is its own little regression. The twist is
*sparsity* — you only evaluate the **top-k components**, not all of them. Like a
mixture model where you skip every component with negligible responsibility.

**What K3 does.** 2.8T total parameters *owned*, but only ~50B *active* per token
(16 of 896). "Stable LatentMoE" = the routing is done in a compressed (latent) space
and tuned not to wobble. "Quantile Balancing" = keep component usage even, so no expert
is overworked and none goes dead (avoiding the degenerate-mixture failure mode).

> Say it: *"Own a mansion, but only light one room per guest — frontier size, mid-size running cost."*

### 2. Attention → KDA (Kimi Delta Attention)

**Base idea.** For each word, attention asks "how relevant is every other word to me?",
turns those into weights, and takes a weighted sum of their values.

**Stats analogy.** That is a **kernel-weighted estimator** (Nadaraya–Watson): the
weights are a similarity kernel, the output is a weighted mean. To get the weights it
compares *every pair* of words — an n-by-n matrix, like a full Gram / pairwise-distance
matrix. n words → n-squared work. **Double the words → 4x the work.** At 1M words that's
fatal. (This "n-squared" is the whole reason long context is expensive.)

**What K3 does.** KDA computes the same thing **recursively** instead of recomputing.
You'd never get a mean by re-summing all data on every new point — you keep a **running
mean and update it** (Welford), an **exponentially-weighted moving average**, or a
**recursive-least-squares / Kalman** update. KDA carries a small **state matrix
(sufficient statistics)** and updates it token by token — linear cost, not quadratic.
The **"Delta"** is the delta rule: update by the correction — erase the stale
association, write the new one — like updating regression coefficients online as (x, y)
pairs stream in.

The pink **"full attention checkpoint"** rows: a recursive estimator drifts as
approximation error accumulates, so every few layers K3 does the *exact* full attention
once to re-anchor — like periodically re-fitting on all the data to correct drift.

> Say it: *"KDA is attention computed like a running average instead of re-summing everything each step."*

### AttnRes (Attention Residuals)

**Base idea.** A deep network stacks layers. A **residual connection** makes each layer
output `input + f(input)` — it keeps a running total and each layer adds a *correction*.

**Stats analogy.** That is **gradient boosting**: each stage fits what's left over (the
residual) and you sum the contributions. It's what lets you stack 100 layers without the
signal degrading. Standard residuals are first-order — layer *k* builds only on layer
*k-1* (Markov-like: each state depends only on the one before).

**What K3 does.** AttnRes lets a later layer take a **learned weighted combination of
*all* earlier layers**, not just the previous one — it *attends over depth*. Think of it
as a regression over all previous boosting stages rather than only the most recent, a
learned mixture that reuses whichever earlier feature stage is useful.

> Say it: *"AttnRes = each layer can reuse any earlier layer's work, not just the one right before it."*

### 3. Serving — built for loops

Not modeling at all — this is the *ops* of running the trained model.

**Two phases with opposite profiles.**
- **Prefill** = read your whole prompt at once (all input tokens in parallel) →
  compute-heavy, throughput job.
- **Decode** = write the answer one token at a time → sequential, latency-bound.

These are as different as a nightly batch job vs. a live web request. **Disaggregated
inference** = run them on *separate machine pools*, each tuned for its job (same instinct
as separating heavy ETL from low-latency query serving).

**Cache = memoization of sufficient statistics.** While generating, the model stores the
representations (keys/values) of tokens it already processed — a **KV cache** — so it
doesn't recompute them. Just **memoization**: don't recompute a statistic on data you've
already seen.

**Why it matters for agents.** An agent loop resends almost the *same* context every
iteration. Almost all of it is already cached → a **cache hit** → you pay ~1/10th
($0.30 vs $3 per million tokens). K3 was engineered so repeated agent loops are cheap.

> Say it: *"Split reading from writing across machines, and never recompute context you've already seen."*

### 4. The price tag ($/M tokens)

**Base idea.** Pricing is quoted per **million tokens** (a token is roughly 3/4 of a
word), split into *input* (what you send) and *output* (what it generates). K3 is
cheaper on both than Opus 4.8 / GPT 5.5 here, and the price stays flat even at 1M
context.

**Stats analogy.** Think of it as **marginal cost per observation**. Three earlier design
choices each drive that marginal cost down, and the price tag is just those savings
passed through:
- MoE → only ~2% of parameters compute per token (cheap *compute* per token).
- KDA → attention is linear, so the marginal cost of one more token doesn't blow up as
  the context grows (that's *why the price is flat across 1M* — no n-squared penalty).
- Cache → re-sent context is a cache hit, so the *effective* input price collapses to
  $0.30 in agent loops.

So the numbers on the card aren't arbitrary — they're the direct financial shadow of
sparsity + linear attention + memoization.

> Say it: *"Cheap and flat because the marginal cost of each extra token was engineered down three different ways."*

---

## Brief history of language models

The whole field is one question answered better each era: **given the words so far,
predict the next word.**

### Bag-of-words
**Idea.** Count which words appear; ignore order. **Stats analogy.** A multinomial /
term-frequency vector — a histogram of tokens. "not good" and "good not" are identical
because a histogram has no order.
> Say it: *"Gets the topic, misses the meaning."*

### LSTM
**Idea.** Read left-to-right, carry a memory as you go. **Stats analogy.** A
**state-space model / recursive filter** over the sequence — a hidden state updated each
step (Kalman-flavored). Two flaws: sequential (slow), and the state fades over long
gaps (forgets the beginning).
> Say it: *"A reader with short-term memory — you built these — but slow and forgetful."*

### Attention (2017) — the turning point
**Idea.** Instead of a running state, look at *all* words at once and weigh what matters.
**Stats analogy.** The kernel-weighted average from Section 2 — but computed over the
whole sequence in parallel, so no sequential bottleneck and no forgetting the start.
> Say it: *"The breakthrough — the one idea the whole talk hinges on."*

### Transformer
**Idea.** Stack attention into many layers. **Stats analogy.** Compose the weighted-
average operation with residual (boosting) connections, deep. The architecture, not just
the trick.
> Say it: *"Attention was the engine; the Transformer is the whole car."*

### LLM
**Idea.** Take a Transformer, make it enormous, train it on the whole internet to predict
the next word. **Stats analogy.** Maximum-likelihood fitting of a next-token distribution
at absurd scale — and the general skills (writing, coding, reasoning) *emerge* from that
single objective.
> Say it: *"This is ChatGPT / Claude / Kimi — the thing you actually use."*

### The cost wall
**Idea.** Scale isn't free. **Stats analogy.** Two costs: too many parameters to run
cheaply, and attention's **n-squared** — double the words, quadruple the work (Section 2).
That's the hidden price tag on long context.
> Say it: *"The reason a bigger, longer-memory model should be unaffordable."*

### Kimi K3
**Idea.** The Transformer/LLM idea that *beats* the wall with three tricks. **Recap.**
MoE (sparse mixture → cheap compute), KDA (running-average attention → kills n-squared),
FP4 (low-precision numbers → 4x smaller). Each attacks one part of the wall.
> Say it: *"Frontier scale, million-word memory, but priced to run cheap — that's what's new."*

---

## Glossary — each acronym with its stats twin

| Term | Plain meaning | Stats twin |
|---|---|---|
| **MoE** | many small experts, only a few fire per word | sparse mixture model + gating (top-k components) |
| **active vs total params** | owned vs actually computing per token | full model vs the components you evaluate |
| **Attention** | weighted look-back over all words | kernel-weighted average (Nadaraya–Watson) |
| **n-squared** | every word compares to every word | full Gram / pairwise matrix; 2x data → 4x work |
| **KDA** | linear attention via a running state | online estimator (Welford / EWMA / RLS) + delta rule |
| **Residual** | add the input back after each layer | gradient boosting (sum of stage-wise corrections) |
| **AttnRes** | layers reuse any earlier layer, not just the last | regression over all prior stages, learned mixture |
| **Gated MLA** | compress the KV cache | low-rank / PCA-style reduction, learned on/off |
| **SiTU** | an activation function | the nonlinearity / link function inside a unit |
| **MXFP4 / MXFP8** | store numbers in 4 / 8 bits | reduced precision — keep 2 sig figs, not 8 |
| **QAT** | train aware of low precision | fit a model knowing measurements are coarsely binned |
| **Per-Head Muon** | the training optimizer, per attention head | the solver that updates weights (like your fitting routine) |
| **prefill / decode** | reading the prompt / writing the answer | batch job vs live sequential request |
| **KV cache / cache-hit** | reuse work for tokens already seen | memoization of sufficient statistics |
| **context window** | how much it reads at once (1M ≈ thousands of pages) | sample size the estimator sees in one pass |

---

## The honest asterisk

The 6.3x / 25% / 2.5x figures are **vendor claims** from Moonshot's own blog; open
weights don't drop until Jul 27, 2026, so nobody has independently verified them.
Opus and Fable internals are undisclosed, so the "vs" is one-directional — you can only
draw K3. That gap is exactly why the plan is to test it live in Waku (same agent, same
tasks, measured on pass rate, dollars, and latency) rather than trust the slide.

*Sources: Moonshot / MarkTechPost, Jul 16 2026; Simon Willison, Jul 16 2026.*
