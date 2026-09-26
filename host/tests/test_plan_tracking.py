"""Where a plan's work lands — branch, issue, PR — and the store that learns it.

The one failure worth the file: a host whose store predates the columns. The
store is opened by a running Hub, not created fresh, so `branch`/`issue`/`pr`
reach it only through `_add_missing_columns`; if they did not, the first
`plan set` on an upgraded Mac would raise and every plan would read as unbound.
"""

import contextlib
import io
import json
import sqlite3
import subprocess

import pytest

from jstack_host import cli, plan_parse, plans, store
from jstack_host.store import SessionStore

#: The plans table exactly as it stood before tracking — the shape every
#: existing host's store file has on disk.
OLD_PLANS = """CREATE TABLE plans (
  id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'planning', plan_file TEXT NOT NULL DEFAULT '',
  engine TEXT NOT NULL DEFAULT '', repo TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT 0,
  deleted INTEGER NOT NULL DEFAULT 0)"""

TRACKED_MD = """# Tracked work

Some context. Issue: this sentence is prose and is not read.
Branch: `issue-12`
**Issue:** owner/repo#12
- PR: #40

## Stage 1 — the thing
Verify: none
Branch: below-the-first-stage

## Stage 2 — the other thing
Verify: none
"""


@pytest.fixture(autouse=True)
def plan_store(tmp_path, monkeypatch):
    s = SessionStore(db_path=tmp_path / "plans.sqlite")
    monkeypatch.setattr(store, "_store", s)
    monkeypatch.setattr(store, "get_store", lambda: s)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return s


def _cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


# ── the migration ────────────────────────────────────────────────────────────

def test_a_store_from_before_tracking_grows_the_columns_and_keeps_its_rows(tmp_path):
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.execute(OLD_PLANS)
    db.execute("INSERT INTO plans (id, title, status) VALUES ('p1', 'old', 'active')")
    db.commit()
    db.close()

    upgraded = SessionStore(db_path=path)
    with upgraded.conn() as conn:
        cols = {r[1]: r for r in conn.execute("PRAGMA table_info(plans)")}
        row = dict(conn.execute("SELECT * FROM plans WHERE id = 'p1'").fetchone())
    for name in plans.TRACKING:
        assert name in cols
        assert cols[name][3] == 0, f"{name} must be nullable"
    assert row["title"] == "old"
    assert (row["branch"], row["issue"], row["pr"]) == (None, None, None)


# ── the parse ────────────────────────────────────────────────────────────────

def test_header_lines_above_the_first_stage_are_read_and_prose_is_not():
    parsed = plan_parse.parse(TRACKED_MD)
    assert (parsed.branch, parsed.issue, parsed.pr) == ("issue-12", "owner/repo#12", "#40")
    assert parsed.problems == []
    assert len(parsed.stages) == 2
    # The late `Branch:` line is stage prose, not a header.
    assert "below-the-first-stage" in parsed.stages[0].body


def test_a_plan_with_no_header_lines_leaves_all_three_empty():
    parsed = plan_parse.parse("# t\n\n## Stage 1 — a\nVerify: none\n")
    assert (parsed.branch, parsed.issue, parsed.pr) == ("", "", "")


def test_a_header_inside_a_fence_is_an_example_not_a_binding():
    parsed = plan_parse.parse("# t\n```\nBranch: example\n```\n## Stage 1 — a\nVerify: none\n")
    assert parsed.branch == ""


# ── binding ──────────────────────────────────────────────────────────────────

def _repo(tmp_path, branch):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", branch, str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "seed"], check=True)
    return str(repo)


def test_header_lines_win_over_the_checkout(tmp_path):
    plan_id = plans.open_plan("t")
    plans.bind_authored(plan_id, plan_parse.parse(TRACKED_MD), _repo(tmp_path, "elsewhere"))
    row = plans.plan(plan_id)
    assert (row["branch"], row["issue"], row["pr"]) == ("issue-12", "owner/repo#12", "#40")


