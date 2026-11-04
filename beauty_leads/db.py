"""SQLite storage.

Tables
  source_records      raw, normalised listings from each source (upserted)
  businesses          deduplicated businesses (rebuilt by `merge`)
  website_checks      website enrichment results, keyed by normalised URL
  registry_matches    BC Registries (OrgBook) matches, keyed by normalised name
  crm_leads           which CRM lead each business was pushed to, plus what was
                      pulled back from it (status, reported software/spend/staff)

Enrichment is keyed by URL / name rather than business id so that `merge`
can be re-run at any time without losing enrichment work.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Iterable, Iterator

from .models import Business, Record

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_records (
    source      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    data        TEXT NOT NULL,
    fetched_at  REAL NOT NULL,
    PRIMARY KEY (source, source_id)
);
CREATE TABLE IF NOT EXISTS businesses (
    key   TEXT PRIMARY KEY,
    data  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS website_checks (
    url_key     TEXT PRIMARY KEY,
    data        TEXT NOT NULL,
    checked_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS crm_leads (
    business_key TEXT PRIMARY KEY,
    crm          TEXT NOT NULL,
    lead_id      TEXT NOT NULL,
    data         TEXT NOT NULL,
    synced_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS registry_matches (
    name_key    TEXT PRIMARY KEY,
    data        TEXT NOT NULL,
    checked_at  REAL NOT NULL
);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- source records ----------------------------------------------------
    def upsert_records(self, records: Iterable[Record]) -> int:
        now = time.time()
        rows = [(r.source, r.source_id, json.dumps(r.to_dict()), now) for r in records]
        with self.conn:
            self.conn.executemany(
                "INSERT INTO source_records (source, source_id, data, fetched_at) VALUES (?,?,?,?) "
                "ON CONFLICT(source, source_id) DO UPDATE SET data=excluded.data, fetched_at=excluded.fetched_at",
                rows,
            )
        return len(rows)

    def iter_records(self, source: str | None = None) -> Iterator[Record]:
        sql = "SELECT data FROM source_records"
        args: tuple = ()
        if source:
            sql += " WHERE source = ?"
            args = (source,)
        for (data,) in self.conn.execute(sql + " ORDER BY source, source_id", args):
            yield Record.from_dict(json.loads(data))

    def record_counts(self) -> dict[str, int]:
        return dict(self.conn.execute("SELECT source, COUNT(*) FROM source_records GROUP BY source"))

    # --- businesses ----------------------------------------------------------
    def replace_businesses(self, businesses: Iterable[Business]) -> int:
        rows = [(b.key, json.dumps(b.to_dict())) for b in businesses]
        with self.conn:
            self.conn.execute("DELETE FROM businesses")
            self.conn.executemany("INSERT INTO businesses (key, data) VALUES (?, ?)", rows)
        return len(rows)

    def iter_businesses(self) -> Iterator[Business]:
        for (data,) in self.conn.execute("SELECT data FROM businesses ORDER BY key"):
            yield Business.from_dict(json.loads(data))

    def business_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]

    # --- website enrichment --------------------------------------------------
    def save_website_check(self, url_key: str, data: dict) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO website_checks (url_key, data, checked_at) VALUES (?,?,?) "
                "ON CONFLICT(url_key) DO UPDATE SET data=excluded.data, checked_at=excluded.checked_at",
                (url_key, json.dumps(data), time.time()),
            )

    def website_checks(self) -> dict[str, dict]:
        return {k: json.loads(v) for k, v in self.conn.execute("SELECT url_key, data FROM website_checks")}

    # --- registry enrichment -------------------------------------------------
    def save_registry_match(self, name_key: str, data: dict) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO registry_matches (name_key, data, checked_at) VALUES (?,?,?) "
                "ON CONFLICT(name_key) DO UPDATE SET data=excluded.data, checked_at=excluded.checked_at",
                (name_key, json.dumps(data), time.time()),
            )

    # --- CRM links -------------------------------------------------------------
    def save_crm_lead(self, business_key: str, crm: str, lead_id: str, data: dict) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO crm_leads (business_key, crm, lead_id, data, synced_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(business_key) DO UPDATE SET crm=excluded.crm, lead_id=excluded.lead_id, "
                "data=excluded.data, synced_at=excluded.synced_at",
                (business_key, crm, lead_id, json.dumps(data or {}), time.time()),
            )

    def crm_leads(self) -> dict[str, dict]:
        return {k: {"crm": c, "lead_id": lid, "data": json.loads(d)}
                for k, c, lid, d in self.conn.execute("SELECT business_key, crm, lead_id, data FROM crm_leads")}

    def registry_matches(self) -> dict[str, dict]:
        return {k: json.loads(v) for k, v in self.conn.execute("SELECT name_key, data FROM registry_matches")}
