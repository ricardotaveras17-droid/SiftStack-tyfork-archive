# Call Coaching — Runbook (stages 1 and 2)

**Reference only. Nothing here runs on a schedule.** These are the commands to type when
you want a coaching batch. Re-verified 2026-08-24: no launchd plist, no cron entry, and no
scheduled task EXECUTES any of these scripts.

One thing does fire weekly, and it runs nothing. The cloud routine **"Call coaching batch —
REMINDER ONLY"** (Mondays 08:00 ET) posts a chat reminder pointing back at this file. It has
no repo attached, no MCP connectors, and only the `Read` tool — it cannot execute a command
even if a future prompt told it to. It is a sticky note, not a scheduler. Do NOT "fix" it by
giving it access to this Mac.

Worth keeping in mind: that routine was created 2026-08-18 with a prompt that tried to run
`pull_calls.py --days 7`. It never worked, because a cloud container cannot see this machine
— which was lucky. `--days 7` overwrites `call_log.json` with only its own window, so a
working version would have shrunk the KPI archive every Monday. Rewritten reminder-only on
2026-08-24.

Covers **stage 1 (pull)** and **stage 2 (transcribe + triage)** only. Stages 3+ (enrich,
export) belong to session 2 and are not documented here yet.

Every command starts with the `cd`. Both scripts resolve paths from `ROOT = Path.cwd()`,
so running them from anywhere else silently looks in the wrong place for `.env`,
`roster.json`, and `smrtphone_state.json`.

---

## The weekly batch

### 1. Pull — free, no API cost

```bash
cd "/Users/ricardotaveras/Desktop/SiftStack" && .venv/bin/python src/call_coaching/pull_calls.py --days 30 --min-seconds 60 --include-dispositions "Correct Number,No Disposition,(none)" --short-floor 30
```

Writes `output/call_coaching/call_log.json`, `calls_to_review.json`, and
`recordings/{call_id}.mp3`.

**Pull 30 days even for a weekly batch.** Each pull OVERWRITES `call_log.json` with only
the window it fetched. Pulling is free, and transcription skips any call it has already
done, so the wider window costs nothing extra. Step 2 below archives the result, so history
survives regardless — but a wide pull keeps the archive dense and catches late dispositions
on older calls.

Why these flags: a bare `--min-seconds 60` discards real short conversations, and Rick
leaves ~31% of his own calls undispositioned, so disposition alone cannot be trusted as a
filter. The `(none)` sentinel reaches rows whose disposition is NULL, which is a different
thing from the literal "No Disposition" label. See the floor analysis below.

### 2. Snapshot — free, run it right after every pull

```bash
cd "/Users/ricardotaveras/Desktop/Rick AI /Projects/kpi-bot" && python3 snapshot_calls.py
```

Merges the pull into `call_history.json`, an append-only archive keyed by `call_id`.
**This is the only thing standing between you and losing KPI history**, because the pull
overwrites `call_log.json` with just its own window. New calls are added, existing ones
refreshed if a disposition landed after the fact, nothing is ever removed.

`--status` reports the archive (span, calls per month, how many of the current log are new)
without changing anything.

Safety: backs up before every write, writes atomically, refuses to proceed on a corrupt or
empty log, and fails loud rather than shrinking the archive. Backups live in
`output/call_coaching/history_backups/`, last 10 kept.

The archive holds owner names and phone numbers. It lives under SiftStack's `output/`,
which is gitignored. **Do not move it into the vault** — `Projects/kpi-bot/` is tracked,
and it would commit PII.

### 3. Transcribe + triage — the only step that costs money

```bash
cd "/Users/ricardotaveras/Desktop/SiftStack" && .venv/bin/python src/call_coaching/transcribe.py
```

Writes `transcripts/{call_id}.md`, `transcripts/{call_id}.json`, and `review_queue.json`.

**Roughly $0.32 per audio-hour** with verification on. A 7-day window is about 2
audio-hours (~$0.65); the 30-day window measured 8.65 audio-hours ($2.77) on 2026-08-18.

Already-transcribed calls are skipped unless you pass `--force`, so re-running after an
interruption costs nothing for completed work, and overlapping windows never re-charge.

### 4. Call KPIs — free, and does not need step 3

```bash
cd "/Users/ricardotaveras/Desktop/Rick AI /Projects/kpi-bot" && python3 kpi_from_calls.py --days 7
```

