"""Plans, stages, tasks and proofs — the state a plan's markdown cannot hold.

A plan is the deliverable; a stage is one isolated verifiable unit inside it; a
task is the order of operations inside a stage. The markdown stays the authored
artifact (`plans.plan_file` points at it); these rows say what started, what
finished, and what proof says so — so "is this done" answers with a stage id and
its evidence.

The enforcement is one refusal and nothing else: `stage_done` raises
`VerificationMissing` when a stage declared a `verify_kind` and no `stage_proofs`
row for it passed. Deliberately no blocking hook anywhere in this design — a hook
that vetoes a turn can wedge a session it does not understand, whereas a writer
that refuses simply leaves the bad row unwritten and unreachable. Every reader
downstream can then trust the table without re-deriving the rule.

Tables are declared in `store._SCHEMA` (they must be, or an existing store never
grows the columns); the CRUD is here, over `store.get_store().conn()`.
"""

import json
import subprocess
import time
import uuid

from . import environment, plan_parse, store

#: How much of a command's combined output a proof row keeps, from the END.
#: A proof is evidence that a check ran and what it said, not an archive of it:
#: the failing assertion, the traceback's last frames and the summary line all
#: live at the tail, while a green run's head is a thousand lines of collection
#: noise. Capping here rather than at read time keeps a runaway build log out of
#: the store file the board queries on every poll.
OUTPUT_TAIL = 4000

#: Stage statuses that count as still open work, and plan statuses that mean a
#: plan is still somebody's current job.
OPEN_PLAN_STATUS = ("planning", "active")

#: The most plans one `list_plans` call will ever return. The clamp lives in
#: the reader and not at the route because the route is not the only caller and
#: the next one would have to remember: `LIMIT -1` is SQLite for no limit at
#: all, so a query string reaching the parameter unchecked turns the board's
#: poll into a full scan of every plan this host has ever held.
MAX_LIST = 500


class VerificationMissing(ValueError):
    """`stage_done` on a stage that declared a proof kind and has no passing proof."""


def _now() -> float:
    return time.time()


def _field(src: object, name: str, default=None):
    """One accessor for both shapes `set_stages` is handed.

    The parser hands back `plan_parse.Stage` objects; the API layer and the
    tests hand back plain dicts of the same fields. Reading both by duck-type
    keeps this module off the parser's dataclass, so a field moving there is a
    field this one stops finding rather than a store that will not import. The
    one thing taken from `plan_parse` is `KINDS`, a tuple of strings: the set of
    proof kinds has to have exactly one definition, and it is the parser's.
    """
    if isinstance(src, dict):
        return src.get(name, default)
    return getattr(src, name, default)


def _rows(cursor) -> list[dict]:
    return [dict(r) for r in cursor.fetchall()]


# ── plans ────────────────────────────────────────────────────────────────────

def open_plan(title: str, *, plan_file: str = "", engine: str = "", repo: str = "",
              session_id: str = "", role: str = "author") -> str:
    """Create a plan in `planning` and, if a session was named, join it."""
    plan_id = str(uuid.uuid4())
    now = _now()
    with store.get_store().conn() as db:
        db.execute(
            "INSERT INTO plans (id, title, status, plan_file, engine, repo,"
            " created_at, updated_at, deleted) VALUES (?,?,?,?,?,?,?,?,0)",
            (plan_id, title or "", "planning", plan_file or "", engine or "",
             repo or "", now, now))
    if session_id:
        join(plan_id, session_id, role)
    return plan_id


def join(plan_id: str, session_id: str, role: str = "") -> None:
    """Attach a session to a plan. Idempotent, and a rejoin never demotes a role.

    A splitoff or a resume joins the plan it is already on, usually with no role
    to declare; overwriting blindly would turn the author of the plan into an
    anonymous member the second they reopened their own terminal.
    """
    with store.get_store().conn() as db:
        db.execute(
            "INSERT INTO plan_sessions (plan_id, session_id, role, joined_at)"
            " VALUES (?,?,?,?) ON CONFLICT(plan_id, session_id) DO UPDATE SET"
            " role = CASE WHEN excluded.role != '' THEN excluded.role"
            "             ELSE plan_sessions.role END",
            (plan_id, session_id, role or "", _now()))