def test_the_checkout_fills_an_empty_branch_and_never_overwrites_a_bound_one(tmp_path):
    repo = _repo(tmp_path, "feature/x")
    parsed = plan_parse.parse("# t\n## Stage 1 — a\nVerify: none\n")
    plan_id = plans.open_plan("t")
    plans.bind_authored(plan_id, parsed, repo)
    assert plans.plan(plan_id)["branch"] == "feature/x"

    plans.set_tracking(plan_id, branch="bound-by-hand")
    plans.bind_authored(plan_id, parsed, repo)
    assert plans.plan(plan_id)["branch"] == "bound-by-hand"


def test_no_repo_and_no_header_binds_nothing(tmp_path):
    plan_id = plans.open_plan("t")
    plans.bind_authored(plan_id, plan_parse.parse("# t\n"), str(tmp_path))
    assert plans.plan(plan_id)["branch"] is None


# ── the CLI ──────────────────────────────────────────────────────────────────

def test_list_json_always_carries_the_three_keys_and_the_session():
    bound = plans.open_plan("bound", session_id="s-author")
    plans.join(bound, "s-later")
    plans.set_tracking(bound, branch="issue-7", issue="o/r#7")
    plans.open_plan("unbound")

    code, out, err = _cli("plan", "list", "--json")
    assert code == 0, err
    rows = {r["title"]: r for r in json.loads(out)}
    assert (rows["bound"]["branch"], rows["bound"]["issue"], rows["bound"]["pr"]) \
        == ("issue-7", "o/r#7", None)
    assert rows["bound"]["session"] == "s-author"
    assert rows["bound"]["sessions"] == ["s-author", "s-later"]
    assert {k: rows["unbound"][k] for k in ("branch", "issue", "pr", "session")} \
        == dict.fromkeys(("branch", "issue", "pr", "session"))


def test_list_text_prints_the_columns():
    plan_id = plans.open_plan("bound", session_id="abcdef0123")
    plans.set_tracking(plan_id, branch="issue-7", pr="41")
    code, out, _ = _cli("plan", "list")
    assert code == 0
    header, line = out.splitlines()[:2]
    assert header.split()[:6] == ["SESSION", "PLAN", "BRANCH", "ISSUE", "PR", "STATUS"]
    assert line.split()[:6] == ["abcdef01", plan_id, "issue-7", "-", "41", "planning"]


def test_set_binds_clears_and_refuses_a_plan_that_is_not_there():
    plan_id = plans.open_plan("t")
    code, out, err = _cli("plan", "set", plan_id, "--branch", "b", "--issue", "o/r#1",
                          "--pr", "https://example.invalid/pull/2")
    assert code == 0, err
    assert out.split() == ["branch", "b", "issue", "o/r#1", "pr",
                           "https://example.invalid/pull/2"]

    assert _cli("plan", "set", plan_id, "--pr", "")[0] == 0
    row = plans.plan(plan_id)
    assert (row["branch"], row["pr"]) == ("b", None), "only the named field moves"

    code, _, err = _cli("plan", "set", "ghost", "--branch", "b")
    assert code == 1 and "ghost" in err
    code, _, err = _cli("plan", "set", plan_id)
    assert code == 1 and "name branch" in err


def test_current_prints_the_session_s_open_plan_or_exits_1_silently(monkeypatch):
    code, out, _ = _cli("plan", "current")
    assert (code, out) == (1, "")

    plan_id = plans.open_plan("mine", session_id="s-here")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s-here")
    code, out, err = _cli("plan", "current")
    assert code == 0, err
    assert out.strip() == plan_id
    assert _cli("plan", "current", "--session", "someone-else")[:2] == (1, "")


def test_filing_stages_from_the_shell_binds_the_header(tmp_path):
    plan_id = plans.open_plan("t")
    path = tmp_path / "plan.md"
    path.write_text(TRACKED_MD)
    code, _, err = _cli("plan", "stages", plan_id, "--from-file", str(path))
    assert code == 0, err
    assert plans.plan(plan_id)["issue"] == "owner/repo#12"
