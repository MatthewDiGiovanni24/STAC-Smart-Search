"""Fixed lexical/semantic eval set for hybrid ranking (Phase 2).

Ten queries with pre-committed expected answers, grounded in the real 55k-row
registry. Run against the LIVE ranking (get_candidate_collections) to capture a
before/after. Two queries are pre-declared EXPECTED FAILURES until the pg_trgm
fuzzy tier lands — so the follow-up has a success criterion decided in advance,
not invented after seeing results.

    DATABASE_URL=postgresql://…/stac python scripts/eval_lexical.py

PASS CRITERIA (committed before implementing tiers, applied UNIFORMLY):
  * "top1"     — the canonical row (anchored ^regex on id OR title) must rank
                 EXACTLY #1. Applied to every lexical query, not just the ones
                 that fail today. An anchored ^ pattern rejects incidental
                 mid-string matches, so this measures ORDERING, not mere presence.
  * "top5"     — soft: a plausible match anywhere in top-5 (semantic queries,
                 which depend on embedding quality we don't control).
  * "xfail"    — expected to fail until the pg_trgm fuzzy tier. Reported, never
                 counted as a regression.

CANONICAL CHOICES (the defensible-rule reasoning, committed up front):
  * EMIT  -> ^EMIT  is FAMILY MEMBERSHIP, deliberately NOT the L2A reflectance
    flagship. All 17 EMIT collections match "EMIT" equally; the ranker has no
    principled signal to prefer one product, so demanding a specific one would
    be overfitting. "An EMIT-family row ranks #1" rejects all incidental
    substring/semantic rows without faking relevance the architecture lacks.
  * MODIS -> ^MODIS is the canonical MODIS/Terra|Aqua products (title-prefix),
    NOT an incidental mid-string "…_MODIS_…" reference. This is the query that
    fails at baseline (a mid-string row wins) and the tiers are meant to fix.

XPASS NOTE (MODSI): if a typo query "passes", it is a SPURIOUS semantic hit — an
unrelated collection that happened to surface — NOT typo tolerance. Do NOT read
a green XPASS as pg_trgm coverage; the fuzzy tier is still required.
"""

import asyncio
import re

from app.database import close_db_pool, init_db_pool
from app.services.embeddings import embed_query
from app.services.registry import get_candidate_collections

TOP_K = 5

# (query, canonical_regex, mode, note)
QUERIES: list[tuple[str, str, str, str]] = [
    ("EMIT",                 r"^EMIT",              "top1",  "acronym, id-prefix; family membership"),
    ("MODIS",                r"^MODIS",             "top1",  "canonical MODIS/* title-prefix (fails at baseline)"),
    ("EMITL2ARFL_001",       r"^EMITL2ARFL_001$",   "top1",  "exact id paste -> tier 0"),
    ("carbon dioxide plume", r"^EMITL2BCO2PLM",     "top1",  "partial title -> CO2 plume product family"),
    ("SWOT",                 r"^SWOT",              "top1",  "acronym, id-prefix"),
    ("sentinel-2-l2a",       r"^sentinel-2-l2a$",   "top1",  "exact id (non-CMR)"),
    ("wildfire burn severity", r"burn|fire",        "top5",  "natural language -> semantic tier"),
    ("sea ice concentration",  r"sea.?ice|ice.?conc", "top5", "natural language -> semantic tier"),
    ("MODSI",                r"^MODIS",             "xfail", "typo of MODIS -> needs pg_trgm (see XPASS NOTE)"),
    ("methan plume",         r"Methane Plume",      "xfail", "typo 'methan' -> needs pg_trgm"),
]


def _first_hit_rank(rows: list[dict], pattern: str) -> int | None:
    """1-based rank of the first row (within top-K) whose id/title matches, else None."""
    rx = re.compile(pattern, re.IGNORECASE)
    for i, r in enumerate(rows[:TOP_K], start=1):
        if rx.search(r.get("id") or "") or rx.search(r.get("title") or ""):
            return i
    return None


def _tier(r: dict) -> str:
    """Best-effort tier label across baseline (is_exact) and tiered output."""
    if "match_tier" in r:
        return str(r["match_tier"])
    return "exact" if r.get("is_exact") else "semantic"


def _judge(mode: str, rank: int | None) -> str:
    """Map (mode, matched-rank) to a result label.

    top1 requires rank == 1 (ordering); top5 requires rank in 1..K (presence);
    xfail expects no match (a match is XPASS — flag, don't celebrate).
    """
    if mode == "top1":
        return "PASS" if rank == 1 else "FAIL"
    if mode == "top5":
        return "PASS" if rank is not None else "FAIL"
    # xfail
    return "XPASS" if rank is not None else "xfail"


async def main() -> None:
    pool = await init_db_pool()
    tally: dict[str, int] = {"PASS": 0, "FAIL": 0, "xfail": 0, "XPASS": 0}
    print(f"{'query':<24}{'mode':<7}{'rank':<6}{'result':<8}top-1 (tier)")
    print("-" * 84)
    for query, pattern, mode, _note in QUERIES:
        vec = await asyncio.to_thread(embed_query, query)
        rows = await get_candidate_collections(
            pool=pool, text=query, search_embedding=vec, limit=TOP_K
        )
        rank = _first_hit_rank(rows, pattern)
        result = _judge(mode, rank)
        tally[result] += 1
        top1 = f"{(rows[0].get('id') if rows else '—')} ({_tier(rows[0]) if rows else '—'})"
        print(f"{query:<24}{mode:<7}{str(rank or '-'):<6}{result:<8}{top1}")

    print("-" * 84)
    print(
        f"PASS {tally['PASS']}  FAIL {tally['FAIL']}  "
        f"xfail {tally['xfail']} (expected)  XPASS {tally['XPASS']} (spurious semantic — not fuzzy)"
    )
    await close_db_pool()


if __name__ == "__main__":
    asyncio.run(main())