def update_plan_meta(plan_id: str, *, title: str | None = None,
                     plan_file: str | None = None) -> None:
    """Correct a plan's title or its authored file after the row exists.

    The row is minted at plan-mode ENTRY, before anything has been authored, so
    it is titled from the first prompt and knows no file. The real title and the
    real path only arrive at approval. Without this, both are discarded on the
    common path and the plan is listed under a truncated prompt forever, with
    `plan_file` empty — which is the column `GET /plans/{id}/document` reads, so
    the document is unreachable too.

    `None` means leave that field alone; `""` is a value and clears it. A field
    nobody named is not the same as a field set to nothing, and collapsing the
    two would make one caller correcting the title silently erase the path.
    """
    sets, vals = [], []
    if title is not None:
        sets.append("title = ?")
        vals.append(title)
    if plan_file is not None:
        sets.append("plan_file = ?")
        vals.append(plan_file)
    if not sets:
        raise ValueError("update_plan_meta: name title, plan_file, or both")
    with store.get_store().conn() as db:
        _require_plan(db, plan_id)
        db.execute(f"UPDATE plans SET {', '.join(sets)}, updated_at = ?"
                   " WHERE id = ?", (*vals, _now(), plan_id))


def _require_plan(db, plan_id: str) -> None:
    """Refuse a plan id nothing answers to, the way the stage writers do.

    An UPDATE naming a row that does not exist reports success and changes
    nothing, so a mistyped id reads as a plan that was activated. The stage
    writers already refuse this; a plan writer that did not would be the one
    place a typo is silently absorbed.
    """
    if db.execute("SELECT 1 FROM plans WHERE id = ? AND deleted = 0",
                  (plan_id,)).fetchone() is None:
        raise ValueError(f"no such plan: {plan_id!r}")


def _set_plan_status(plan_id: str, status: str) -> None:
    with store.get_store().conn() as db:
        _require_plan(db, plan_id)
        db.execute("UPDATE plans SET status = ?, updated_at = ? WHERE id = ?",
                   (status, _now(), plan_id))


def activate(plan_id: str) -> None:
    _set_plan_status(plan_id, "active")


def finish(plan_id: str) -> None:
    _set_plan_status(plan_id, "done")


def abandon(plan_id: str) -> None:
    _set_plan_status(plan_id, "abandoned")


# ── stages ───────────────────────────────────────────────────────────────────

