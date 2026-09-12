---
paths:
  - "Logs/Timeline/**"
  - "**/bin/log_event"
---

# Timeline — the running memory

The timeline is the single running record of what happened, when — and each
seat's memory: a session's entries under its `agent/submode` source are
injected into that seat's next LIVE session on start (a human sitting down
mid-history). Auto sessions and injections never mix, in either direction:
headless spawns never receive an injection, and `origin=indirect` entries
(crons, publish wakes, spawned work) never ride in one — the injected view
is `tail --origin direct`, the seat's human-driven narrative. Feeds daily
briefs, nightly reviews, and every seat's cold start. **Not a session log.
Not a commit log. Not a build report.** It *links* to commits (see below) and
is still none of those: one entry per sitting, not one per change.

Store: sqlite at `{timeline_dir}/timeline.db` (default `{root}/Logs/Timeline/`,
so `~/Logs/Timeline/` on an install that declares no root) —
the ONLY artifact; there are no rendered files. Everything goes through the
jstack `log_event` tool (in the plugin's `bin/`, on PATH for review spawns):
writes AND reads. Never write the db from other code — a second writer forks
the source of truth. Timeline dir override: `JSTACK_TIMELINE_DIR`.

## Format — strict

Every entry is one block (this is also how `recall`/`tail` print it):

```
HH:MM [agent/submode]
Headline — present-tense, one line, ≤120 chars.
- optional detail
- max 3 bullets, each ≤80 chars
```

- 24h `HH:MM`. Never relative, never seconds.
- `[agent/submode]` is the lowercase seat that did the work — seats are
  directories: the session dir's full path under the agent root (e.g.
  `alpha/chat`, `delta/social/chat` — each dir its own seat). Always pass the
  submode when the seat is known — seat-tagged entries are what the next
  session of that seat boots on; a seat's tail also serves its ancestor dirs'
  rows (`social/chat` pulls `social`), never a sibling dir's; a bare
  `[agent]` entry is invisible to seat injection once the seat's own entries
  fill the window.
- Headline is one line. 0-3 detail bullets follow, each starting with `- `.

## What belongs

- Code shipped, feature live, decision made, problem fixed.
- User directive, user question that drove work, user call.
- Pipeline task state change (consolidated to one block per task).
- Significant autonomous work.
- A detail bullet earns its place when the NEXT session of the seat needs it:
  an open thread, a decision and its why, a do-not-repeat.

## What does NOT belong

- File paths, commit hashes, branch names, session UUIDs, PIDs, exit codes —
  **in the prose**. A sha belongs to the entry, but not to its sentences: it
  rides the `--commit` link (below), where a reader can follow it and a writer
  never has to spend a bullet on it.
- Test counts, line counts, build configs, device/simulator models, OS versions.
- Process noise — "pushed", "build clean", "5/5 tests pass".
- Routine maintenance — "reviewed session".
- Multiple entries for the same event from different angles.

A reader asks "what happened today?" — not "which simulator on which iOS?".

## Depth on demand — `--context`

The block stays lean — it is what every next session boots on. When an event
carries state worth more than three bullets (a design's why, an incident's
trace, the exact state of a half-done thread), put it in `--context`: stored
on the entry, never injected. It surfaces only via `log_event show <id>` or
`tail --json`. The `--session` link is advisory — transcripts get cleaned
over time; the entry plus its context must stand alone.

## Commits — what the sitting shipped

An entry and a commit are two halves of one event, and neither absorbs the
other. The **commit** says what changed, in the store that can show you the
diff. The **entry** says why the sitting happened, what it means for the next
one, and everything that shipped no code at all — a decision, a dead end, a
directive, a blocker. Link them; never restate one inside the other.

```bash
log_event {agent}/{submode} "headline" --commit {sha}            # resolves in $PWD
log_event {agent}/{submode} "headline" --commit {sha}@{repo_root} # from anywhere
```

Repeat `--commit` once per sha the sitting shipped. The subject is **read from
git**, never retyped — a hand-copied message drifts from the commit the moment
either is edited, and a link whose text disagrees with its target is worse than
a bare sha. An unresolvable sha warns and still stores; the link never costs the
entry.

Where it shows: `show`, `recall`, `tail --json`, and `grep` — searching commit
subjects along with the prose, so "when did we touch the lockout?" lands on the
entry whose commit says lockout. Where it does NOT show: the injected boot
window, which stays prose.

**Log at delivery, not only at the end.** The moment a unit of work is done and
committed is the moment its entry is cheapest and truest to write — the shas
exist, the reasoning is still loaded, and nothing has been compacted away yet. A
sitting that delivers three times leaves three entries. Waiting for session end
means one entry written from whatever survived the last boundary.

## Origin — who drove the session

Every entry carries an origin: `direct` (a human was at the wheel of the
session that produced it) or `indirect` (cron / gateway / spawned work, no
human driving). Resolution, first match wins:

1. `--origin direct|indirect` on the write
2. `JSTACK_TIMELINE_ORIGIN` env (spawn plumbing sets it on unattended
   sessions; the session-end engine sets it on self-write resumes)
3. `direct`

Interactive sessions never need the flag. Pass it only when writing on
behalf of the other kind (e.g. a human logging an event a cron performed).
Dashboards and queries filter on it — a mislabeled origin miscounts the
day's autonomous vs driven work.

## Tags — the subject, across seats

A seat answers "who did it" and a date answers "when". Neither answers
"show me everything we did on X" — one seat spans several subjects in a week,
and one subject spans several seats. The tag is that axis, and it is a
relation on the **session**, not the entry: a session is one sitting with one
subject, and entries reach their tag through `session_id`.

Whoever writes the entry assigns the tag, in the same breath:

```bash
log_event tag list                                # the whole vocabulary, with counts
log_event tag set payments --session {session_id}  # --session defaults to $CLAUDE_CODE_SESSION_ID
```

**Pick from the list; minting is the rare path.** The value of a tag is that
it means the same thing to every writer, which only holds while the list stays
short. A near match beats a new tag almost every time. When nothing on the
list plausibly covers the work:

```bash
log_event tag new <name> --description "one line: what work belongs under this"
```

Lowercase, hyphens, no spaces. A tag names a **subject** — never the seat, the
date, or a restatement of the headline. If only this one session would ever
carry it, it isn't a tag.

**Tags are siblings, never nested.** A second tag means the session did a
second thing — never that a wider subject contains the first. Work on
something that runs on the substrate is not also work on the substrate,
whatever it touched getting there: file the narrower subject and stop.
Stacking the container beside it turns that tag into a bucket, and the bucket
is what whoever opens it asked to get away from.

Reads filter with `--tag`, and that read is unbounded by seat — which is the
whole point:

```bash
log_event tag show payments --since 2026-08-01  # every seat's work on it
log_event tail alpha/chat --tag infra           # one seat, one subject
log_event grep "webhook" --tag infra
log_event recall 2026-08-01..2026-08-31 all --tag payments
```

An undefined tag is an error on every read, not an empty list — silence there
would read as "we never worked on it", which is a different answer.

A session can also be **opened on** a tag: `JSTACK_TIMELINE_TAG=<name>` swaps
the seat's injected history for that subject's, across every seat that worked
it, and the session tags itself so the thread continues. Replacement, not
addition — the seat still says where the terminal runs, and stops saying what
gets read. Any spawner can set it; a board that pins a subject per pane is
one caller.

A tag can only reach entries that carry a `session_id`, so a write with no
`--session` falls back to `$CLAUDE_CODE_SESSION_ID`. Pass `--session`
explicitly only when writing on another session's behalf.

## Headline grade

The headline reads like a news ticker. Short, declarative, present-tense.

✅ `Search v3 shipped — 8 pipeline tasks merged to v3.`
❌ `Pipeline #87 (custom boards + share flow) MERGED to v3 via manual PR after orchestrator failure. Built clean 04:25 (commits 174ff33 + 45b4640 on task/87-custom-boards: generator + profanity list + ...).`

Bullets are punchy too:
✅ `- 14 themes, daily seed rotation`
❌ `- ThemesService greedy 6×7 placer, longest-first retry shuffles, SplitMix64 seed in .../Services/...`

## Order is chronological

Stamps are machine-local wall clock, and `--at`/`--date` default to now and
today — **omit both when logging as you go** (a session-end self-write passes
neither). Pass them only for an event that happened earlier: a review writing
for an already-ended session stamps the transcript file's **mtime**; a
late-logged event from a previous local day adds `--date YYYY-MM-DD`. Never
copy a timestamp from inside a session JSONL — those are UTC and land hours
ahead (`log_event` clamps impossible future stamps to now as a backstop).

## One event, one entry

Before logging, check what's already recorded — `log_event tail <agent> -n 15`.
If the event is already covered by another block, skip; don't restate it from
your angle.

Pipeline tasks (multi-session work tracked by an issue) **must** use
`--pipeline-task {repo}#{issue}` so the new block replaces prior ones. One
live block per task, always current.

## How to write

```bash
log_event {agent}/{submode} "headline"                    # stamps now — the normal self-write
log_event {agent}/{submode} "headline" --detail "bullet" --detail "bullet"
log_event {agent}/{submode} "headline" --context "freeform depth, loaded on demand"
log_event {agent}/{submode} --pipeline-task appx#89 "headline" --detail "bullet"
log_event {agent}/{submode} --at 14:05 "event from earlier today"
log_event {agent}/{submode} --at 23:10 --date 2026-04-28 "late-logged event"
log_event {agent}/{submode} --session {session_id} "headline"   # link the transcript
log_event {agent}/{submode} --origin indirect "headline"  # writing for unattended work
log_event {agent}/{submode} "headline" --commit {sha}@{repo}    # link what it shipped
```

Agents write their own seat as source. Reserved sources (e.g. `assistant`)
belong to the system that owns them.

## Reading back — the default recall surface

"What did we do?" questions resolve here first, not by digging transcripts:
a **date** question ("what happened Monday?") → `log_event recall`; a
**keyword** question ("when did we ship X?") → `log_event grep`. Injection
and recall are the same mechanism — one store, different read shapes.

```bash
log_event tail alpha/chat -n 10     # a seat's recent history, all origins
log_event tail alpha/chat --sessions 10 --origin direct  # the seat's last 10 sittings,
                                    # human-driven only — exactly what injection shows
log_event tail alpha -n 20          # all of an agent's seats
log_event tail alpha/chat --json    # structured: ids, session ids, origins, verdicts, context
log_event recall 2026-04-28                    # a day replayed, all seats
log_event recall 2026-04-28 alpha              # one agent's day (alpha/chat = one seat)
log_event recall 2026-04-21..2026-04-27 --full # a week, context blobs included
log_event grep "publish endpoint" --seat alpha/chat --since 2026-04-01
log_event show 1234                 # everything one entry holds (id from grep/recall/tail --json)
log_event tag show payments         # one subject, every seat that touched it
```

`--tag <name>` narrows `tail`, `grep` and `recall` the same way — see **Tags**.

## Verdicts — the independent check

A reviewing process (e.g. a nightly meta review) stamps its call on a seat's
latest entry:

```bash
log_event verdict beta/pm blocked --note "do not repeat: bare re-ping; escalate format past 5 cycles"
```

Verdicts ride `tail`, `recall`, and seat injection (`↳ verdict: ...`) so the
next run sees the call.
