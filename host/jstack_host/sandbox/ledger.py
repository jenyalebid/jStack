"""The host's ledger: every guest, lease and waiting ticket on this machine.

One sqlite file under the footprint root. Every admission runs inside
`BEGIN IMMEDIATE`, so two callers racing for the last slot serialise here
rather than both booting a guest.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS guests (
  name TEXT PRIMARY KEY, tenant TEXT NOT NULL, image TEXT NOT NULL,
  role TEXT NOT NULL, created REAL NOT NULL, ip TEXT DEFAULT '',
  net TEXT DEFAULT '', parked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS leases (
  id TEXT PRIMARY KEY, tenant TEXT NOT NULL, image TEXT NOT NULL,
  kind TEXT NOT NULL, guest TEXT NOT NULL, seat TEXT DEFAULT '',
  owner TEXT NOT NULL, client TEXT NOT NULL, state TEXT NOT NULL,
  created REAL NOT NULL, renewed REAL NOT NULL, orphaned REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS tickets (
  id TEXT PRIMARY KEY, tenant TEXT NOT NULL, image TEXT NOT NULL,
  kind TEXT NOT NULL, created REAL NOT NULL, polled REAL NOT NULL);
"""


# Columns a ledger written before them lacks; added in place on open.
COLUMNS = (("guests", "net", "TEXT DEFAULT ''"), ("guests", "parked", "INTEGER DEFAULT 0"))


def _migrate(db) -> None:
    for table, col, decl in COLUMNS:
        have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        if col not in have:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def path(root: Path) -> Path:
    return root / "ledger.db"


@contextlib.contextmanager
def open_db(root: Path, write: bool = False):
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path(root), timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.executescript(SCHEMA)
        _migrate(db)
        if write:
            db.execute("BEGIN IMMEDIATE")
        yield db
        if write:
            db.execute("COMMIT")
    except BaseException:
        if write and db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()


def lease_row(row) -> dict:
    out = dict(row)
    out["owner"] = json.loads(out["owner"])
    return out


def leases(db, **where) -> list[dict]:
    sql, args = "SELECT * FROM leases", []
    if where:
        sql += " WHERE " + " AND ".join(f"{k}=?" for k in where)
        args = list(where.values())
    return [lease_row(r) for r in db.execute(sql + " ORDER BY created", args)]


def guests(db, **where) -> list[dict]:
    sql, args = "SELECT * FROM guests", []
    if where:
        sql += " WHERE " + " AND ".join(f"{k}=?" for k in where)
        args = list(where.values())
    return [dict(r) for r in db.execute(sql + " ORDER BY created", args)]


def add_lease(db, lease: dict) -> None:
    now = time.time()
    db.execute(
        "INSERT INTO leases (id,tenant,image,kind,guest,seat,owner,client,state,"
        "created,renewed) VALUES (?,?,?,?,?,?,?,?, 'active', ?, ?)",
        (lease["id"], lease["tenant"], lease["image"], lease["kind"],
         lease["guest"], lease.get("seat", ""), json.dumps(lease["owner"]),
         lease["client"], now, now))


def add_guest(db, name: str, tenant: str, image: str, role: str,
              net: str = "") -> None:
    db.execute("INSERT INTO guests (name,tenant,image,role,created,net) "
               "VALUES (?,?,?,?,?,?)", (name, tenant, image, role, time.time(), net))