def set_stages(plan_id: str, parsed_stages) -> None:
    """Reconcile a plan's stages against a fresh parse, by `(plan_id, ordinal)`.

    An existing ordinal is UPDATED IN PLACE — title, body and verify fields move,
    while `status`, `session_id`, `started_at`, `finished_at` and the stage's
    proofs stay exactly as they were. A new ordinal is inserted.

    A stage whose GATE moved — `verify_kind` or `verify_spec`, not its title or
    its prose — is re-stamped with `verify_set_at`, which retires the proofs it
    already holds without deleting one of them. Otherwise a stage declaring
    `command · echo ok`, verified green, and then re-parsed into
    `command · ./full-acceptance.sh` closes on the cheap receipt: the board
    shows a green proof, the heavy script never ran, and the record is a lie of
    exactly the kind this table exists to prevent. The old proof stays visible
    as the history of the old gate; it simply stops answering for the new one.

    A stage already `done` stays done. Reopening finished work every time the
    markdown is touched would make the reconcile unusable, and the record is
    still honest: the retired proof is visibly older than the declaration, and
    any further attempt to close that stage refuses. What is not claimed is
    that a done stage was proved against its current wording.

    Only a stage whose ordinal is past the new end AND is still `pending` is
    deleted. A stage that already ran is history: the user edits the markdown
    constantly, and a re-parse that dropped a finished stage would erase the
    proof that it was finished — the one fact this table exists to keep. So a
    `done`, `running` or `blocked` stage past the end simply stays, visibly
    beyond the plan, rather than being silently rewritten out of it.

    A `verify_kind` outside `plan_parse.KINDS` — including the empty string the
    parser leaves on a line it could not read — RAISES, and the whole call
    writes nothing. It used to fall back to `none`, which is the one kind
    `stage_done` closes on nobody's evidence: an author who typed a gate and
    misspelled it got a stage that reads as gated, is not, and reports done on
    no receipts. Unreadable must never resolve to needs-no-proof. `none` is
    available and is a deliberate declaration; it has to be that word.

    Accepts `plan_parse.Stage` objects or plain dicts carrying the same fields.
    """
    incoming = []
    for i, src in enumerate(parsed_stages or []):
        raw = _field(src, "ordinal")
        ordinal = i + 1 if raw is None else int(raw)
        title = str(_field(src, "title", "") or "")
        kind = str(_field(src, "verify_kind", "none") or "")
        if kind not in plan_parse.KINDS:
            raise ValueError(
                f"stage #{ordinal} {title!r}: verify_kind {kind!r} is not a "
                f"proof kind; use one of {', '.join(plan_parse.KINDS)}. Nothing "
                f"was written — fix the `Verify:` line and parse the plan again.")
        incoming.append({
            "ordinal": ordinal,
            "title": title,
            "body": str(_field(src, "body", "") or ""),
            "verify_kind": kind,
            "verify_spec": str(_field(src, "verify_spec", "") or ""),
        })
    last = max((s["ordinal"] for s in incoming), default=0)
    now = _now()
    with store.get_store().conn() as db:
        for s in incoming:
            prior = db.execute(
                "SELECT verify_kind, verify_spec, verify_set_at FROM stages"
                " WHERE plan_id = ? AND ordinal = ?",
                (plan_id, s["ordinal"])).fetchone()
            if prior is None:
                db.execute(
                    "INSERT INTO stages (id, plan_id, ordinal, title, body, status,"
                    " verify_kind, verify_spec, updated_at, verify_set_at)"
                    " VALUES (?,?,?,?,?,'pending',?,?,?,?)",
                    (str(uuid.uuid4()), plan_id, s["ordinal"], s["title"],
                     s["body"], s["verify_kind"], s["verify_spec"], now, now))
                continue
            moved = (prior["verify_kind"] != s["verify_kind"]
                     or prior["verify_spec"] != s["verify_spec"])
            db.execute(
                "UPDATE stages SET title = ?, body = ?, verify_kind = ?,"
                " verify_spec = ?, updated_at = ?, verify_set_at = ?"
                " WHERE plan_id = ? AND ordinal = ?",
                (s["title"], s["body"], s["verify_kind"], s["verify_spec"], now,
                 now if moved else prior["verify_set_at"], plan_id, s["ordinal"]))
        dropped = [r["id"] for r in db.execute(
            "SELECT id FROM stages WHERE plan_id = ? AND ordinal > ?"
            " AND status = 'pending'", (plan_id, last)).fetchall()]
        for stage_id in dropped:
            db.execute("DELETE FROM stage_tasks WHERE stage_id = ?", (stage_id,))
            db.execute("DELETE FROM stages WHERE id = ?", (stage_id,))
        db.execute("UPDATE plans SET updated_at = ? WHERE id = ?", (now, plan_id))


def stage_start(stage_id: str, *, session_id: str = "", env=None) -> None:
    """Mark a stage running, recording which session took it and the environment
    it was dispatched with — a snapshot, so a later settings change cannot
    rewrite the answer to "how was this run".

    `env=None` means RESOLVE the named session's environment, not store nothing.
    A caller that forgets the snapshot is exactly the caller whose stage later
    needs explaining, and an empty column cannot say whether the work was taken
    inline or handed to a subagent. Pass a dict to record something else; pass
    `{}` to record deliberately nothing.

    `environment.resolve` is reached through the module rather than bound at
    import for `store`'s reason: both are resolved per call, so a test or an
    embedding process can put its own in front of this one.
    """
    if env is None:
        env = {k: v for k, (v, _) in environment.resolve(session_id).items()}
    now = _now()
    with store.get_store().conn() as db:
        changed = db.execute(
            "UPDATE stages SET status = 'running', session_id = ?, env = ?,"
            " blocked_reason = '', started_at = ?, updated_at = ? WHERE id = ?",
            (session_id or "", json.dumps(env or {}), now, now, stage_id)).rowcount
    if not changed:
        raise ValueError(f"no such stage: {stage_id!r}")