Reads the archive from step 2 by default, falling back to the raw log if no archive exists.
It never touches transcripts, so it costs nothing and can be run on its own — you can
report KPIs weekly without ever paying for transcription.

Every report names its source in the header, so a number's provenance is never ambiguous.
`--source log` forces the last pull only; `--source history` demands the archive.
`--days 30` for the rolling window, `--by-caller` to break it out, `--json` for
machine-readable output. Therese is excluded automatically via `roster.json`.

Once the archive spans a quarter, `--days 90` works without re-pulling anything.

Nothing is posted anywhere; it reads and prints.

---

## Before you spend

Survey the window without downloading anything:

```bash
cd "/Users/ricardotaveras/Desktop/SiftStack" && .venv/bin/python src/call_coaching/pull_calls.py --list --days 7
```

Prints the duration histogram, the disposition breakdown of sub-60s recorded calls, CRM
record-link coverage, and the exact caller names (use those spellings in `roster.json`).

Cap a test run to a few cents:

```bash
cd "/Users/ricardotaveras/Desktop/SiftStack" && .venv/bin/python src/call_coaching/transcribe.py --limit 5
```

---

## When the dialer session expires

`pull_calls.py` exits 2 and names the fix. Sessions last weeks.

```bash
cd "/Users/ricardotaveras/Desktop/SiftStack" && .venv/bin/python src/call_coaching/smrtphone_login.py
```

A headed Chromium opens at `phone.smrt.studio/login`. Log in by hand; on reaching the
dashboard it writes `smrtphone_state.json` and prints `LOGGED IN`. Close the window.

Never paste dialer credentials into a chat, and never log in programmatically from stored
credentials.

---

## Reading the output

Three things matter in the run summary.

| line | what it tells you |
|---|---|
| `MEASURED cost` | actual dollars billed, not an estimate |
| `Two-pass transcription agreement` | if `diverged` climbs well past 20%, transcription quality is drifting and the gates need re-measuring |
| `QUARANTINED` | a review list, **not** a discard pile — these transcripts are untrustworthy and must never reach a rubric |

The run also reconciles: `calls_in == transcribed + skipped + errors`. If that fails it
exits 1. A silent loss shows up as arithmetic that does not add up rather than as a call
nobody misses.

---

## Baseline, measured 2026-08-18

From a 30-day window (826 calls in the log, 223 pulled, 162 transcribed after roster
exclusions):

- **Triage split:** conversation 56%, voicemail 33%, wrong number 9%, dead air 2%.
- **Yield:** 62 gradeable calls of 162 transcribed (38%).
- **Quarantined:** 24 — 17 transcription divergence, 10 unreliable speaker labels, 1
  undecided pipeline (overlapping).
- **Pipeline routing:** 80% unanimous across 3 votes; 21% of conversations genuinely
  touched two phases.
- **Cost:** $0.320/audio-hour with verification, $0.146/audio-hour without.

Duration distribution behind the `--short-floor 30` choice — real conversations
(`Correct Number`) run a **median of 117s**, and only 6 of 63 fall in the 30-59s band,
while that band holds 127 "No Answer, No VM" ring-outs. Dropping the floor to 30s
indiscriminately would nearly double the queue to recover those 6.

---

## Two flags to leave alone

`--no-verify` halves audio cost on conversations by skipping the second transcription
pass. It also removes the only real check on transcription reliability, which catches
about one gradeable call in five. Cheaper, but you would be grading fiction without
knowing which fifth.

`--short-floor` above 30 starts cutting real 30-second conversations. Do not raise it
without re-reading the `--list` histogram first.

---

## Why the gates exist

`google/gemini-2.5-flash` transcribes the **same audio differently between runs** — up to
15x variance in output length, measured on identical files. Failure runs in both
directions: truncation (a 1051s call rendered as 87 words) and runaway generation on quiet
audio (a 447s call rendered as 26,215 words). The model's self-reported confidence does not
detect this; every divergent pass called its own speaker labelling "reliable".

So two gates run, both measured from outside the model:

- **Density gate** `[1.0, 6.0]` words/sec, calibrated from the p10/p90 of a 157-call run.
- **Two-pass divergence** — word ratio < 0.50 or vocabulary overlap < 0.30 quarantines the
  call.

Do not widen either without re-measuring on ~150 calls. Consensus voting on the
*classification* stage does not substitute for this: it made a wrong verdict look
unanimous, because all three ballots read the same bad transcript.

