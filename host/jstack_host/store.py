"""The session store: one SQLite file, the long-term source for session meta.

Two kinds of rows, one sync surface:

* **Derived index** (`sessions`) — per-transcript summary fields (titles,
  first/last exchange, token totals, stamps) extracted from the JSONL under
  `~/.claude/projects`. The JSONL stays the raw truth; these rows are the
  index that makes sort/search/display instant, rebuildable from scratch.
  Kept current by an incremental tailer: a per-file byte offset means a live
  session costs one read of its appended lines, never a reparse.

* **User-authored meta** (`marks`, `session_meta`, `settings`) — facts the
  transcripts can't carry: mark definitions, which mark a session is filed
  under, app settings. This part is primary source. Devices push writes here
  and mirror the table back; the store replaces the app's CloudKit container.

Sync is a sequence cursor: every write bumps a global `seq` and stamps the
rows it touched. A client asks "everything since N" and gets exactly the
changed rows of the user-meta tables (small, mirrored whole) — sessions are
never mirrored whole to a device; they're queried on demand (`query_sessions`)
and only the hot window rides along.

Concurrency: WAL. The indexer thread is the sole writer of `sessions`; API
threads write only user-meta tables through short-lived connections. The
indexer starts from the app's startup hook, never at import.
"""

import contextlib
import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from . import compaction
from . import hostenv
from .transcripts import machine_typed_prompt

_STATE_DIR = hostenv.state_dir()
DB_PATH = _STATE_DIR / "jremote_store.sqlite"

# Text caps mirror what the cards render — the store is an index, not a copy.
_CAP_LONG = 400
_CAP_TITLE = 200

# The colors Claude Code's `/color` accepts, verbatim (its own list). A session
# records its choice as an `agent-color` line in its transcript; the same line
# is written when an active agent definition supplies the color, so what lands
# here is the session's EFFECTIVE color either way.
#
# Claude only — Codex rollouts have no equivalent. Names outside this set are
# dropped rather than carried: the transcript is an engine's file, not our
# schema, and a client that has to guess at a color name will guess wrong.
_AGENT_COLORS = frozenset(
    ("red", "blue", "green", "yellow", "purple", "orange", "pink", "cyan")
)


def normalize_agent_color(value: object) -> str:
    """A transcript's `agentColor` → a name every client can render, or "".

    "" is the whole vocabulary for "no color": `/color default` writes the
    literal "default", and a session reset that way has to read exactly like a
    session that never set one.
    """
    name = str(value or "").strip().lower()
    return name if name in _AGENT_COLORS else ""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  project_dir TEXT NOT NULL DEFAULT '',
  path TEXT NOT NULL DEFAULT '',
  agent_id TEXT NOT NULL DEFAULT '',
  sub_mode TEXT NOT NULL DEFAULT '',
  -- ISO text, not epoch floats: clients hold and compare these exact strings
  -- (paging cursors, sort keys), and a float→ISO→float round trip rounds.
  spawned TEXT NOT NULL DEFAULT '',
  last_activity TEXT NOT NULL DEFAULT '',
  first_msg TEXT NOT NULL DEFAULT '',
  first_real_user_msg TEXT NOT NULL DEFAULT '',
  last_msg TEXT NOT NULL DEFAULT '',
  last_prompt TEXT NOT NULL DEFAULT '',
  ai_title TEXT NOT NULL DEFAULT '',
  custom_title TEXT NOT NULL DEFAULT '',
  -- The session's own color, as Claude Code's `/color` recorded it. Claude
  -- only; '' means no color, which is also what a reset writes.
  agent_color TEXT NOT NULL DEFAULT '',
  slug TEXT NOT NULL DEFAULT '',
  entrypoint TEXT NOT NULL DEFAULT '',
  in_tokens INTEGER NOT NULL DEFAULT 0,
  out_tokens INTEGER NOT NULL DEFAULT 0,
  calls INTEGER NOT NULL DEFAULT 0,
  last_context INTEGER NOT NULL DEFAULT 0,
  has_queue_op INTEGER NOT NULL DEFAULT 0,
  has_telegram_meta INTEGER NOT NULL DEFAULT 0,
  byte_offset INTEGER NOT NULL DEFAULT 0,
  file_size INTEGER NOT NULL DEFAULT 0,
  mtime_ns INTEGER NOT NULL DEFAULT 0,
  last_billed_msg_id TEXT NOT NULL DEFAULT '',
  deleted INTEGER NOT NULL DEFAULT 0,
  seq INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS sessions_activity ON sessions(last_activity DESC);
