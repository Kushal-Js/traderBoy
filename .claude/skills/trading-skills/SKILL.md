---
name: trading-skills
description: Curated trading knowledge for DhanBoy/K01 (the options trading bot in this repo) - screener filter breakdowns (Krishvi, DanDanaDan-2, Kaashvi-28), exit-mechanics findings, backtest methodology, config-tuning history, and intraday-options-trading fundamentals (Greeks, liquidity, timing), maintained at github.com/Kushal-Js/trading-skills. ALWAYS consult this before analyzing a Chartink screener URL, interpreting a backtest result, investigating a real trade/incident, or making a bot config/strategy tuning decision - even if the user doesn't explicitly mention "trading-skills" or the repo by name. Also use it to record new findings there afterward (auto-update + push, no need to ask - see the repo's SAFETY.md for what stays off-limits regardless).
---

# trading-skills

This skill wraps the **trading-skills** knowledge base
(github.com/Kushal-Js/trading-skills) — a separate repo from `traderBoy`
(the bot's actual code) holding durable findings, incident write-ups, and
design docs about how DhanBoy/K01 and the market actually behave. The
split is deliberate: `traderBoy` is bot code, `trading-skills` is
learnings and skills, per the user's own instruction when it was created.

## Where the content lives

Local clone (kept in sync by this session's own commits):
`/Users/kushalgaur/Desktop/projects/trading/trading-skills`

Before relying on any file's content for something that matters (a config
decision, an incident conclusion), run `git pull` in that directory first
— other sessions/devices may have pushed updates since it was last read.

If the local clone is missing, clone it fresh:
`git clone https://github.com/Kushal-Js/trading-skills.git /Users/kushalgaur/Desktop/projects/trading/trading-skills`

## What's in it

Read the repo's own `README.md` for the authoritative, current structure
and writing style — it's short and this file won't duplicate it. In
brief:

- **`SAFETY.md`** — the non-negotiable boundary: no autonomous real
  trades or live-bot restarts, no matter how much knowledge accumulates
  here. Read this first if a task starts drifting toward "the docs say X
  looks good, so let's just do it."
- **`learnings/`** — durable findings by topic: `exit-mechanics.md`,
  `capacity-and-ranking.md`, `backtest-methodology.md`,
  `technical-patterns/` (Minervini Trend Template, VCP, classic chart
  patterns), `screener-analysis/<scan-name>.md` (one file per Chartink
  scan analyzed so far), `intraday-options-trading/` (Greeks/decay,
  liquidity/execution, timing patterns), and
  `paper-trading-shared-infrastructure-risk.md`.
- **`incidents/YYYY-MM-DD-<slug>.md`** — dated case studies of specific
  notable trades/days worth remembering in detail.
- **`designs/<name>.md`** — proposals for new tools/strategies, each with
  a `Status:` line tracking whether it's still a proposal, built, or
  deployed (e.g. `designs/k01.md`).

## When to consult it

- **Analyzing any Chartink screener** (a `chartink.com/screener/...` URL)
  — check `learnings/screener-analysis/` first; several scans are already
  broken down there (full filter table, structural comparison to siblings
  already analyzed, any parameter mismatches against the bot's own
  indicator settings). Write a new one in the same format rather than
  starting from a blank page each time.
- **Interpreting a backtest result** — `learnings/backtest-methodology.md`
  covers what `traderBoy/bt_common.py` actually replicates vs.
  approximates, and the standard workflow for a new CSV.
- **Investigating a real trade or live incident** — check `incidents/` for
  a similar prior case before re-deriving everything from scratch, and
  write up a new one if the investigation turns up something reusable.
- **Making or reviewing a config/strategy tuning decision** —
  `learnings/exit-mechanics.md`, `learnings/capacity-and-ranking.md`, and
  `learnings/intraday-options-trading/` hold the reasoning behind several
  already-made decisions; check whether the current question is already
  answered there before re-investigating from scratch.
- **Building or reviewing a new paper-trading strategy** (like K01) —
  `learnings/paper-trading-shared-infrastructure-risk.md` covers a real,
  non-obvious risk (a paper strategy sharing a broker connection/process/
  rate-limit budget with real-money strategies) worth checking before its
  first live session, not after.

## Keeping it current — standing authorization, don't ask first

The user's own instruction (30 Aug 2026): **"Keep updating this yourself
and push everything on this repo."** Whenever a session produces a
finding worth keeping — a screener's filter logic pulled from Chartink, a
real incident investigated, a config change's measured effect, a
mechanism discovered — write it up as a new or updated `.md` file under
`learnings/`, `incidents/`, or `designs/`, commit, and push, in the same
session, without asking first. This is pre-approved and carries none of
the real-money risk that gates changes to `traderBoy` itself (which still
goes through the full test → confirm → deploy checklist).

Update an existing file rather than leaving a stale one when a later
finding supersedes it — note what changed and why (see the repo's own
`README.md` style guide), don't just delete the history.

## What this skill does NOT authorize

Same boundary as the repo's own `SAFETY.md`: no real order is ever
placed, and the live bot is never started or restarted, based on anything
this skill's content suggests — "the docs say this setup looks good" is
never itself a reason to act on real money or a live service. Those still
require the user's explicit, current go-ahead every time, per `traderBoy`'s
own established safety practice.
