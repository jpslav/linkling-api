# ADR-0002 — The alphabet and length of generated names

- Status: Accepted
- Approver: @jpslav
- Date: 2026-09-22

## Context

"Made-up ones should be short and easy to read out loud or type from a slide — no
characters people confuse" (`raw/brief.md#L16-L17`), named alongside the URL shape as
something to settle "before anything ships" (`#L44-L45`).

That one sentence names **two different confusions**. *Visual*: `0/O`, `1/l/I`, `5/S`,
`2/Z`, `8/B` — most of which disappear once the alphabet is lowercase-only (ADR-0001c).
*Spoken*: the rhyming set `b c d e g p t v z`, plus `m/n`, `s/f`, `i/y`. No single-symbol
alphabet is good to read aloud — that is why phonetic alphabets exist — so "read out loud"
is really a choice between *symbols plus a spelling the CLI prints* and *words*.

**The brief does not say** the target length, nor whether the set of targets is
confidential — which is what decides how enumerable the namespace may be. That second one
is a question for the owner and it changes the answer.

## Decision

**Proposed.** Namespace size N and the guess-hit rate k/N — the chance a random probe lands
on a live link and reveals its target — at k live links:

| Option | Symbols | len | N | k=1k | k=20k | k=100k | Recurring |
|---|---|---|---|---|---|---|---|
| A. lowercase alnum minus `0 o 1 l i` | 31 | 6 | 887M | 1.1e-6 | 2.3e-5 | 1.1e-4 | R4 — random strings spell words, some unfortunate, so a blocklist gets an owner |
| **A′. consonants + `2-9`, excluding `a e i o u` and `y`** | 28 | 6 | 482M | 2.1e-6 | 4.1e-5 | 2.1e-4 | **R1** |
| B. lowercase letters minus `l i o` | 23 | 7 | 3.4B | 2.9e-7 | 5.9e-6 | 2.9e-5 | R1 |
| C. two short words — `calm-otter` | 1296² | 2 | 1.68M | 6.0e-4 | **1.2e-2** | 6.0e-2 | R4 — the word list is curated, as is the "unfortunate pairs" filter |

Reading the table: at 20,000 links, the two-word option leaks a live target roughly every
83 probes — an afternoon's scan enumerates the team's entire link set. A′ at length 6 needs
about **24,000** probes per hit.

**The arithmetic in this table was recomputed rather than inherited.** The exact alphabet
matters and the first draft of this ADR got it wrong: "no vowels" alone leaves `y`, which is
29 symbols and 29⁶ ≈ 595M, not the 482M in the row. 482M is 28⁶, so the alphabet is the 20
consonants **excluding `y`** plus the digits `2`–`9`. Excluding `y` is independently
defensible — it is vowel-like when read aloud and is confusable with `v` in several slide
fonts — but it has to be *stated*, because "no vowels" and the number in the table were
describing two different alphabets. The probe figure moved with it: 24,095 rather than the
44,375 that belongs to option A's 31 symbols.

**Recommend A′: length 6, lowercase, excluding the five vowels, `y`, `0` and `1`** — the 28
symbols `b c d f g h j k l m n p q r s t v w x z 2 3 4 5 6 7 8 9`. Why this one:

1. "Short" is the first thing the brief asks for, and the memorable case is already covered
   by custom names (`q3-plan`, `#L15`) — the generator does not have to be memorable too.
2. Excluding vowels means a generated name **cannot spell anything**, which removes the
   blocklist that option A would otherwise hand somebody to maintain. That is the only R4
   in this door, and it is removed by construction rather than by a list.
3. Read-aloud is solved without lengthening the link: the CLI prints
   `x7kq2m  (x-ray seven kilo quebec two mike)` beside it. Cheap, reversible, and it keeps
   the printed artefact short.

**Collision handling is not optional and must not be traded against length.** At 20,000
links drawn from the recommended 28⁶ the probability of at least one collision is **≈0.34**,
and at 28⁵ it is **≈1.000**. (For option A's 31 symbols: ≈0.20 at 31⁶, ≈0.999 at 31⁵.) The
generator retries on the `UNIQUE` violation, and no length may be chosen on the grounds that
retries are "rare enough to skip" — at the recommended length a collision is roughly a
one-in-three event over the product's life, not a curiosity.

## Consequences

- Names look like `x7kq2m`. Six characters, unambiguous on a slide, nothing spellable.
- Adding a *second* generator later (words, for people who want them) is additive and is
  not foreclosed. **Removing a symbol from the alphabet later is not** — names already
  issued keep it.
- If the answer to "is the link set confidential?" is "never", the two-word option becomes
  viable and this ADR should be revisited before it is Accepted.

**What would settle it** — the one thing that cannot be computed: *transcription error
rate*. Read twenty A′-6 names and twenty two-word names aloud over a call and have the
listener type them; count how many arrive wrong per alphabet. If A′ loses badly the
fallback is **three** words, not two — two is enumerable at team scale, per the table.

## Decision record

Accepted 2026-09-22 by @jpslav, from line comments on [the definition PR](https://github.com/jpslav/tinyworks-program/pull/1). Written by `tools/decision-record.py`; each answer is also in its question file.

- **Is the set of link targets confidential — should a stranger be unable to list them by guessing names?** — **A.** Yes — nobody outside the team should be able to enumerate the targets — @jpslav, 2026-09-22: "Accepting the recommendation." ([comment](https://github.com/jpslav/tinyworks-program/pull/1#discussion_r4074334367)) <!-- decided: 2026-09-17-generated-name-alphabet/confidential: A -->
- **What should a made-up link name look like?** — **A.** Six characters from 28 symbols, no vowels, `y`, `0` or `1` — `x7kq2m`; the CLI prints a phonetic spelling beside it — @jpslav, 2026-09-22: "Accepting the recommendation." ([comment](https://github.com/jpslav/tinyworks-program/pull/1#discussion_r4074334492)) <!-- decided: 2026-09-17-generated-name-alphabet/alphabet: A -->
