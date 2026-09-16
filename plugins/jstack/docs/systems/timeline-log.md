# Timeline — Architecture & Usage

The timeline is the single running memory: a sqlite store of seat-tagged entries at `{timeline_dir}/timeline.db`, written and read by `bin/log_event` — the store is the only artifact, there are no rendered files. All consumers query it through the CLI: day-level readers (daily briefs, nightly reviews, dashboards) use `log_event recall`; per-seat readers (the SessionStart injector, meta reviews, validators) use `log_event tail`.

## Components

| Piece | Path | Role |
|-------|------|------|
| Writer/query CLI | `bin/log_event` | The only sanctioned writer; also serves seat queries, recalls, verdicts |
| Read-half hook | `hooks/session-start-inject.py` | Injects everything a seat's last N **sessions** wrote, on SessionStart (`timeline_inject` config) |
| User command | `skills/recall/SKILL.md` | `/jstack:recall` — date words + scope → `log_event recall` → readable outline |
| Format rule | `rules-stage/timeline.md` | Auto-loads (via `/jstack:install-rules` + path-rule-injection) when the timeline is touched — carries the format spec and editorial bar |
| Tests | `tests/log-event.sh`, `tests/timeline-injection.sh` | Hermetic CLI-contract + injection-contract verification |

## Writer contract

```bash
log_event <agent[/submode]> "<headline>" [--at HH:MM] [--date YYYY-MM-DD]
          [--detail "..."]... [--context "..."] [--origin direct|indirect]
          [--commit <sha>[@<repo>]]... [--pipeline-task <repo>#<issue>]
          [--session <sid>]
```

- **Source is the seat:** `agent/submode` (e.g. `alpha/chat`) — stored as split columns; always pass the submode when the seat is known, since seat injection filters on it. A bare `agent` source is legal but invisible to seat tails once seat-tagged entries fill the window.
- **Block shape:** `HH:MM [agent/submode]` header, one-line headline, 0–3 `- ` detail bullets — this is how `recall`/`tail` print entries.
- **Chronological reads:** queries sort by `--at`, not insert order. Late-logged events slot into place.
- **`--at`/`--date` are optional and default to now/today, machine-local** — writers logging as they go pass neither; explicit stamps are for events that happened earlier (`--date` files under a previous local day).
- **Future stamps are impossible and clamp:** a `--date` after today lands today stamped now; a same-day `--at` past the current minute (2-min grace) becomes now. Backstop for the classic writer error — HH:MM copied from a UTC transcript timestamp.
- **`--context`** is on-demand depth: stored on the entry, never injected — surfaced only by `show` and `tail --json`. Keeps the boot window lean while the entry stays self-sufficient.
- **`--origin direct|indirect`** marks who drove the session the entry came from: `direct` = a human at the wheel, `indirect` = cron/gateway/spawned. Resolution: flag > `JSTACK_TIMELINE_ORIGIN` env > direct. Spawn plumbing sets the env on unattended sessions (a bad env value falls back to direct — origin is metadata and must never block a write); pre-origin rows read back as `''`.
- **`--commit <sha>[@<repo>]`** (repeatable) links the entry to what the sitting shipped. The sha is the link; the subject is read from git and stored beside it, so `grep` can find work by what its commits said and a reader is never handed a bare hash. `<repo>` defaults to `$PWD` — `/jstack:push` passes it explicitly, since it commits with `git -C <root>` and knows which root each sha came from. Dedup is on the *resolved* sha, so a short ref and a full one are one commit. An unresolvable ref stores the sha with an empty subject and warns: the link is metadata and must never cost the entry, the same rule origin follows. Surfaces in `show`, `recall`, `tail --json` and `grep` — never in the injected boot window, which stays prose. Consolidation accumulates them (see `--pipeline-task`).
- **`--pipeline-task <tag>`** consolidates: every existing entry carrying the tag (tagged column or legacy tag-in-text) is replaced by one current entry. The tag is auto-prepended to the headline; the earliest matched timestamp is kept unless `--at` is given, and linked commits accumulate across the replacements for the same reason — a task's shipped work is the whole of it, not whatever the last session touched. One live entry per task, always current.
- **`--session <sid>`** links the entry to the transcript that produced it, and is the column tags resolve through. Falls back to `$CLAUDE_CODE_SESSION_ID` when the flag is absent, so a hand-typed write is still reachable by tag; the explicit flag wins, since a caller writing on another session's behalf knows better than its own environment. The transcript link itself is advisory — transcripts get cleaned over time; anything the future must know rides the entry itself (bullets or `--context`).
- Headlines collapse internal newlines/whitespace; details are normalized to `- ` bullets.

## Query / admin contract

