# Fidelity protocol for existing strategies (mandatory)

**Why:** if we code a trader's strategy differently from how he trades it and it fails, we learn
nothing and we wrongly bury a working idea. Every Track 2 strategy goes through these steps, in order.
No step may be skipped, by Claude or by any agent.

## 0. Hard rule: no copy without enough information
An existing strategy is copied **1:1 or not at all**. Before writing any code, check that every rule
(context, location, trigger, stop, management, exit, risk) is known from the trader's own words.
If something is unknown, proprietary or only in a source we don't have yet: **stop, say exactly what is
missing, and ask for the source.** Never fill gaps with a plausible guess and call it his strategy.
This applies to Claude and to every agent. (Lesson: VALENTINI-4 draft, 2026-11-14.)

## 0b. Hard rule: full audit BEFORE the first line of code
Before any code is written, go through the **entire** transcript once more, start to finish, and fill the audit table
(`audits/<NAME>_audit_<date>.md`) with four sections: **A** misread (code would contradict his words),
**B** forgotten (indicators/rules he uses that are not in the sheet), **C** not copyable with our data,
**D** correct. Every indicator he mentions anywhere (profile, delta, CVD, big trades, absorption, follow-through,
session ranges, bias rules, management) must appear in one of the four sections. Code starts only when A and B are
empty or explicitly accepted by Kian. (Lesson: VALENTINI suite v4 was tested with 4 misreadings and 5 missing tools,
found only on Kian's second request, 2026-11-15.)

## 1. Primary source
- Work from **the trader's own words**: video transcript (YouTube → "…more" → Show transcript),
  his own written material, or his chart walkthroughs. Gemini recaps are an **index** to find the right
  segments, not a source: they summarise, merge and sometimes invent structure.
- Save the transcript segments in `sources/` with the video URL and timestamps.

## 2. Rule sheet (before any code)
`rulesheets/<NAME>.md`, one row per rule. Every row has:

| # | Rule | Quote (timestamp) | Status |
|---|---|---|---|

Status is one of:
- **STATED:** he says it explicitly; quote attached.
- **INTERPRETED:** he is vague ("tight", "strong", "zone") and we pin one definition. The row says what
  we chose and why, and lists plausible alternatives (to test as variants).
- **MISSING:** the rule needs data or discretion we do not have (e.g. order-flow bubbles, his feel).
  The row says which proxy, if any, stands in.
- **CONFLICT:** two sources say different things (e.g. buy-stop entry vs body-close entry). Both
  become variants; neither is silently picked.

Also listed: **context and preconditions** (market state, session, what must have happened before the
setup), **entry**, **stop**, **management**, **exit**, **risk/discipline rules**, **markets/timeframes**.
Context rules are the most often lost and matter most (Valentini: out-of-balance filter = +20-30% win rate).

## 3. Kian reviews the sheet
No code until Kian has approved the rule sheet. INTERPRETED rows are where his read of the video
counts most.

## 4. Implementation = the sheet, nothing else
- Code comments cite the rule-sheet row numbers. Nothing in code that is not in the sheet.
- A second implementation (Python harness vs Pine) of the same sheet is the cross-check: same period,
  same trades, or we find out why.

## 5. Fidelity grade on every result
- **Faithful:** all STATED rules implemented, INTERPRETED rows approved, nothing MISSING.
- **Proxy:** some MISSING rules replaced by proxies (named in the result).
- **Simplified:** rules left out. Never used to judge the trader's strategy.

Reports say "our *proxy/simplified* version of X fails", never "X does not work", unless the grade
is Faithful.

## 6. Change log
Any change after approval is a new version (`NAME-v2`) with the reason. Results never move between versions.
