"""Match businesses to BC Registries (OrgBook) records.

Gives "years in business" and entity type (sole proprietor vs corporation),
both used by the decision matrix. Matching order:

1. the local OrgBook index built by `collect orgbook` (fast, no requests)
2. optionally, a live OrgBook name search per unmatched business (`--online`)

Matches require a high name similarity because OrgBook has no addresses.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from ..config import Config
from ..db import Database
from ..http import HttpClient
from ..normalize import distinctive_tokens, name_key, name_similarity
from ..sources import orgbook

log = logging.getLogger(__name__)

MIN_SIMILARITY = 0.9


class RegistryIndex:
    def __init__(self, entries: list[dict]):
        self.entries = entries
        self.by_key: dict[str, list[dict]] = defaultdict(list)
        self.by_token: dict[str, list[dict]] = defaultdict(list)
        for e in entries:
            for n in e.get("all_names") or [e["name"]]:
                self.by_key[name_key(n)].append(e)
                for tok in distinctive_tokens(n):
                    self.by_token[tok].append(e)

    def best(self, names: list[str]) -> tuple[dict | None, float]:
        best, best_score = None, 0.0
        for name in names:
            if not name:
                continue
            exact = self.by_key.get(name_key(name))
            if exact:
                return exact[0], 1.0
            cands = {id(e): e for tok in distinctive_tokens(name) for e in self.by_token.get(tok, [])}
            for e in cands.values():
                score = max(name_similarity(name, n) for n in (e.get("all_names") or [e["name"]]))
                if score > best_score:
                    best, best_score = e, score
        return (best, best_score) if best_score >= MIN_SIMILARITY else (None, best_score)


def _entry_from_record(rec) -> dict:
    return {"source_id": rec.source_id, "name": rec.name, **rec.extra}


def match_registry(cfg: Config, db: Database, http: HttpClient | None = None,
                   online: bool = False, limit: int | None = None) -> dict:
    index = RegistryIndex([_entry_from_record(r) for r in db.iter_records("orgbook")])
    existing = db.registry_matches()
    stats = {"local": 0, "online": 0, "unmatched": 0}
    online_budget = limit
    for biz in db.iter_businesses():
        nk = name_key(biz.name)
        if not nk or (nk in existing and existing[nk].get("match")):
            continue
        names = [biz.name] + biz.alt_names + [biz.extra.get("legal_name") or ""]
        entry, score = index.best(names)
        method = "local"
        if not entry and online and http is not None and (online_budget is None or online_budget > 0):
            if nk in existing and existing[nk].get("searched_online"):
                continue
            if online_budget is not None:
                online_budget -= 1
            try:
                results = orgbook.lookup_name(cfg, http, biz.name)
            except RuntimeError as exc:
                log.warning("OrgBook lookup failed for %s: %s", biz.name, exc)
                results = []
            online_index = RegistryIndex(results)
            entry, score = online_index.best(names)
            method = "online"
            if not entry:
                db.save_registry_match(nk, {"match": None, "searched_online": True})
                stats["unmatched"] += 1
                continue
        if entry:
            db.save_registry_match(nk, {"match": entry, "score": round(score, 3), "method": method})
            stats[method] += 1
        else:
            stats["unmatched"] += 1
    return stats