```bash
log_event tail <agent[/submode]> [-n N] [--json]     # seat history, oldest→newest; bare agent = all seats
log_event tail --tag <name> [-n N] [--json]          # SEATLESS: one subject across every seat, each line naming who worked it
log_event grep "<substring>" [--seat <seat>] [--since YYYY-MM-DD] [--json]   # keyword recall: case-insensitive over headline+details+context; exit 1 = no match
log_event recall <YYYY-MM-DD[..YYYY-MM-DD]> [<seat>|all] [--full] [--json]   # date recall: a day or range replayed, optionally one seat; --full rides context blobs
log_event tail <seat> --json                         # structured: ids, session ids, origins, verdicts, context, tags
log_event show <id>                                  # everything one entry holds, incl. context (id from grep / recall / tail --json)
log_event verdict <agent[/submode]> shipped|drift|blocked|empty --note "..."
log_event tag list [--session <id>]                   # the vocabulary; inside a session, `●` marks the tags that session already carries
log_event tag show <name>|new <name> --description "..."|set <name>...|unset <name>...
log_event tag describe <name> --description "..."   # rewrite what belongs under it
log_event tag rename <old> <new>                    # filings come along; ids, not names
log_event tag delete <name> [--force]               # --force once any session carries it
```

Recall routing: a date question ("what happened Monday?") is `recall`; a keyword question ("when did we ship X?") is `grep`; either chains into `show <id>` for one entry's full depth. All are cheaper and more reliable than transcript archaeology — and they are the same mechanism as seat injection: one store, different read shapes (`tail` = last-N entries or last-N sessions per seat, `recall` = per date, `grep` = per keyword, `show` = per entry).

Seat tails also match the agent's submode-less rows (pre-seat-era migrations) so seats aren't blind right after migration; those age out of the last-N window naturally. `verdict` stamps an independent review's call on the seat's latest entry — it rides `tail`, `recall`, and injection (`↳ verdict:`).

### Asking what a seat gets injected

The injection window is not "the seat's last N rows": it counts **sessions** (`tail --sessions N` — one sitting is one unit, and a session that logged three times rides whole), `--origin direct` drops auto/cron entries (`origin=indirect` — they neither receive injections nor ride in them), and a per-dir config walk decides N. Anything that displays, checks, or audits an injection asks the injector for its own answer instead of re-deriving it:

```bash
hooks/session-start-inject.py --explain <agent[/submode]>
# {"ok": true, "seat": "alpha/social/chat", "sessions": 10, "ids": [3099, 3567, ...]}
```

`ok: false` means the answer couldn't be computed — a consumer renders that as unknown, never as "nothing injects". Liveness is deliberately not part of the answer: it reports what a person sitting down in that seat would receive, which is what a marker in a UI means. A second implementation of this window is a second truth, and the copy that isn't the injector is the one that goes stale and lies.

## Tags — the third axis

`tail` answers *which seat*, `recall` answers *when*. Neither answers *what a stretch of work was about*: one seat spans several subjects in a week, and one subject spans several seats. Tags are that axis, and they are a **relation on the session, not the entry** — a session is one sitting with one subject, so tagging each entry separately would ask the same question repeatedly and let one sitting's entries disagree about what it was. Entries reach their tag through `entries.session_id` (`tags` + `session_tags`, migrated in place like the columns).

Assignment happens at write time, by the writer that still knows what the session was — the session-end self-write and the review skill both read `tag list` and `tag set` the best match. Both scope the read with `--session <sid>` too, and not only the write: a writer running *about* another session (a self-write in a disposable dub, a review in a fresh seat) has its own session id, so a bare list reports its own filings — none — and it files a second tag beside a right one instead of seeing that the session is already filed. Two gates keep the vocabulary small enough to be worth sharing: `tag new` requires a `--description`, and `tag set` refuses a name it doesn't know rather than minting it.