CREATE INDEX IF NOT EXISTS sessions_agent ON sessions(agent_id, last_activity DESC);
CREATE INDEX IF NOT EXISTS sessions_seq ON sessions(seq);
CREATE TABLE IF NOT EXISTS marks (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  color_hex TEXT NOT NULL DEFAULT '',
  sort_index INTEGER NOT NULL DEFAULT 0,
  deleted INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL DEFAULT 0,
  seq INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS session_meta (
  session_id TEXT PRIMARY KEY,
  mark_id TEXT,
  updated_at REAL NOT NULL DEFAULT 0,
  deleted INTEGER NOT NULL DEFAULT 0,
  seq INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT '',
  updated_at REAL NOT NULL DEFAULT 0,
  seq INTEGER NOT NULL DEFAULT 0
);
-- Which seats the Agents tab shows, and what the user calls them. NOT the roster:
-- `config/agents.json` says what an agent IS and a CLAUDE.md says where a seat
-- is, both on disk, both untouched by anything here. This table only records
-- the ones the user chose to keep in front of them, so deleting a row takes the card
-- and leaves the directory — the un-pin-without-delete case it exists for.
-- Root chat seats are never rows: they come off the roster, always shown,
-- never deletable, so an empty table is exactly today's board.
--
-- A shortcut is a saved way IN: a seat, the subject to open it on (`tag` is ''
-- for the seat's own history), and `config` — how the session it launches is
-- to be spawned. One table for both kinds of card because a device asks one
-- question of them ("what ways in do I keep?") and because the answer has to
-- travel — a shortcut is the user's own filing and means the same thing on every
-- device the user owns. Which of them sit on a given Home is the device's own
-- business and lives nowhere near here.
--
-- **The id is the identity.** It used to be the pair (seat_id, tag), with a
-- twin merge tombstoning the second row onto a pair — right while a shortcut
-- said only *this way in*, and wrong the moment it carries a config: two ways
-- into one seat that spawn different engines are two different launchers, and
-- merging them deletes one the user made on purpose.
--
-- `config` is JSON and **the host never reads it**. It carries the app's launch
-- settings (engine, model, extra tags, whatever is added next) between devices
-- exactly as it carries `label` and `emoji`, and resolution of any of them
-- still happens at `/sessions/open-new`, against this Mac's own roster. A
-- column per setting would make every new one a schema change on a host that
-- has no opinion about it; a blob makes it an app-only change. Which also
-- means a build that predates a key MUST NOT drop it — see `apply_push`.
CREATE TABLE IF NOT EXISTS shortcuts (
  id TEXT PRIMARY KEY,
  seat_id TEXT NOT NULL DEFAULT '',
  tag TEXT NOT NULL DEFAULT '',
  label TEXT NOT NULL DEFAULT '',
  emoji TEXT NOT NULL DEFAULT '',
  config TEXT NOT NULL DEFAULT '',
  sort_index INTEGER NOT NULL DEFAULT 0,
  deleted INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL DEFAULT 0,
  seq INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS shortcuts_seq ON shortcuts(seq);
-- Which shortcut launched a session — the provenance the spawn is the only
-- witness to, kept so a shortcut can show its OWN sittings instead of the
-- seat's whole history.
--
-- Its own table and not a column on `sessions`, because the indexer rewrites
-- that row whole (`INSERT OR REPLACE`) from the transcript every time the file
-- grows, and a fact no transcript carries would be wiped on the next append.
-- Not the open registry either: that entry is popped at close, and a
-- shortcut's history is mostly sessions that are over.
CREATE TABLE IF NOT EXISTS session_launch (
  session_id TEXT PRIMARY KEY,
  shortcut_id TEXT NOT NULL DEFAULT '',
  opened_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS launch_shortcut ON session_launch(shortcut_id);
-- The other machines this one knows about (docs/multi-host-access.md, P3).
-- The fifth synced table, and the only one that is NOT user-authored: every
-- other row here is pushed by a device and mirrored out, while a host row is
-- written by the host itself when a machine enrols, and travels one way. That
-- is why `hosts` is absent from `SyncPush` — a device that could push one
-- could invent a machine, and the tile a user taps to hand over credentials
-- must name a machine this host actually let in.
--
-- NEVER a token, and the schema is the enforcement: there is no column one
-- could sit in. A device learns that a host EXISTS from here and asks for a
-- credential separately, so a stolen mirror yields a map and not a key.
--
-- `key` is the machine's own id as its `/host` reports it, not one minted
-- here. A registry that renamed the thing it points at would let a device
-- reach a Mac and be told it is somewhere else — the id is exactly what
-- local-first routing compares to decide whether the host on loopback is the
-- host it was configured for.
CREATE TABLE IF NOT EXISTS hosts (
  key TEXT PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  address TEXT NOT NULL DEFAULT '',
  port INTEGER NOT NULL DEFAULT 9090,
  enrolled_at INTEGER NOT NULL DEFAULT 0,
  deleted INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL DEFAULT 0,
  seq INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS hosts_seq ON hosts(seq);
-- Device identities for the API — the host's own table, and it NEVER rides
-- sync. If tokens travelled with MetaSync, revoking one device would revoke
-- all of them and every host's credentials would pool in one store
-- (docs/multi-host-access.md). `changes_since` must never learn this table.
-- token_hash is a digest, never the token: the plaintext exists only on the
-- device that was handed it at mint. revoked_at NULL = live.
--
-- `identity` is a stable, app-generated id for the PHYSICAL device (the iOS
-- app persists a UUID in the Keychain and presents it at pair time). NULL for
-- any row minted without one — the legacy token, host-internal plumbing, and
-- every device paired by an app that predates the field. When present it keys
-- the row: re-pairing the same device rotates its one row in place instead of
-- minting a second beside it (the duplicate "Laptop"/"iPad" rows this
-- column exists to stop). One row per identity is enforced by the partial
-- unique index `devices_identity`, created after the migration that adds the
-- column — see `_ensure_device_identity_index`.
CREATE TABLE IF NOT EXISTS devices (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  token_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  last_seen_at INTEGER,
  revoked_at INTEGER,
  identity TEXT,
  -- What the app last said it was ("1.0 (62)"), off the X-JRemote-Build
  -- header on any authenticated request. NULL until a build that sends it
  -- checks in. Exists so "did the fix reach the phone" is a row on the host
  -- instead of another TestFlight build cut to find out.
  client_build TEXT
);
-- One-time enrolment codes (docs/multi-host-access.md, P3). Host-only, and it
-- never syncs for the same reason `devices` never does — a code is a
-- credential-in-waiting, and a table of them pooling across hosts would let
-- one compromised store enrol devices everywhere.
--
-- code_hash, never the code: this table is what a stolen store yields, and a
-- code sitting in it in the clear is a live credential for its whole TTL.
-- Single-use is `used_at`, enforced by a conditional UPDATE rather than a
-- read-then-write, so two machines racing the same code cannot both win.
--
-- `kind` is 'device' or 'host', and it is a property of the CODE, decided by
-- whoever minted it — never of the request that redeems it. A redeemer allowed
-- to declare its own kind could register itself as a machine in the grid the
-- user reads, which is the same self-naming hole that keeps `name` on this row.
CREATE TABLE IF NOT EXISTS enrolment_codes (
  code_hash TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  created_by TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'device',
  used_at INTEGER,
  used_by TEXT
);
CREATE INDEX IF NOT EXISTS enrolment_expiry ON enrolment_codes(expires_at);
-- Delegated minting, the two halves of it (grants.py, docs/multi-host-access.md).
-- Both are host-only and NEITHER syncs, for the reason `devices` does not: these
-- are credentials, and a table of them riding MetaSync would pool every machine's
-- authority in one store.
--
-- `host_grants` is what a PARENT holds — one credential per machine it adopted,
-- issued by that machine at attach. It is the only table in this file that stores
-- a token in the clear, and it has to: a digest cannot be presented, and
-- presenting it is the entire job. The row is what makes a leaf reachable to
-- every device the parent already trusts; revoking it un-delegates that machine
-- without touching the mesh or any device row.
CREATE TABLE IF NOT EXISTS host_grants (
  host_key TEXT PRIMARY KEY,
  token TEXT NOT NULL,
  parent_url TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL DEFAULT 0,
  last_used_at INTEGER,
  revoked_at INTEGER
);
-- `parent_grants` is the reciprocal, on the LEAF — the credentials this machine
-- ISSUED to a parent, hashed like every other credential this host holds the
-- verifying end of. A grant authenticates exactly one route (`/delegate/mint`)
-- and nothing else: it is not a device row, cannot drive an agent, and cannot
-- read a session. That narrowness is the whole reason it is its own table rather
-- than a `devices` row with a flag — a flag is a thing a future route forgets to
-- check, and a separate gate cannot be forgotten into.
CREATE TABLE IF NOT EXISTS parent_grants (
  token_hash TEXT PRIMARY KEY,
  parent TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL DEFAULT 0,
  last_used_at INTEGER,
  minted INTEGER NOT NULL DEFAULT 0,
  revoked_at INTEGER
);
CREATE TABLE IF NOT EXISTS session_closes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL DEFAULT '',
  closed_at TEXT NOT NULL DEFAULT '',
  mode TEXT NOT NULL DEFAULT '',
  closed INTEGER NOT NULL DEFAULT 0,
  review INTEGER NOT NULL DEFAULT 0,
  pristine INTEGER NOT NULL DEFAULT 0,
  client TEXT NOT NULL DEFAULT '',
  user_agent TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS closes_session ON session_closes(session_id, closed_at DESC);
CREATE INDEX IF NOT EXISTS closes_at ON session_closes(closed_at DESC);
"""

# Session-row fields that sync/serve — everything except indexer bookkeeping.
_SERVE_FIELDS = (
    "session_id", "project_dir", "path", "agent_id", "sub_mode", "spawned",
    "last_activity", "first_msg", "first_real_user_msg", "last_msg",
    "last_prompt", "ai_title", "custom_title", "agent_color", "slug",
    "entrypoint",
    "in_tokens", "out_tokens", "calls", "last_context", "deleted",
)


def canonical_id(value) -> str:
    """A UUID is a 128-bit value; its spelling is presentation.

    Foundation writes `AF116D56-…`, a script writes `af116d56-…`, and a TEXT
    primary key makes those two rows — two cards on one seat, which the twin
    merge then resolves by tombstoning one of them. The app parses both
    spellings back to the same `UUID`, so it lands that tombstone on the row it
    is still showing and the card disappears. Seen end-to-end on the sim: one
    card added, one pull, gone.

    Canonical is RFC 4122's lowercase. Anything that is not a UUID is left
    exactly as it came — the column is TEXT and nothing here decides what a
    non-UUID id is allowed to mean.
    """
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return str(value)


def _projects_root() -> Path:
    """Resolved per call so a test HOME takes effect."""
    return Path.home() / ".claude" / "projects"


class SessionStore:
    """One store file. The dashboard holds a process-wide instance
    (`get_store()`); tests build their own against a tmp path."""

    def __init__(self, db_path: Path | str = DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        with self._conn() as db:
            db.executescript(_SCHEMA)
            self._add_missing_columns(db)
            self._ensure_device_identity_index(db)
            self._unmachine_prompts(db)
            self._canonicalise_shortcut_ids(db)
        # {path: (mtime_ns, size)} — what the index already reflects, so a
        # no-change tick costs stats and nothing else.
        self._known: dict[str, tuple[int, int]] = {}
        self._load_known()

    @contextlib.contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """One short-lived connection: committed (or rolled back) and CLOSED.

        `with connection:` manages only the transaction — the connection stays
        open until its object dies. On CPython 3.14 a `sqlite3.Connection` sits
        in a reference cycle from the moment it is created, so refcounting never
        frees it; it lives until the cyclic collector's next pass, holding the
        store file and its WAL open the whole time. Every call here opens one,
        so a host that leans on the garbage collector leaks two descriptors per
        store call. Under launchd's default soft limit of 256 that filled the
        table in about half an hour of a device polling the board, after which
        accept() failed with EMFILE and every client saw a dead host. Close it
        ourselves, always; the collector's timing is not a contract.
        """
        db = sqlite3.connect(self.db_path, timeout=30)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _canonicalise_shortcut_ids(db: sqlite3.Connection) -> None:
        """Fold any pre-canonical shortcut id down to `canonical_id`.

        Ingest normalises from here on, but a row written before that would sit
        beside the normalised form of itself as two cards on one seat — and a
        merge that tombstones one of them is a card vanishing off the device
        that made it. Both halves of a collision are the same card by
        definition, so the newer stamp wins and the other row goes.

        Runs on every open and is a no-op on a converged table.
        """
        rows = db.execute("SELECT id, updated_at FROM shortcuts").fetchall()
        if all(r["id"] == canonical_id(r["id"]) for r in rows):
            return
        keep: dict[str, sqlite3.Row] = {}
        for r in rows:
            want = canonical_id(r["id"])
            best = keep.get(want)
            if best is None or r["updated_at"] > best["updated_at"]:
                keep[want] = r
        for want, winner in keep.items():
            for r in rows:
                if canonical_id(r["id"]) == want and r["id"] != winner["id"]:
                    db.execute("DELETE FROM shortcuts WHERE id=?", (r["id"],))
            if winner["id"] != want:
                db.execute("UPDATE shortcuts SET id=? WHERE id=?",
                           (want, winner["id"]))

    @staticmethod
    def _add_missing_columns(db: sqlite3.Connection) -> None:
        """Bring an existing store up to `_SCHEMA`'s column set.

        `CREATE TABLE IF NOT EXISTS` is a no-op on a store that already exists,
        so a column added to the schema would never reach this Mac's 5000-row
        index — and the first INSERT naming it would fail, which is the whole
        index going dark rather than one field arriving blank.

        The wanted shape is read out of `_SCHEMA` itself, applied to a scratch
        in-memory database: the schema string stays the one place a column is
        declared, and no ALTER has to be written by hand and kept in step with
        it. Adding a column is all this does — nothing is dropped, retyped or
        backfilled, because a column that arrives empty is exactly what an
        un-refolded row honestly knows.
        """
        ref = sqlite3.connect(":memory:")
        try:
            ref.executescript(_SCHEMA)
            for table in [r[0] for r in ref.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")]:
                have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
                if not have:
                    continue
                for col in ref.execute(f"PRAGMA table_info({table})"):
                    _, name, ctype, notnull, default, _pk = col
                    if name in have:
                        continue
                    ddl = f"ALTER TABLE {table} ADD COLUMN {name} {ctype}"
                    if notnull:
                        ddl += " NOT NULL"
                    if default is not None:
                        ddl += f" DEFAULT {default}"
                    db.execute(ddl)
        finally:
            ref.close()

    @staticmethod
    def _ensure_device_identity_index(db: sqlite3.Connection) -> None:
        """One device row per physical-device `identity`.

        Deliberately NOT in `_SCHEMA` beside the other indexes: it is partial on
        a column `_add_missing_columns` may have only just added, and
        `executescript(_SCHEMA)` runs before that ALTER — a `CREATE INDEX`
        naming `identity` in the schema string would fail on every store that
        predates the column, taking the whole open down. Created here, after the
        column is guaranteed present, and `IF NOT EXISTS` so a converged DB pays
        one no-op. Partial (`WHERE identity IS NOT NULL`) because the legacy,
        host-internal and pre-field rows all carry NULL and must not collide
        with each other — only a real device identity is unique."""
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS devices_identity "
            "ON devices(identity) WHERE identity IS NOT NULL")

    @staticmethod
    def _unmachine_prompts(db: sqlite3.Connection) -> None:
        """Re-derive `last_prompt` on rows that recorded a machine-typed one.

        The fold above no longer writes `/compact`, but a row folded before it
        learned that keeps its value for good: the index is incremental, and a
        finished transcript is never re-read. So the rows are repaired in place
        — reread the file, keep the newest `last-prompt` a person typed.

        Self-clearing rather than flagged: once repaired nothing matches, so
        every later startup costs one scan of a column it already holds in
        memory and no file reads at all. `machine_typed_prompt` stays the only
        definition of what to repair — no LIKE encoding a second copy of it.

        A file since deleted, or one that turns out to carry no human prompt at
        all, clears the field — an empty exchange line beats a false one."""
        rows = db.execute(
            "SELECT path, last_prompt FROM sessions WHERE last_prompt <> ''"
        ).fetchall()
        stale = [r for r in rows if machine_typed_prompt(r["last_prompt"])]
        for r in stale:
            found = ""
            try:
                with open(r["path"], "rb") as fh:
                    for raw in fh:
                        if b'"last-prompt"' not in raw:
                            continue
                        try:
                            e = json.loads(raw)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            continue
                        if e.get("type") != "last-prompt":
                            continue
                        prompt = e.get("lastPrompt") or ""
                        if prompt and not machine_typed_prompt(prompt):
                            found = prompt[:_CAP_LONG]
            except OSError:
                pass
            db.execute("UPDATE sessions SET last_prompt=? WHERE path=?",
                       (found, r["path"]))

    def _load_known(self) -> None:
        with self._conn() as db:
            rows = db.execute(
                "SELECT path, mtime_ns, file_size, first_msg FROM sessions WHERE deleted=0"
            ).fetchall()
        self._known = {
            r["path"]: (r["mtime_ns"], r["file_size"])
            for r in rows
            if not ("/.codex/sessions/" in r["path"] and
                    str(r["first_msg"]).startswith("# AGENTS.md instructions for "))
        }

    # ── seq ──

    @staticmethod
    def _bump_seq(db: sqlite3.Connection) -> int:
        """Next global sequence number. One per transaction is enough — the
        cursor contract is strictly-greater-than."""
        db.execute(
            "INSERT INTO meta(key, value) VALUES('seq','1') "
            "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1"
        )
        return int(db.execute("SELECT value FROM meta WHERE key='seq'").fetchone()[0])

    def current_seq(self) -> int:
        with self._conn() as db:
            row = db.execute("SELECT value FROM meta WHERE key='seq'").fetchone()
        return int(row[0]) if row else 0

    # ── indexing: transcripts → sessions rows ──

    def refresh(self, limit_paths: set[str] | None = None) -> int:
        """Fold everything that changed on disk into the index. Returns the
        number of rows written. The whole scan is stats; only changed files
        are read, and only from their stored offset."""
        root = _projects_root()
        seen: set[str] = set()
        changed = 0
        for pd in root.iterdir() if root.exists() else []:
            if not pd.is_dir() or pd.name.startswith("."):
                continue
            for f in pd.glob("*.jsonl"):
                path = str(f)
                seen.add(path)
                if limit_paths is not None and path not in limit_paths:
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                if self._known.get(path) == (st.st_mtime_ns, st.st_size):
                    continue
                if self._index_file(f, st):
                    changed += 1
        # Codex writes rollouts under a date tree rather than Claude's
        # project-dir tree. They feed the same index and therefore the same
        # History, search, Conversation and Events surfaces.
        from .codex_transcript import root as codex_root
        croot = codex_root()
        for f in croot.glob("**/rollout-*.jsonl") if croot.exists() else []:
            path = str(f)
            seen.add(path)
            if limit_paths is not None and path not in limit_paths:
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            if self._known.get(path) == (st.st_mtime_ns, st.st_size):
                continue
            if self._index_file(f, st):
                changed += 1
        # Transcripts that vanished are tombstones — devices must drop them.
        gone = [p for p in self._known if p not in seen
                and (limit_paths is None or p in limit_paths)]
        if gone:
            with self._write_lock, self._conn() as db:
                seq = self._bump_seq(db)
                for path in gone:
                    db.execute(
                        "UPDATE sessions SET deleted=1, seq=? WHERE path=?",
                        (seq, path))
                    self._known.pop(path, None)
                changed += len(gone)
        return changed

    def _index_file(self, f: Path, st) -> bool:
        """Fold one transcript's new bytes into its row."""
        path = str(f)
        with self._conn() as db:
            row = db.execute(
                "SELECT * FROM sessions WHERE path=?", (path,)).fetchone()
        state = dict(row) if row else self._fresh_state(f)
        # Migration from the first Codex adapter build: boot-injected AGENTS
        # context was mistaken for the user's opening prompt and commentary was
        # counted as completed turns. Rollouts are raw truth, so rebuild the
        # affected derived row once instead of carrying poisoned summaries.
        if row and f.name.startswith("rollout-") \
                and str(row["first_msg"]).startswith("# AGENTS.md instructions for "):
            state = self._fresh_state(f)
        if f.name.startswith("rollout-"):
            try:
                from .managed import open_registry
                bound_sid = next((sid for sid, info in open_registry().items()
                                  if info.get("transcript") == path), "")
                if bound_sid:
                    state["session_id"] = bound_sid
            except Exception:
                pass
        if row and (st.st_size < row["byte_offset"]):
            # Truncated or rewritten — the offset points past EOF, so the
            # aggregates describe bytes that no longer exist. Start over.
            state = self._fresh_state(f)
        offset = state["byte_offset"]
        try:
            with open(f, "rb") as fh:
                fh.seek(offset)
                chunk = fh.read()
        except OSError:
            return False
        # Never fold a half-written line: without a trailing newline the last
        # line may still be flushing — leave it for the next pass.
        end = chunk.rfind(b"\n")
        if end < 0:
            new_offset = offset
            lines: list[bytes] = []
        else:
            lines = chunk[:end].split(b"\n")
            new_offset = offset + end + 1
        self._fold(state, lines)
        state["byte_offset"] = new_offset
        state["file_size"] = st.st_size
        state["mtime_ns"] = st.st_mtime_ns
        state["last_activity"] = _iso(st.st_mtime)
        if not state["spawned"]:
            state["spawned"] = _iso(getattr(st, "st_birthtime", st.st_mtime))
        state["deleted"] = 0
        cols = [k for k in state.keys() if k != "seq"]
        with self._write_lock, self._conn() as db:
            state["seq"] = self._bump_seq(db)
            cols.append("seq")
            placeholders = ",".join(["?"] * len(cols))
            # A Codex rollout can be indexed once under its native id before
            # the live process binder supplies jRemote's public board handle.
            # Migrate that path; never leave a duplicate history card behind.
            db.execute("DELETE FROM sessions WHERE path=? AND session_id<>?",
                       (path, state["session_id"]))
            db.execute(
                f"INSERT OR REPLACE INTO sessions ({','.join(cols)}) "
                f"VALUES ({placeholders})",
                [state[c] for c in cols])
        self._known[path] = (st.st_mtime_ns, st.st_size)
        return True

    def _fresh_state(self, f: Path) -> dict:
        agent_id, sub_mode = "", ""
        try:
            if f.name.startswith("rollout-"):
                from .codex_transcript import metadata
                meta = metadata(f)
                cwd = Path(meta.get("cwd") or "")
                from .hostenv import active_agents, workspace
                for base in active_agents():
                    root = workspace(base)
                    try:
                        rel = cwd.resolve().relative_to(root.resolve())
                    except (OSError, ValueError):
                        continue
                    agent_id = base
                    if rel.parts:
                        from .board import _display_sub_mode
                        sub_mode = _display_sub_mode("/".join(rel.parts))
                    break
                native_sid = meta.get("session_id") or meta.get("id") or ""
                # Managed Codex sessions keep jRemote's board handle as their
                # public identity. The transcript path is the exact bridge.
                from .managed import open_registry
                board_sid = next((sid for sid, info in open_registry().items()
                                  if info.get("transcript") == str(f)), "")
                sid = board_sid or native_sid
                project_dir = "codex:" + str(cwd)
            else:
                sid = f.stem
                project_dir = f.parent.name
            from .hostenv import project_dir_to_agent
            from .board import _display_sub_mode
            parsed = None if f.name.startswith("rollout-") \
                else project_dir_to_agent(f.parent.name)
            if parsed and not agent_id:
                agent_id, sub_mode = parsed[0], _display_sub_mode(parsed[1])
        except Exception:
            sid, project_dir = f.stem, f.parent.name
        return {
            "session_id": sid, "project_dir": project_dir, "path": str(f),
            "agent_id": agent_id, "sub_mode": sub_mode,
            "spawned": "", "last_activity": "",
            "first_msg": "", "first_real_user_msg": "", "last_msg": "",
            "last_prompt": "", "ai_title": "", "custom_title": "",
            "agent_color": "", "slug": "",
            "entrypoint": "", "in_tokens": 0, "out_tokens": 0, "calls": 0,
            "last_context": 0, "has_queue_op": 0, "has_telegram_meta": 0,
            "byte_offset": 0, "file_size": 0, "mtime_ns": 0,
            "last_billed_msg_id": "", "deleted": 0,
        }

    @staticmethod
    def _fold(state: dict, lines: list[bytes]) -> None:
        """Fold JSONL lines into the running summary. Same field semantics as
        the card parser this replaces: first-wins for the opening exchange,
        last-wins for titles and the last exchange, token totals billed once
        per assistant message id (each content block repeats the id and the
        usage; `last_billed_msg_id` guards the read boundary)."""
        billed: set[str] = set()
        if state["last_billed_msg_id"]:
            billed.add(state["last_billed_msg_id"])
        for raw in lines:
            if not raw.strip():
                continue
            try:
                e = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            etype = e.get("type", "")
            if etype in ("response_item", "event_msg", "session_meta"):
                SessionStore._fold_codex(state, e)
                continue
            if etype == "queue-operation":
                state["has_queue_op"] = 1
            elif etype == "ai-title":
                state["ai_title"] = (e.get("aiTitle") or "")[:_CAP_TITLE]
            elif etype == "custom-title":
                state["custom_title"] = (e.get("customTitle") or "")[:_CAP_TITLE]
            elif etype == "last-prompt":
                # Last-wins, except when the machine typed it — `/compact` is
                # otherwise the newest prompt on every heavy session.
                # See `transcripts.machine_typed_prompt`.
                prompt = e.get("lastPrompt") or ""
                if not machine_typed_prompt(prompt):
                    state["last_prompt"] = prompt[:_CAP_LONG]
            elif etype == "agent-color":
                # Last-wins like the titles beside it: Claude Code re-appends
                # this line as the session runs, and a reset writes the color
                # "default", which normalizes to no color at all.
                state["agent_color"] = normalize_agent_color(e.get("agentColor"))
            if not state["entrypoint"] and e.get("entrypoint"):
                state["entrypoint"] = str(e["entrypoint"])[:100]
            if e.get("slug"):
                state["slug"] = str(e["slug"])[:_CAP_TITLE]
            if etype == "user":
                content = (e.get("message") or {}).get("content", "")
                text = ""
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = (block.get("text") or "").strip()
                            break
                elif isinstance(content, str):
                    text = content.strip()
                if not text:
                    continue
                if not state["has_telegram_meta"] and "Conversation info" in text \
                        and "sender_id" in text:
                    state["has_telegram_meta"] = 1
                if not state["first_msg"] and not text.startswith("<"):
                    state["first_msg"] = text[:_CAP_LONG]
                if not state["first_real_user_msg"]:
                    if text.startswith("<system>") and "</system>" in text:
                        trigger = text.split("</system>", 1)[1].strip()
                        if trigger:
                            state["first_real_user_msg"] = trigger[:_CAP_LONG]
                    elif not text.startswith("<system>"):
                        state["first_real_user_msg"] = text[:_CAP_LONG]
            elif etype == "system":
                # A compaction moves the reading without writing a turn. The
                # session's overhead comes off its opening turn, which an
                # incremental fold has long since passed — so it is read back
                # off the file, at the cost of one head read per compaction.
                boundary = compaction.boundary_tokens(e)
                if boundary:
                    pre, post = boundary
                    state["last_context"] = compaction.reading_after(
                        post, compaction.first_reading(state["path"], json.loads),
                        pre)
            elif etype == "assistant":
                msg = e.get("message") or {}
                usage = msg.get("usage") or {}
                in_tok = compaction.request_tokens(usage)
                # Context-size reading, not a sum — any block of the turn.
                if in_tok > compaction.MIN_READING:
                    state["last_context"] = in_tok
                mid = msg.get("id") or ""
                if not mid or mid not in billed:
                    if mid:
                        billed.add(mid)
                        state["last_billed_msg_id"] = mid
                    state["in_tokens"] += in_tok
                    state["out_tokens"] += usage.get("output_tokens", 0)
                    state["calls"] += 1
                content = msg.get("content", "")
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text" \
                                and (c.get("text") or "").strip():
                            state["last_msg"] = c["text"].strip()[:_CAP_LONG]
                elif isinstance(content, str) and content.strip():
                    state["last_msg"] = content.strip()[:_CAP_LONG]

    @staticmethod
    def _fold_codex(state: dict, e: dict) -> None:
        """Fold one Codex rollout record into the shared summary fields."""
        payload = e.get("payload") or {}
        if e.get("type") == "session_meta":
            state["entrypoint"] = str(payload.get("source") or "codex")[:100]
            return
        if e.get("type") == "event_msg" and payload.get("type") == "token_count":
            usage = ((payload.get("info") or {}).get("last_token_usage") or {})
            state["last_context"] = int(usage.get("input_tokens") or 0)
            return
        if e.get("type") != "response_item":
            return
        if payload.get("type") != "message":
            return
        role = payload.get("role")
        texts = [str(b.get("text") or "").strip()
                 for b in payload.get("content") or [] if isinstance(b, dict)
                 and b.get("type") in ("input_text", "output_text")
                 and str(b.get("text") or "").strip()]
        if not texts:
            return
        text = "\n".join(texts)
        if role == "user":
            # Strip Codex's inlined attachment markup first: a prompt opening
            # with a screenshot is still the user's opening prompt, and judging it
            # by its leading '<' would hand the card's title to their next one.
            from .codex_transcript import strip_attachments
            text = strip_attachments(text)
            if not text or text.startswith(("<", "# AGENTS.md instructions for ")):
                return
            if not state["first_msg"]:
                state["first_msg"] = text[:_CAP_LONG]
            if not state["first_real_user_msg"]:
                state["first_real_user_msg"] = text[:_CAP_LONG]
            state["last_prompt"] = text[:_CAP_LONG]
        elif role == "assistant":
            state["last_msg"] = text[:_CAP_LONG]
            if payload.get("phase") == "final_answer":
                state["calls"] += 1

    # ── serving: on-demand session queries ──

    def query_sessions(self, agent: str | None = None, q: str = "",
                       before: str = "", limit: int = 50,
                       include_deleted: bool = False) -> list[dict]:
        """Paged, newest-first session index — the on-demand path. `agent`
        matches base id ('ops') or seat id ('ops-chat'); `q` searches
        every title/exchange field; `before` (ISO stamp) pages past the last
        row the caller holds."""
        where, args = [], []
        if not include_deleted:
            where.append("deleted=0")
        if agent:
            base, _, mode = agent.partition("-")
            where.append("agent_id=?")
            args.append(base.lower())
            if mode:
                # Stored sub_mode is display form (chat/remote); a seat id
                # arrives hyphenated — normalize with the board's own rule.
                from .board import _display_sub_mode
                where.append("sub_mode=?")
                args.append(_display_sub_mode(mode))
        if q:
            like = f"%{q}%"
            fields = ("custom_title", "ai_title", "first_msg",
                      "first_real_user_msg", "last_msg", "last_prompt", "slug")
            where.append("(" + " OR ".join(f"{f} LIKE ?" for f in fields) + ")")
            args.extend([like] * len(fields))
        if before:
            where.append("last_activity < ?")
            args.append(before)
        sql = (f"SELECT {','.join(_SERVE_FIELDS)} FROM sessions "
               + ("WHERE " + " AND ".join(where) if where else "")
               + " ORDER BY last_activity DESC LIMIT ?")
        args.append(max(1, min(int(limit), 500)))
        with self._conn() as db:
            rows = db.execute(sql, args).fetchall()
        return [_serve_session(dict(r)) for r in rows]

    def session_colors(self) -> dict[str, str]:
        """{session_id: color} for every session that has one — sids absent
        from the map have no color, which is nearly all of them.

        The store is the ONE reader of `agent-color`, and this is how the
        board gets it rather than parsing for itself. Both payloads describe
        the same session and land on the same record on the device, so a
        second opinion here is not redundancy — it is a field that flickers.
        And the opinions would genuinely differ: the board's summary parser
        samples a large transcript (head + tail) while this index folds every
        line it was ever appended, so a color set early in a long session is
        exactly what sampling drops.
        """
        with self._conn() as db:
            rows = db.execute(
                "SELECT session_id, agent_color FROM sessions "
                "WHERE agent_color <> '' AND deleted=0").fetchall()
        return {r["session_id"]: r["agent_color"] for r in rows}

    def transcript_path(self, session_id: str) -> str:
        """The file this session's row was folded from — '' when unindexed.

        The durable half of the sid→transcript binding. A *managed* Codex
        session's public id is jRemote's board handle, a name the rollout file
        does not carry and the open registry forgets the instant the session
        ends; without this the card outlives the only thing that could open
        it. Every History row is served from this table, so answering "which
        file is that card?" from the same table is what keeps the two agreeing.
        """
        with self._conn() as db:
            row = db.execute(
                "SELECT path FROM sessions WHERE session_id=? "
                "ORDER BY deleted, last_activity DESC LIMIT 1",
                (session_id,)).fetchone()
        return str(row["path"]) if row else ""

    # ── session closes: every end of a session, and who asked ──
    #
    # Rows here are neither derived from a transcript nor authored on a
    # device: a close is a host event about a session, so it lands beside the
    # session it ended rather than in a file of its own. Written by API
    # threads through short-lived connections, the same as the meta tables —
    # the indexer stays the sole writer of `sessions`.

    def record_close(self, session_id: str, mode: str, closed: bool,
                     review: bool, pristine: bool,
                     client: str = "", user_agent: str = "") -> None:
        """Append one close attempt, including the ones that changed nothing.

        No seat column: the session's own row already carries agent_id and
        sub_mode, and `recent_closes` joins for them. A copy here would be a
        second thing to keep true."""
        with self._write_lock, self._conn() as db:
            db.execute(
                "INSERT INTO session_closes (session_id, closed_at, mode, closed,"
                " review, pristine, client, user_agent) VALUES (?,?,?,?,?,?,?,?)",
                (session_id, datetime.now().isoformat(timespec="seconds"), mode,
                 1 if closed else 0, 1 if review else 0, 1 if pristine else 0,
                 client[:64], user_agent[:200]))

    def recent_closes(self, session_id: str = "", limit: int = 50) -> list[dict]:
        """Newest-first close history, seat resolved from the session index.

        LEFT JOIN, not INNER: a `pid-` row is a bare running process with no
        session identity, so it never joins and never will — and a SIGKILL
        into a live agent CLI is exactly what this table exists to name. Every
        real sid joins: the index holds a row for every transcript on disk,
        and a session with no transcript is not a session."""
        sql = ("SELECT c.*, COALESCE(s.agent_id, '') AS agent_id, "
               "COALESCE(s.sub_mode, '') AS sub_mode FROM session_closes c "
               "LEFT JOIN sessions s ON s.session_id = c.session_id ")
        args: list = []
        if session_id:
            sql += "WHERE c.session_id = ? "
            args.append(session_id)
        sql += "ORDER BY c.closed_at DESC, c.id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        with self._conn() as db:
            rows = db.execute(sql, args).fetchall()
        return [{**dict(r), "closed": bool(r["closed"]),
                 "review": bool(r["review"]), "pristine": bool(r["pristine"])}
                for r in rows]

    # ── devices: per-device API credentials ──
    #
    # Host-only rows — deliberately absent from `changes_since`, and they must
    # stay absent (see the schema comment). Written by API threads through
    # short-lived connections, same as the meta tables. No seq column: these
    # rows never travel, so nothing needs a cursor over them.

    def count_devices(self) -> int:
        """Every row, revoked included — 'has this host ever migrated' is a
        question about the table existing, not about who is currently live."""
        with self._conn() as db:
            return int(db.execute("SELECT COUNT(*) FROM devices").fetchone()[0])

    def device(self, device_id: str) -> dict | None:
        with self._conn() as db:
            row = db.execute("SELECT * FROM devices WHERE id=?",
                             (device_id,)).fetchone()
        return dict(row) if row else None

    def list_devices(self) -> list[dict]:
        with self._conn() as db:
            rows = db.execute(
                "SELECT * FROM devices ORDER BY created_at, id").fetchall()
        return [dict(r) for r in rows]

    def add_device(self, device_id: str, name: str, token_hash: str) -> bool:
        """Insert a device, never replace one. False = the id already exists —
        an INSERT that overwrote would let a colliding mint silently take over
        an existing device's identity."""
        with self._write_lock, self._conn() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO devices (id, name, token_hash, created_at) "
                "VALUES (?,?,?,?)",
                (device_id, name, token_hash, int(time.time())))
            return cur.rowcount > 0

    def upsert_device_identity(self, device_id: str, name: str,
                               token_hash: str, identity: str) -> str | None:
        """Rotate the row already keyed to `identity`, or mint a fresh one under
        it — the re-pairing path.

        Returns the id of the row that now carries `identity`: the EXISTING
        row's id when one was found (same physical device pairing again — new
        `token_hash`, `name` refreshed, `revoked_at` cleared so a revoked device
        that re-pairs is revived in place, never a second row), or the supplied
        `device_id` when a new row was inserted. None means `device_id` collided
        with a live primary key on the insert path, and the caller retries with
        a fresh id exactly as the no-identity mint loop does.

        Unlike `add_device` this is an UPDATE-or-INSERT, not an INSERT-or-ignore:
        re-keying the matched row in place is the whole point. The SELECT and the
        write share one transaction under `_write_lock`, and the partial unique
        index `devices_identity` is the cross-process backstop, so two pairings
        of one device racing here cannot leave two rows behind."""
        with self._write_lock, self._conn() as db:
            row = db.execute(
                "SELECT id FROM devices WHERE identity=?", (identity,)).fetchone()
            if row is not None:
                db.execute(
                    "UPDATE devices SET token_hash=?, name=?, revoked_at=NULL "
                    "WHERE id=?", (token_hash, name, row["id"]))
                return row["id"]
            try:
                db.execute(
                    "INSERT INTO devices "
                    "(id, name, token_hash, created_at, identity) "
                    "VALUES (?,?,?,?,?)",
                    (device_id, name, token_hash, int(time.time()), identity))
            except sqlite3.IntegrityError:
                return None  # id collision — the caller mints a new id
            return device_id

    def set_device_hash(self, device_id: str, token_hash: str) -> bool:
        """Re-key a LIVE device (the host re-minting its own internal
        credential after losing the plaintext). Refuses revoked rows: a re-key
        that resurrected one would be revocation quietly undone."""
        with self._write_lock, self._conn() as db:
            cur = db.execute(
                "UPDATE devices SET token_hash=? "
                "WHERE id=? AND revoked_at IS NULL", (token_hash, device_id))
            return cur.rowcount > 0

    def rename_device(self, device_id: str, name: str) -> bool:
        with self._write_lock, self._conn() as db:
            cur = db.execute("UPDATE devices SET name=? WHERE id=?",
                             (name, device_id))
            return cur.rowcount > 0

    def revoke_device(self, device_id: str) -> bool:
        """Stamp revoked_at on a live row. False = unknown or already revoked
        (idempotent — a second tap on Revoke is not an error)."""
        with self._write_lock, self._conn() as db:
            cur = db.execute(
                "UPDATE devices SET revoked_at=? "
                "WHERE id=? AND revoked_at IS NULL",
                (int(time.time()), device_id))
            return cur.rowcount > 0

    def delete_device(self, device_id: str) -> bool:
        """Remove the row outright. False = there was no such row.

        Revoking stamps a row and keeps it forever, which is the right default
        for a device somebody actually paired — the roster should be able to
        say "this iPad was here and is not trusted now". It is the wrong and
        only answer for a row that should never have existed: a test that
        paired against the live registry, a name typed wrong, a daemon row from
        a migration. Those accumulate at the top of the one screen a person
        uses to check who can reach their Mac, and no amount of revoking clears
        them.

        The contract on every caller is that the row is ALREADY revoked, so
        this can never be the thing that cuts a live session's trust out from
        under it. Nothing calls it yet — it is the storage half of clearing
        the permanent revoked rows, landed on its own so the route that
        exposes it starts from a primitive that is already tested.
        """
        with self._write_lock, self._conn() as db:
            cur = db.execute("DELETE FROM devices WHERE id=?", (device_id,))
            return cur.rowcount > 0

    def touch_device(self, device_id: str) -> None:
        with self._write_lock, self._conn() as db:
            db.execute("UPDATE devices SET last_seen_at=? WHERE id=?",
                       (int(time.time()), device_id))

    def note_device_build(self, device_id: str, build: str) -> None:
        """Record what the client says it is. Guarded by the caller's cache —
        the value changes once per app update, not once per request."""
        with self._write_lock, self._conn() as db:
            db.execute("UPDATE devices SET client_build=? WHERE id=?",
                       (build, device_id))

    # ── enrolment codes: credentials-in-waiting, also host-only ──

    def add_enrolment_code(self, code_hash: str, name: str, expires_at: int,
                           created_by: str, kind: str = "device") -> bool:
        with self._write_lock, self._conn() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO enrolment_codes "
                "(code_hash, name, created_at, expires_at, created_by, kind) "
                "VALUES (?,?,?,?,?,?)",
                (code_hash, name, int(time.time()), int(expires_at),
                 created_by, kind))
            return cur.rowcount > 0

    def enrolment_code(self, code_hash: str) -> dict | None:
        with self._conn() as db:
            row = db.execute("SELECT * FROM enrolment_codes WHERE code_hash=?",
                             (code_hash,)).fetchone()
        return dict(row) if row else None

    def list_enrolment_codes(self) -> list[dict]:
        with self._conn() as db:
            rows = db.execute(
                "SELECT * FROM enrolment_codes ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def consume_enrolment_code(self, code_hash: str, used_by: str,
                               now: int) -> bool:
        """Claim a code for one enrolment. True exactly once, ever.

        The unused-and-unexpired test lives INSIDE the UPDATE. Read-then-write
        would let two machines redeeming the same code in the same instant both
        pass the read and both mint — which is the one thing single-use has to
        prevent, and the window is widest exactly when someone has just read a
        code aloud to two people.
        """
        with self._write_lock, self._conn() as db:
            cur = db.execute(
                "UPDATE enrolment_codes SET used_at=?, used_by=? "
                "WHERE code_hash=? AND used_at IS NULL AND expires_at > ?",
                (now, used_by, code_hash, now))
            return cur.rowcount > 0

    def note_enrolment_device(self, code_hash: str, used_by: str) -> bool:
        """Name the device a claimed code produced.

        `consume` has to run before the device exists — single-use is the thing
        it protects, and it cannot wait on a mint — so it can only write the
        address it was redeemed from. This closes the record afterwards. Only a
        claimed row is touched: an unused code has no device to name, and
        writing one would forge the audit trail this column exists to be.
        """
        with self._write_lock, self._conn() as db:
            cur = db.execute(
                "UPDATE enrolment_codes SET used_by=? "
                "WHERE code_hash=? AND used_at IS NOT NULL",
                (used_by, code_hash))
            return cur.rowcount > 0

    def revoke_enrolment_code(self, code_hash: str) -> bool:
        """Drop an unused code. A used row stays — it is the audit trail of
        which device this host let in, and deleting it would erase that."""
        with self._write_lock, self._conn() as db:
            cur = db.execute(
                "DELETE FROM enrolment_codes WHERE code_hash=? AND used_at IS NULL",
                (code_hash,))
            return cur.rowcount > 0

    def sweep_enrolment_codes(self, before: int) -> int:
        """Delete expired codes that were never used. Housekeeping only —
        expiry is enforced at redemption, so a code this misses is still dead."""
        with self._write_lock, self._conn() as db:
            cur = db.execute(
                "DELETE FROM enrolment_codes WHERE used_at IS NULL AND expires_at <= ?",
                (before,))
            return cur.rowcount

    # ── sync: user-authored meta ──

    def changes_since(self, since: int) -> dict:
        """Everything a device mirror needs past its cursor: the changed
        user-meta rows (mirrored whole) and the current seq. Sessions ride
        the on-demand path, never this one."""
        with self._conn() as db:
            marks = db.execute(
                "SELECT id, name, color_hex, sort_index, deleted, updated_at, seq "
                "FROM marks WHERE seq > ?", (since,)).fetchall()
            metas = db.execute(
                "SELECT session_id, mark_id, updated_at, deleted, seq "
                "FROM session_meta WHERE seq > ?", (since,)).fetchall()
            settings = db.execute(
                "SELECT key, value, updated_at, seq FROM settings WHERE seq > ?",
                (since,)).fetchall()
            shortcuts = db.execute(
                "SELECT id, seat_id, tag, label, emoji, config, sort_index, "
                "deleted, updated_at, seq FROM shortcuts WHERE seq > ?",
                (since,)).fetchall()
            # The one table here the host writes and the device only reads.
            # Adding it was a wire change: a build whose cursor is already past
            # these rows would never see them, which is what the app's table
            # epoch exists to reset (MetaSync.tableEpoch).
            hosts = db.execute(
                "SELECT key, name, address, port, enrolled_at, deleted, "
                "updated_at, seq FROM hosts WHERE seq > ?", (since,)).fetchall()
        return {
            "seq": self.current_seq(),
            "marks": [dict(r) for r in marks],
            "session_meta": [dict(r) for r in metas],
            "settings": [dict(r) for r in settings],
            "shortcuts": [dict(r) for r in shortcuts],
            "hosts": [dict(r) for r in hosts],
        }

    # ── hosts: the machines this one has let in ──
    #
    # Written here and nowhere else on the device's behalf — `apply_push` has no
    # branch for this table and must never grow one (see the schema comment).
    # Every writer stamps a seq, because a row that changed without one is a row
    # no device will ever pull.

    def upsert_host(self, key: str, name: str, address: str,
                    port: int = 9090) -> dict:
        """Record a machine this host enrolled, or refresh what it knows.

        `enrolled_at` is restamped on a re-enrolment rather than preserved: the
        row answers "when was this machine last granted access", and a forgotten
        machine that comes back was granted access again. The same write clears
        `deleted`, so re-enrolling is how a forgotten host returns.
        """
        now = time.time()
        with self._write_lock, self._conn() as db:
            seq = self._bump_seq(db)
            db.execute(
                "INSERT INTO hosts (key, name, address, port, enrolled_at, "
                "deleted, updated_at, seq) VALUES (?,?,?,?,?,0,?,?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "name=excluded.name, address=excluded.address, "
                "port=excluded.port, enrolled_at=excluded.enrolled_at, "
                "deleted=0, updated_at=excluded.updated_at, seq=excluded.seq",
                (key, name, address, int(port), int(now), now, seq))
            row = db.execute("SELECT * FROM hosts WHERE key=?", (key,)).fetchone()
        return dict(row)

    def host_row(self, key: str) -> dict | None:
        with self._conn() as db:
            row = db.execute("SELECT * FROM hosts WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    def list_hosts(self, include_forgotten: bool = False) -> list[dict]:
        """The machines this one knows about. Tombstones stay in the table for
        the mirror's sake and are filtered here, the way `shortcuts()` does."""
        sql = "SELECT * FROM hosts"
        if not include_forgotten:
            sql += " WHERE deleted=0"
        with self._conn() as db:
            return [dict(r) for r in db.execute(sql + " ORDER BY name, key")]

    def rename_host(self, key: str, name: str) -> bool:
        """Rename a machine in the grid. Refuses a forgotten row: renaming one
        would resurrect it on every device without granting anything."""
        with self._write_lock, self._conn() as db:
            seq = self._bump_seq(db)
            cur = db.execute(
                "UPDATE hosts SET name=?, updated_at=?, seq=? "
                "WHERE key=? AND deleted=0", (name, time.time(), seq, key))
            return cur.rowcount > 0

    def forget_host(self, key: str) -> bool:
        """Tombstone, never DELETE. A row that vanished would be a row every
        device already past its seq keeps drawing forever — the tombstone is
        the only thing that travels."""
        with self._write_lock, self._conn() as db:
            seq = self._bump_seq(db)
            cur = db.execute(
                "UPDATE hosts SET deleted=1, updated_at=?, seq=? "
                "WHERE key=? AND deleted=0", (time.time(), seq, key))
            return cur.rowcount > 0

    # ── grants: the two ends of delegated minting ──
    #
    # Neither table is in `changes_since` and neither may ever be. `hosts` tells
    # a device a machine EXISTS; these two are what let it get in, and the split
    # is the point (see the schema comments).

    def put_host_grant(self, host_key: str, token: str,
                       parent_url: str = "") -> None:
        """Hold the credential a machine issued this one at attach.

        Replaces on conflict rather than accumulating: a machine re-attaching
        issues a fresh grant and the old one is dead the moment it does, so a
        second row could only ever be a credential nothing can spend.
        """
        now = int(time.time())
        with self._write_lock, self._conn() as db:
            db.execute(
                "INSERT INTO host_grants (host_key, token, parent_url, "
                "created_at, revoked_at) VALUES (?,?,?,?,NULL) "
                "ON CONFLICT(host_key) DO UPDATE SET "
                "token=excluded.token, parent_url=excluded.parent_url, "
                "created_at=excluded.created_at, revoked_at=NULL",
                (host_key, token, parent_url, now))

    def host_grant(self, host_key: str) -> dict | None:
        """The live grant for `host_key`, or None. Revoked reads as absent —
        every caller wants "can I mint there", and a revoked row cannot."""
        with self._conn() as db:
            row = db.execute(
                "SELECT * FROM host_grants WHERE host_key=? AND revoked_at IS NULL",
                (host_key,)).fetchone()
        return dict(row) if row else None

    def list_host_grants(self) -> list[dict]:
        """Every grant this host holds, revoked ones included — the roster a
        person reads to see which machines it can hand out access to. The token
        is dropped here: nothing that lists needs it, and a listing that carries
        credentials is one log line from leaking them."""
        with self._conn() as db:
            rows = db.execute(
                "SELECT host_key, parent_url, created_at, last_used_at, "
                "revoked_at FROM host_grants ORDER BY host_key").fetchall()
        return [dict(r) for r in rows]

    def note_host_grant_used(self, host_key: str) -> None:
        with self._write_lock, self._conn() as db:
            db.execute("UPDATE host_grants SET last_used_at=? WHERE host_key=?",
                       (int(time.time()), host_key))

    def revoke_host_grant(self, host_key: str) -> bool:
        """Stop being able to mint on that machine. Local only — the credential
        stays live on the machine that issued it until IT revokes, which is the
        honest shape: authority is revoked where it is verified."""
        with self._write_lock, self._conn() as db:
            cur = db.execute(
                "UPDATE host_grants SET revoked_at=? "
                "WHERE host_key=? AND revoked_at IS NULL",
                (int(time.time()), host_key))
            return cur.rowcount > 0

    def put_parent_grant(self, token_hash: str, parent: str) -> None:
        """Record a credential this machine just issued to a parent."""
        with self._write_lock, self._conn() as db:
            db.execute(
                "INSERT OR REPLACE INTO parent_grants (token_hash, parent, "
                "created_at, minted, revoked_at) VALUES (?,?,?,0,NULL)",
                (token_hash, parent, int(time.time())))

    def parent_grant(self, token_hash: str) -> dict | None:
        with self._conn() as db:
            row = db.execute(
                "SELECT * FROM parent_grants WHERE token_hash=? "
                "AND revoked_at IS NULL", (token_hash,)).fetchone()
        return dict(row) if row else None

    def note_parent_grant_used(self, token_hash: str) -> None:
        """Stamp a use and count the mint. `minted` is the number that makes an
        abused grant visible — a parent legitimately mints once per device, so a
        row in the hundreds is a fact worth being able to read."""
        with self._write_lock, self._conn() as db:
            db.execute(
                "UPDATE parent_grants SET last_used_at=?, minted=minted+1 "
                "WHERE token_hash=?", (int(time.time()), token_hash))

    def list_parent_grants(self) -> list[dict]:
        with self._conn() as db:
            rows = db.execute(
                "SELECT token_hash, parent, created_at, last_used_at, minted, "
                "revoked_at FROM parent_grants ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def revoke_parent_grants(self, parent: str = "") -> int:
        """Revoke the grants this machine issued — one parent's, or all of them
        when `parent` is empty. Detaching takes the second form: a machine that
        left a mesh has not authorized anybody there to mint on it."""
        sql = ("UPDATE parent_grants SET revoked_at=? WHERE revoked_at IS NULL"
               + (" AND parent=?" if parent else ""))
        params = (int(time.time()),) + ((parent,) if parent else ())
        with self._write_lock, self._conn() as db:
            return db.execute(sql, params).rowcount

    def shortcuts(self) -> list[dict]:
        """The live shortcuts, in the order they are drawn. Tombstones stay in
        the table for the devices that have not heard about them yet; nobody
        reading the board wants to see one.

        Subject shortcuts are in here too, and they name the same seats the
        plain cards do — every caller that wants seats out of this has to
        de-dupe on `seat_id` (`board.carded_seats` does). Two rows may now name
        the same (seat, tag) as well: since the id is the identity, they are two
        launchers onto one door, and de-duping is the caller's job either way."""
        with self._conn() as db:
            rows = db.execute(
                "SELECT id, seat_id, tag, label, emoji, config, sort_index, "
                "updated_at FROM shortcuts WHERE deleted=0 "
                "ORDER BY sort_index, seat_id, tag"
            ).fetchall()
        return [dict(r) for r in rows]

    def record_launch(self, session_id: str, shortcut_id: str) -> None:
        """Remember which shortcut spawned this session. Called once, by the
        spawn — the only moment anything knows."""
        if not session_id or not shortcut_id:
            return
        with self._write_lock, self._conn() as db:
            db.execute(
                "INSERT INTO session_launch (session_id, shortcut_id, opened_at) "
                "VALUES (?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
                "shortcut_id=excluded.shortcut_id, opened_at=excluded.opened_at",
                (session_id, canonical_id(shortcut_id), time.time()))

    def launched_by(self, shortcut_id: str) -> set[str]:
        """The sessions one shortcut has opened. A set, because it is used to
        filter a session list the board built for other reasons."""
        if not shortcut_id:
            return set()
        with self._conn() as db:
            rows = db.execute(
                "SELECT session_id FROM session_launch WHERE shortcut_id=?",
                (canonical_id(shortcut_id),)).fetchall()
        return {r["session_id"] for r in rows}

    def launch_shortcuts(self) -> dict[str, str]:
        """{session_id: shortcut_id} for every session a shortcut opened —
        one read, because the board stamps a whole page of rows at once."""
        with self._conn() as db:
            rows = db.execute(
                "SELECT session_id, shortcut_id FROM session_launch").fetchall()
        return {r["session_id"]: r["shortcut_id"] for r in rows}

    def apply_push(self, payload: dict) -> int:
        """Land a device's user-meta writes. Upsert by natural key, newest
        `updated_at` wins (a stale push loses silently — the loser's device
        converges on the next pull). After marks land, exact (name, color)
        twins merge — two devices that each seeded or created the same mark
        must converge on one, with filings repointed. Returns the new seq."""
        now = time.time()
        with self._write_lock, self._conn() as db:
            seq = self._bump_seq(db)
            for m in payload.get("marks") or []:
                if not m.get("id"):
                    continue
                ts = float(m.get("updated_at") or now)
                db.execute(
                    "INSERT INTO marks (id, name, color_hex, sort_index, deleted, updated_at, seq) "
                    "VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                    "name=excluded.name, color_hex=excluded.color_hex, "
                    "sort_index=excluded.sort_index, deleted=excluded.deleted, "
                    "updated_at=excluded.updated_at, seq=excluded.seq "
                    "WHERE excluded.updated_at > marks.updated_at",
                    (str(m["id"]), str(m.get("name") or ""),
                     str(m.get("color_hex") or ""), int(m.get("sort_index") or 0),
                     1 if m.get("deleted") else 0, ts, seq))
            for sm in payload.get("session_meta") or []:
                if not sm.get("session_id"):
                    continue
                ts = float(sm.get("updated_at") or now)
                db.execute(
                    "INSERT INTO session_meta (session_id, mark_id, updated_at, deleted, seq) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
                    "mark_id=excluded.mark_id, updated_at=excluded.updated_at, "
                    "deleted=excluded.deleted, seq=excluded.seq "
                    "WHERE excluded.updated_at > session_meta.updated_at",
                    (str(sm["session_id"]),
                     str(sm["mark_id"]) if sm.get("mark_id") else None,
                     ts, 1 if sm.get("deleted") else 0, seq))
            raw = payload.get("settings") or []
            if isinstance(raw, dict):   # convenience form — stamped on arrival
                raw = [{"key": k, "value": v} for k, v in raw.items()]
            for s in raw:
                if not s.get("key"):
                    continue
                ts = float(s.get("updated_at") or now)
                db.execute(
                    "INSERT INTO settings (key, value, updated_at, seq) "
                    "VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                    "value=excluded.value, updated_at=excluded.updated_at, "
                    "seq=excluded.seq "
                    "WHERE excluded.updated_at > settings.updated_at",
                    (str(s["key"]), json.dumps(s.get("value")), ts, seq))
            held_config = self._shortcut_configs(db)
            for sc in payload.get("shortcuts") or []:
                if not sc.get("id") or not sc.get("seat_id"):
                    continue
                ts = float(sc.get("updated_at") or now)
                sid_ = canonical_id(sc["id"])
                # An ABSENT `config` means "I have nothing to say about it",
                # never "clear it". Every device pushes its whole shortcut
                # table on every sync, so a build that predates the column —
                # or predates one key inside it — pushes rows without it, and a
                # plain `excluded.config` would wipe the user's engine choice off
                # every card the moment an older phone synced. Present-and-empty
                # is still a real value and does clear.
                config = (str(sc.get("config") or "") if "config" in sc
                          else held_config.get(sid_, ""))
                db.execute(
                    "INSERT INTO shortcuts (id, seat_id, tag, label, emoji, "
                    "config, sort_index, deleted, updated_at, seq) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "seat_id=excluded.seat_id, tag=excluded.tag, "
                    "label=excluded.label, "
                    "emoji=excluded.emoji, config=excluded.config, "
                    "sort_index=excluded.sort_index, "
                    "deleted=excluded.deleted, updated_at=excluded.updated_at, "
                    "seq=excluded.seq "
                    "WHERE excluded.updated_at > shortcuts.updated_at",
                    (sid_, str(sc["seat_id"]),
                     str(sc.get("tag") or ""), str(sc.get("label") or ""),
                     str(sc.get("emoji") or ""), config,
                     int(sc.get("sort_index") or 0),
                     1 if sc.get("deleted") else 0, ts, seq))
            self._merge_twin_marks(db, seq, now)
        return self.current_seq()

    @staticmethod
    def _shortcut_configs(db: sqlite3.Connection) -> dict[str, str]:
        """{id: config} as held right now — what an absent `config` falls back
        to. Read once per push, not per row."""
        return {r["id"]: r["config"] or ""
                for r in db.execute("SELECT id, config FROM shortcuts")}

    @staticmethod
    def _merge_twin_marks(db: sqlite3.Connection, seq: int, now: float) -> None:
        """An exact (name, color_hex) twin is a double-seed, not a choice —
        keep the lowest id, repoint filings at it, tombstone the rest.

        Every row the merge rewrites gets a fresh `updated_at`. A device lands
        a delta row only when its stamp beats the one it already holds, so a
        tombstone carrying the loser's own creation stamp is a tombstone the
        device that authored the loser can never hear: it keeps the mark on
        screen next to the winner it just pulled, and files sessions under an
        id that is dead everywhere else. The stamp is what makes the merge
        travel — the seq only decides that the row ships.

        Filings on an already-dead mark heal the same way, which is what an
        offline device's late push leaves behind."""
        rows = db.execute(
            "SELECT id, name, color_hex, deleted FROM marks "
            "ORDER BY name, color_hex, id").fetchall()
        winners: dict[tuple, str] = {}
        for r in rows:
            if not r["deleted"]:
                winners.setdefault((r["name"], r["color_hex"]), r["id"])
        for r in rows:
            winner = winners.get((r["name"], r["color_hex"]))
            if winner is None or winner == r["id"]:
                continue
            # MAX(now, held + 1): strictly newer than the stamp any device
            # holds for this row, even if a clock put the row in the future.
            db.execute(
                "UPDATE session_meta SET mark_id=?, updated_at=MAX(?, updated_at + 1), "
                "seq=? WHERE mark_id=?", (winner, now, seq, r["id"]))
            if not r["deleted"]:
                db.execute(
                    "UPDATE marks SET deleted=1, updated_at=MAX(?, updated_at + 1), "
                    "seq=? WHERE id=?", (now, seq, r["id"]))

    # A twin merge for shortcuts lived here and is GONE. It tombstoned the
    # second live row onto a (seat, tag) pair, which was right while a shortcut
    # said only *this way in* — two devices adding the same card meant the same
    # thing twice. It stopped being right the moment a shortcut carries a
    # config: "Ada on Opus" and "Ada on Codex" are the same pair and two
    # different launchers, and the merge would silently delete whichever the user
    # made second, on every device, minutes later. Identity is the row id, and
    # `_canonicalise_shortcut_ids` is what keeps that one honest.


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).isoformat() if epoch else ""


def _serve_session(row: dict) -> dict:
    """A stored row in the shape the app's session lists already consume —
    the SessionSummary fields, live-state left to the board endpoints.
    Preview/exchange lines share the board's rules — one machine, one seam:
    spawn-marked tasks surface, `<`-shaped noise drops."""
    from .board import _convo_lines, _preview
    last_prompt, last_reply = _convo_lines(row)
    return {
        "session_id": row["session_id"],
        "agent_id": row["agent_id"],
        "sub_mode": row["sub_mode"],
        "preview": _preview(row),
        "last_prompt": last_prompt,
        "last_reply": last_reply,
        "last_activity": row.get("last_activity") or "",
        "spawned": row.get("spawned") or "",
        "tokens": int(row.get("in_tokens") or 0) + int(row.get("out_tokens") or 0),
        "last_context": int(row.get("last_context") or 0),
        "turns": int(row.get("calls") or 0),
        "agent_color": row.get("agent_color") or "",
        "path": row.get("path") or "",
        "size": int(row.get("file_size") or 0),
        "deleted": bool(row.get("deleted")),
        "live": False, "open": False, "on_mac": False, "managed": False,
        "attention": "", "unread": False,
    }


# ── process-wide instance + indexer thread (startup-bound, never import) ──

_store: SessionStore | None = None
_store_lock = threading.Lock()
_indexer: threading.Thread | None = None

TICK = 3.0


def get_store() -> SessionStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = SessionStore()
        return _store


def start_indexer() -> None:
    """Run the refresh loop for the life of the process. Called from the
    dashboard's startup hook — an import (a test, a script) must never start
    filesystem work by itself."""
    global _indexer
    if _indexer is not None and _indexer.is_alive():
        return

    def _run() -> None:
        store = get_store()
        while True:
            try:
                store.refresh()
            except Exception as e:  # noqa: BLE001 — the loop must outlive a bad pass
                print(f"jremote store: refresh failed ({type(e).__name__}: {e})",
                      flush=True)
            time.sleep(TICK)

    _indexer = threading.Thread(target=_run, name="jremote-store-indexer",
                                daemon=True)
    _indexer.start()


if __name__ == "__main__":
    # One-shot backfill/refresh: `python -m jstack_host.store`
    t0 = time.time()
    n = get_store().refresh()
    print(f"indexed {n} changed transcripts in {time.time() - t0:.1f}s "
          f"(seq {get_store().current_seq()})")