def stage_env(stage) -> dict[str, str]:
    """A stage's dispatch snapshot, decoded — the reader half of `stage_start`.

    It exists so the API and the CLI do not each re-derive the column's format
    from the writer; whoever holds the row passes the row, whoever holds only an
    id passes the id, the same two shapes `_field` exists for.

    An undecodable column reads as an empty snapshot rather than raising. The
    snapshot is evidence ABOUT a stage, not part of its state machine, and a
    board poll that fails over one malformed row hides every good row behind it.
    """
    if isinstance(stage, dict):
        raw = stage.get("env") or ""
    else:
        with store.get_store().conn() as db:
            row = db.execute("SELECT env FROM stages WHERE id = ?",
                             (stage,)).fetchone()
        raw = (row["env"] if row else "") or ""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def stage_done(stage_id: str) -> None:
    """Close a stage — or refuse, and write nothing at all.

    The refusal is the enforcement: a stage that declared any `verify_kind` other
    than `none` closes only once some `stage_proofs` row for it has `ok = 1` AND
    was filed against the declaration in force now — `created_at >=
    verify_set_at`. A proof older than the gate it is being counted against is
    evidence about the previous gate, and counting it is how a re-parse turns a
    cheap green receipt into a closed heavy stage. The read and the write share
    one transaction so a proof cannot land between them.
    """
    with store.get_store().conn() as db:
        row = db.execute(
            "SELECT id, ordinal, title, verify_kind, verify_spec, verify_set_at"
            " FROM stages WHERE id = ?", (stage_id,)).fetchone()
        if row is None:
            raise ValueError(f"no such stage: {stage_id!r}")
        kind = row["verify_kind"] or "none"
        if kind != "none":
            passed = db.execute(
                "SELECT 1 FROM stage_proofs WHERE stage_id = ? AND ok = 1"
                " AND created_at >= ? LIMIT 1",
                (stage_id, row["verify_set_at"] or 0)).fetchone()
            if passed is None:
                stale = db.execute(
                    "SELECT 1 FROM stage_proofs WHERE stage_id = ? AND ok = 1"
                    " LIMIT 1", (stage_id,)).fetchone() is not None
                raise VerificationMissing(_refusal(row, stale=stale))
        now = _now()
        db.execute(
            "UPDATE stages SET status = 'done', blocked_reason = '',"
            " finished_at = ?, updated_at = ? WHERE id = ?", (now, now, stage_id))


def _refusal(row, *, stale: bool = False) -> str:
    """The message a refused close leaves behind.

    It names the stage, what it declared, and the one command that would satisfy
    it, because the reader is an agent mid-turn that has to act on it without
    going to read this file. The remedy is a CLI line and not a Python call for
    the same reason: that reader is standing in a shell, and a remedy they
    cannot run sends them into the source, which is what the message exists to
    save them.
    """
    kind = row["verify_kind"]
    spec = row["verify_spec"] or ""
    stage_id = row["id"]
    by_hand = f"jstack-host plan proof {stage_id} --kind"
    remedy = {
        "command": f"jstack-host plan verify {stage_id} — or {by_hand} command --ok",
        "commit": f"{by_hand} commit --ok --detail <sha>",
        "artifact": f"{by_hand} artifact --ok --detail <path>",
        "manual": f"{by_hand} manual --ok --detail <what the user confirmed>",
    }.get(kind, f"{by_hand} {kind} --ok --detail …")
    # A green proof on the board beside a refusal reads as a broken gate unless
    # the message says which of the two the proof belongs to.
    held = ("its passing proof predates this declaration and proves the gate "
            "this stage used to have" if stale else "has no passing proof")
    return (f"stage {row['id']} (#{row['ordinal']} {row['title']!r}) declares "
            f"verify_kind={kind!r} spec={spec!r} and {held}; "
            f"it stays open. To satisfy it: {remedy}.")