`tag list` — the vocabulary a writer picks from — **ranks on breadth, not depth**: the number of distinct seats that have filed under a tag, session count only breaking a tie. A tag is by definition a subject *several sessions share across seats*, and once auto sessions tag their own work (the Stop-hook reminder, so the fleet's recurring work is no longer invisible to the vocabulary) a depth ranking would let one nightly job own the top of the list — it reuses its subject tag every run, by design, so raw session count grows that one tag without bound and buries the cross-cutting subjects a picker most needs. Breadth is immune to cadence: a job hammering one tag from its single seat counts once, however many nights it runs. The session count is still stored and shown next to the seat count; it just no longer decides the order (issue #3).

A vocabulary is only worth sharing if it can be corrected, so it is editable in place. `describe` rewrites the sentence the next writer matches against — the boundary of a subject is learned by working it, and a first guess left standing misroutes everything after it. `rename` moves a name and every session filed under it comes along, because sessions reach a tag through `tag_id` and never through its name; it is the repair for a misnamed subject. `delete` is the only one that destroys: the filings go with it and nothing remembers they existed, so a carried tag refuses without `--force` and reports the count it would unfile. Prefer renaming a wrong tag to deleting and re-minting one — the second throws away the history that made the tag worth having.

`/tag` is the same act by hand, and it **never reaches the model**: `hooks/tag-command.py` answers it at UserPromptSubmit and blocks the prompt, the way Claude Code's own `/color` takes effect without asking anything of the session. Bare answers what this session is filed under and then names the other tags, and nothing else — the use counts and descriptions belong to the question `log_event tag list` answers, and rendering them here made a one-line answer into a table. A carried name toggles off; a known name attaches; an unknown name mints only when trailing words give it a description. The answer goes out as a `decision: block` JSON payload (`hooks/_answer.py`), which is what keeps Claude Code from welding the hook's own path onto the first line and echoing the prompt underneath it; the same text on stderr with exit 2 is the fallback, and it is the exit code — not the JSON — that stops the turn. Every branch is decidable from the db, which is exactly why spending a turn on it was the wrong shape. The skill of the same name stays for the model tagging mid-turn, in light of work it has been doing. Reads (`tail`, `grep`, `recall`) take `--tag <name>` and error on an undefined one — silence there would read as "that never happened", a different and more misleading answer than "no such tag". `tag show <name>` is the cross-seat surface: one subject, every seat that touched it.

### Opening a session ON a subject

A seat can be opened on a tag instead of on itself. Export `JSTACK_TIMELINE_TAG=<name>` for the session and the injector **swaps the seat window for the subject window**: the last N sittings *any* seat had on that tag, oldest first, each line naming who worked it.

```bash
JSTACK_TIMELINE_TAG=payments claude    # the seat's terminal, the subject's history
```

Replacement, not addition. A pinned session is one sitting with one subject, and the seat's other threads are noise against it — the same seat opened on two tags is two cockpits, and the point of opening one is not to read the other. The seat still decides *where* the terminal runs, because a tag has no workspace; it stops deciding what gets read.

Two properties make the pin hold rather than decay:

- **The session tags itself**, at its first instant (`INSERT OR IGNORE`, never minting). Without it a pinned session files untagged and the *next* session on the same pin cannot see what it did.
- **An unknown tag falls back and says so.** Injection refuses a name outside the vocabulary and prints a visible note under the seat's own history, rather than booting a session blind on a subject that does not exist — the same refusal `tag set` and the `--tag` reads make, for the same reason.

Any spawner can set the variable; nothing else is required of it. A board UI that pins a (tag, seat) pair and exports it into the pane it opens is one caller of a contract that is just an env var.

An env var is also the pin's one weakness as a *readable* fact: it lives in the session's shell and nowhere else, so nothing outside that shell can tell which window a running session got. Two reads close that. `tail --json` carries each entry's `tags` — its session's subjects — so a window can be asked what its rows are filed under, not just what they say; a sitting filed under two subjects sits in both windows, and until the tags ride the read there is nothing to see that by. And `pict --session <id>` reads the injected block off the session's own transcript, where the harness records SessionStart hook output verbatim: the pin stops being inferred from ambient env and becomes the bytes the session actually received. Between them, "which window am I in, and why is that row here" is answerable from outside the session.

## The store

- Sqlite, WAL — concurrent session-ends write safely.
- Schema upgrades happen in place on connect (a pre-`context`/pre-`origin` db gains the columns on first write; a pre-tag db gains the `tags`/`session_tags` relation).
- The db is the source of truth AND the only artifact. Nothing else writes it; nothing renders from it — display surfaces (dashboards, recall outlines) query and format on read.

## Configuration

| Knob | Default | Meaning |
|------|---------|---------|
| `JSTACK_TIMELINE_DIR` | `root.timeline_dir()` — `{root}/Logs/Timeline` | Directory holding `timeline.db`. One answer for `log_event`, `msg` and the session-end engine alike |
| `JSTACK_TIMELINE_ORIGIN` | unset (→ direct) | Default origin for writes in this process tree — spawn plumbing sets `indirect` on unattended sessions |
| `JSTACK_TIMELINE_TAG` | unset (→ seat history) | Open this session **on a subject**: the injector serves the tag's cross-seat window instead of the seat's own, and the session tags itself so the thread continues (see Opening a session ON a subject) |
| `timeline_inject` (review config) | none | `{"*/*": N, "agent/submode": N, "*/submode": N}` — fleet default plus optional exact/family overrides; nearest match wins. A new seat inherits `*/*` automatically. N = **sessions** deep |

Self-contained python3 stdlib — no venv, no host imports. Safe to call from hooks, crons, spawned reviews, or interactively.

## Host delegation

A host machine may have its own in-process caller (e.g. a daemon module that logs events). It must **delegate to `bin/log_event`** for writes — a second writer implementation would fork the source of truth. Direct sqlite reads are fine (read-only). A host that wraps the binary should pin the delegation with a test (same inputs through the wrapper and the binary → identical rows in the store).

## Editorial bar (enforced by the rule, summarized)

Timeline-worthy: shipped code, live features, decisions, fixed problems, user directives that drove work, durable lessons. NOT timeline-worthy: process noise, counts, hashes, paths, session UUIDs, routine maintenance. One event = one entry; check `log_event tail` before logging; detail bullets earn their place by serving the seat's next run.