---

## Prompt: run a coaching batch

Paste into a fresh session. Self-contained, no prior context needed. Change only the
`--days` value.

```
Run a call-coaching batch for the last 7 days.

Repo: /Users/ricardotaveras/Desktop/SiftStack (its own git repo, NOT the Rick AI vault).
Run every command from the SiftStack root with .venv/bin/python — both scripts resolve
.env, roster.json and smrtphone_state.json from the working directory, so running from
anywhere else silently reads the wrong files.

READ FIRST: src/call_coaching/RUNBOOK.md. It has the calibrated settings, the measured
baseline, and why the quality gates exist. Do not re-derive any of it.

DO THIS, in order:

1. Survey before spending anything:
     .venv/bin/python src/call_coaching/pull_calls.py --list --days 7
   If it exits 2 the dialer session expired. STOP and tell me to run
   src/call_coaching/smrtphone_login.py myself in a headed window. Never ask me to paste
   smrtPhone credentials into chat and never try to log in programmatically.

2. Pull, at the calibrated floor. Use --days 30 even for a weekly batch:
     .venv/bin/python src/call_coaching/pull_calls.py --days 30 --min-seconds 60 \
       --include-dispositions "Correct Number,No Disposition,(none)" --short-floor 30
   Free — no API cost. Report the qualifying count and any download failures.
   Do NOT narrow this to --days 7: the pull OVERWRITES call_log.json with only the window
   it fetched, and that file is what the KPI report reads, so a narrow pull silently
   destroys the rolling KPI window. Transcription skips calls it has already done, so the
   wider pull costs nothing extra.

4. Transcribe and triage:
     .venv/bin/python src/call_coaching/transcribe.py
   THIS COSTS MONEY: about $0.32 per audio-hour. A 7-day window is normally ~2 audio-hours
   (~$0.65). Before running it, tell me the audio-hours the pull produced and the implied
   cost. If it is over $3, stop and ask first. Already-transcribed calls are skipped
   automatically, so re-running after an interruption re-charges nothing.

3. Snapshot the pull — free, and do NOT skip it:
     cd "/Users/ricardotaveras/Desktop/Rick AI /Projects/kpi-bot"
     python3 snapshot_calls.py
   Merges the pull into an append-only archive. The pull overwrites call_log.json with
   only its own window, so this is the only thing preserving KPI history. Report how many
   calls were new and how many were refreshed.

5. Call KPIs — free, and independent of step 4:
     cd "/Users/ricardotaveras/Desktop/Rick AI /Projects/kpi-bot"
     python3 kpi_from_calls.py --days 7
   Reads the archive from step 3. No API cost. Report the connection rates and the
   conversation-vs-contact gap it prints, and name the source line it reports.

REPORT BACK, from the run summary — measured values only, never estimates:
- the MEASURED cost line and the audio-hours
- the triage split (conversation / voicemail / wrong_number / dead_air)
- gradeable count by pipeline, and the quarantined count with reasons
- the two-pass divergence rate
- from the KPI step: answer / conversation / meaningful / contact rates, and the
  conversation-vs-contact gap

BASELINE to compare against (measured 2026-08-18, 162 calls): conversation 56%, voicemail
33%, wrong number 9%, dead air 2%; 38% gradeable yield; 20% two-pass divergence. Flag any
material drift rather than reporting the numbers flat.

HARD RULES:
- Quarantined calls are a REVIEW LIST, not discards. Never feed one to a grading rubric and
  never "fix" one by inverting its speaker labels — the label errors are localised, so a
  global inversion corrupts the parts that were right.
- Do NOT widen the density band [1.0, 6.0] words/sec or the divergence thresholds
  (word ratio 0.50, vocab overlap 0.30). They were calibrated on 157 calls. Changing them
  needs a fresh measurement on ~150 calls, not a judgement call.
- Do NOT pass --no-verify. It halves audio cost and removes the only check on
  transcription reliability, which catches about one gradeable call in five.
- Do NOT pass --force without asking me. It re-transcribes and re-charges.
- Do NOT create any scheduled task, cron entry, or launchd job. This pipeline runs on
  command only, by design.
- Do NOT touch enrich_records.py, add_property_column.py or any export_*.py — different
  session owns those.
- Never print, log, commit or hardcode an API key or a session cookie.

If anything fails, report the actual error and stop. Do not work around a failure, and do
not tell me it worked without showing the run summary.
```