def subagent_env(stage_id: str) -> dict[str, str]:
    """The variables a per-stage subagent is spawned with.

    Three names that two sides have to agree on letter for letter — the spawner
    sets them, the hooks read them to know this sitting is working one stage of
    one plan — so they are spelled once, here, next to the rows they point at.
    The plan id travels with them because the stage row already knows it and a
    spawner would otherwise query for it itself.
    """
    with store.get_store().conn() as db:
        row = db.execute("SELECT plan_id FROM stages WHERE id = ?",
                         (stage_id,)).fetchone()
    if row is None:
        raise ValueError(f"no such stage: {stage_id!r}")
    return {"JSTACK_PLAN_ID": row["plan_id"],
            "JSTACK_STAGE_ID": stage_id,
            "JSTACK_PLAN_SUBAGENT": "1"}


def stage_block(stage_id: str, reason: str) -> None:
    """Park a stage with a reason — or refuse, like every other writer here.

    The rowcount is the existence check: an UPDATE that matched nothing has
    written nothing, so there is no read to race and nothing to undo. Silence
    here was a `plan block <typo>` reporting a stage parked that is still
    running, which the next reader acts on.
    """
    now = _now()
    with store.get_store().conn() as db:
        changed = db.execute(
            "UPDATE stages SET status = 'blocked', blocked_reason = ?,"
            " updated_at = ? WHERE id = ?",
            (reason or "", now, stage_id)).rowcount
    if not changed:
        raise ValueError(f"no such stage: {stage_id!r}")


# ── proofs ───────────────────────────────────────────────────────────────────

def add_proof(stage_id: str, kind: str, ok, *, detail: str = "", output: str = "",
              exit_code: int = 0, duration_ms: int = 0) -> int:
    """Record one piece of evidence and return its row id.

    The path for every kind `run_verify` cannot run: `commit` puts the sha in
    `detail`, `artifact` the path, `manual` the user's own word. Proofs are
    append-only — a stage that failed twice before passing keeps all three rows,
    because the history of a check is part of the evidence.

    An unknown `stage_id` raises. `stage_proofs.stage_id` carries no foreign
    key, so a mistyped id used to insert cleanly and return a row id: a green
    receipt filed against nothing, which no stage will ever read and which looks
    from the call site exactly like diligence. The check shares the insert's
    transaction so the stage cannot be deleted between them.
    """
    with store.get_store().conn() as db:
        if db.execute("SELECT 1 FROM stages WHERE id = ?",
                      (stage_id,)).fetchone() is None:
            raise ValueError(f"no such stage: {stage_id!r}")
        cur = db.execute(
            "INSERT INTO stage_proofs (stage_id, kind, ok, detail, output,"
            " exit_code, duration_ms, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (stage_id, kind or "", 1 if ok else 0, detail or "",
             _tail(output), int(exit_code), int(duration_ms), _now()))
        return int(cur.lastrowid)


def _tail(text: str) -> str:
    text = text or ""
    if len(text) <= OUTPUT_TAIL:
        return text
    return "…[truncated]…\n" + text[-OUTPUT_TAIL:]


def run_verify(stage_id: str, *, cwd=None, timeout: int = 900) -> dict:
    """Run a `command` stage's `verify_spec` and record what it did.

    Only `command` means anything here; any other kind raises rather than
    inventing a proof, because a fabricated pass is worse than no gate at all —
    the whole point of the table is that a green row was earned.

    A timeout is recorded as a failing proof rather than raised away: the check
    ran, it did not pass, and a stage whose verification hangs must not read as a
    stage nobody tried to verify.
    """
    with store.get_store().conn() as db:
        row = db.execute(
            "SELECT verify_kind, verify_spec FROM stages WHERE id = ?",
            (stage_id,)).fetchone()
    if row is None:
        raise ValueError(f"no such stage: {stage_id!r}")
    kind = row["verify_kind"] or "none"
    if kind != "command":
        raise ValueError(
            f"run_verify only runs 'command' stages; stage {stage_id} declares {kind!r}")
    spec = row["verify_spec"] or ""
    if not spec.strip():
        raise ValueError(f"stage {stage_id} declares 'command' with an empty verify_spec")

    t0 = time.monotonic()
    try:
        done = subprocess.run(spec, shell=True, capture_output=True, text=True,
                              timeout=timeout, cwd=cwd)
        code, output = done.returncode, (done.stdout or "") + (done.stderr or "")
        detail = spec
    except subprocess.TimeoutExpired as e:
        # 124 is what `timeout(1)` reports, so a reader of the column does not
        # need to know this branch exists to read it as "killed on time".
        code = 124
        output = _decode(e.stdout) + _decode(e.stderr)
        detail = f"{spec} — timed out after {timeout}s"
    duration_ms = int((time.monotonic() - t0) * 1000)

    proof_id = add_proof(stage_id, "command", code == 0, detail=detail,
                         output=output, exit_code=code, duration_ms=duration_ms)
    with store.get_store().conn() as db:
        return dict(db.execute("SELECT * FROM stage_proofs WHERE id = ?",
                               (proof_id,)).fetchone())


def _decode(chunk) -> str:
    if not chunk:
        return ""
    return chunk if isinstance(chunk, str) else chunk.decode("utf-8", "replace")


# ── tasks ────────────────────────────────────────────────────────────────────

def set_tasks(stage_id: str, tasks) -> None:
    """Replace a stage's task list, reconciled by position.

    By position and not by wiping the table, so a task row's id survives a
    re-listing: the engines that push their native todo list re-send the whole
    list on every change, and an id that churned would make every reader treat
    an unchanged task as a new one.
    """
    now = _now()
    items = list(tasks or [])
    with store.get_store().conn() as db:
        for i, t in enumerate(items, start=1):
            subject = str(_field(t, "subject", "") or "")
            status = str(_field(t, "status", "pending") or "pending")
            native = str(_field(t, "native_id", "") or "")
            changed = db.execute(
                "UPDATE stage_tasks SET subject = ?, status = ?, native_id = ?,"
                " updated_at = ? WHERE stage_id = ? AND ordinal = ?",
                (subject, status, native, now, stage_id, i)).rowcount
            if not changed:
                db.execute(
                    "INSERT INTO stage_tasks (id, stage_id, ordinal, subject,"
                    " status, native_id, updated_at) VALUES (?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), stage_id, i, subject, status, native, now))
        db.execute("DELETE FROM stage_tasks WHERE stage_id = ? AND ordinal > ?",
                   (stage_id, len(items)))


def upsert_task(stage_id: str, native_id: str, subject: str, status: str) -> None:
    """One task by its engine-side id — the incremental path beside `set_tasks`."""
    now = _now()
    with store.get_store().conn() as db:
        changed = db.execute(
            "UPDATE stage_tasks SET subject = ?, status = ?, updated_at = ?"
            " WHERE stage_id = ? AND native_id = ? AND native_id != ''",
            (subject or "", status or "pending", now, stage_id, native_id or "")
        ).rowcount
        if not changed:
            nxt = db.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 AS n FROM stage_tasks"
                " WHERE stage_id = ?", (stage_id,)).fetchone()["n"]
            db.execute(
                "INSERT INTO stage_tasks (id, stage_id, ordinal, subject, status,"
                " native_id, updated_at) VALUES (?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), stage_id, nxt, subject or "",
                 status or "pending", native_id or "", now))


# ── readers ──────────────────────────────────────────────────────────────────

def plan(plan_id: str) -> dict | None:
    with store.get_store().conn() as db:
        row = db.execute("SELECT * FROM plans WHERE id = ? AND deleted = 0",
                         (plan_id,)).fetchone()
    return dict(row) if row else None


def stage(stage_id: str) -> dict | None:
    """One stage by id — what the API and the CLI need to show or check a
    stage they were handed the id of, without reading its whole plan."""
    with store.get_store().conn() as db:
        row = db.execute("SELECT * FROM stages WHERE id = ?", (stage_id,)).fetchone()
    return dict(row) if row else None


def stages(plan_id: str) -> list[dict]:
    with store.get_store().conn() as db:
        return _stage_rows(db, plan_id)


def _stage_rows(db, plan_id: str) -> list[dict]:
    return _rows(db.execute(
        "SELECT * FROM stages WHERE plan_id = ? ORDER BY ordinal", (plan_id,)))


def tasks(stage_id: str) -> list[dict]:
    with store.get_store().conn() as db:
        return _task_rows(db, stage_id)


def _task_rows(db, stage_id: str) -> list[dict]:
    return _rows(db.execute(
        "SELECT * FROM stage_tasks WHERE stage_id = ? ORDER BY ordinal",
        (stage_id,)))


def proofs(stage_id: str) -> list[dict]:
    with store.get_store().conn() as db:
        return _proof_rows(db, stage_id)


def _proof_rows(db, stage_id: str) -> list[dict]:
    return _rows(db.execute(
        "SELECT * FROM stage_proofs WHERE stage_id = ? ORDER BY id", (stage_id,)))


def plan_detail(plan_id: str) -> dict | None:
    """A whole plan — stages, evidence, order of work — from one read snapshot.

    `work`'s reasoning, applied to the other screen: four reads that can each
    see a different commit will happily render proofs under a stage list taken
    before the `set_stages` that replaced it, and the stage those proofs belong
    to is no longer in the list. `None` for a plan that is absent or deleted, so
    the route's 404 stays the route's decision.
    """
    with store.get_store().conn() as db:
        db.execute("BEGIN")
        row = db.execute("SELECT * FROM plans WHERE id = ? AND deleted = 0",
                         (plan_id,)).fetchone()
        if row is None:
            return None
        rows = _stage_rows(db, plan_id)
        return {"plan": dict(row), "stages": rows,
                "proofs": {s["id"]: _proof_rows(db, s["id"]) for s in rows},
                "tasks": {s["id"]: _task_rows(db, s["id"]) for s in rows}}


def list_plans(*, limit: int = 50, include_done: bool = True) -> list[dict]:
    """The newest plans first, never more than `MAX_LIST` of them.

    The limit is clamped rather than validated: a caller asking for nonsense
    gets the nearest sane page, because the reader is a board poll and failing
    it gives a screen with nothing on it over a query string.
    """
    sql = "SELECT * FROM plans WHERE deleted = 0"
    args: list = []
    if not include_done:
        sql += " AND status IN (?, ?)"
        args += list(OPEN_PLAN_STATUS)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    args.append(max(1, min(int(limit), MAX_LIST)))
    with store.get_store().conn() as db:
        return _rows(db.execute(sql, args))


def plans_for_session(session_id: str) -> list[dict]:
    with store.get_store().conn() as db:
        return _rows(db.execute(
            "SELECT p.* FROM plans p JOIN plan_sessions s ON s.plan_id = p.id"
            " WHERE s.session_id = ? AND p.deleted = 0"
            " ORDER BY p.updated_at DESC", (session_id,)))


def open_plan_for_session(session_id: str) -> dict | None:
    """The plan this session is currently on — most recently touched wins.

    A session can be joined to plans it has finished with; only `planning` or
    `active` is work in front of it, and the statusline has room for one.
    """
    with store.get_store().conn() as db:
        return _open_plan_row(db, session_id)


def _open_plan_row(db, session_id: str) -> dict | None:
    row = db.execute(
        "SELECT p.* FROM plans p JOIN plan_sessions s ON s.plan_id = p.id"
        " WHERE s.session_id = ? AND p.deleted = 0 AND p.status IN (?, ?)"
        " ORDER BY p.updated_at DESC LIMIT 1",
        (session_id, *OPEN_PLAN_STATUS)).fetchone()
    return dict(row) if row else None


def work(session_id: str) -> dict:
    """Everything a session's Work view needs, from ONE read of the store.

    One function because the API layer's alternative is three round trips whose
    answers can disagree — a stage list from before a `set_stages` beside tasks
    from after it renders as tasks belonging to nothing.

    One connection is not enough on its own to make that true, which is why the
    `BEGIN` is here: Python's sqlite3 opens a transaction before a write and
    NOT before a SELECT, so three reads on one connection are three autocommit
    reads of whatever is committed at the time of each. The explicit begin takes
    one WAL read snapshot and holds every read in this call against it; the
    `conn()` block closes it. Nothing here writes, so there is no implicit
    begin to collide with.
    """
    with store.get_store().conn() as db:
        db.execute("BEGIN")
        p = _open_plan_row(db, session_id)
        if p is None:
            return {"plan": None, "stages": [], "tasks": {}}
        rows = _stage_rows(db, p["id"])
        return {"plan": p, "stages": rows,
                "tasks": {s["id"]: _task_rows(db, s["id"]) for s in rows}}
