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

FUZZY TIER (pg_trgm) OUTCOME, decided from measured word_similarity:
  * "methan plume" -> PASS via the FUZZY tier. EMITL2BCH4PLM scores 0.80 (titles
    literally say "Methane Plume Complexes"), clear of the ~0.54 noise floor.
    The "tier" column MUST read "fuzzy" — a pass via "semantic" would be the
    spurious-hit failure mode, not typo tolerance.
  * "MODSI" -> STAYS xfail. The I<->S transposition drops word_similarity(MODSI,
    MODIS) to 0.5, exactly tied with the "Model" false-positive wall — no
    threshold separates them. Trigram cannot resolve a transposition into a
    common word; this needs edit-distance/phonetic matching, NOT pg_trgm.

XPASS NOTE (MODSI): the anchored ^MODIS pattern already rejects incidental
semantic hits, so MODSI reports a clean xfail. If it ever XPASSes, it is a
spurious semantic hit, NOT typo tolerance — do not read it as fuzzy coverage.
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
    ("methan plume",         r"^EMITL2BCH4PLM",     "top1",  "typo -> fuzzy tier (must match at tier=fuzzy)"),
    ("MODSI",                r"^MODIS",             "xfail", "typo transposition -> trigram cannot resolve"),
]

# Queries whose passing row must be served by a specific tier (guards against a
# green result that's actually coming from the wrong path, e.g. a semantic fluke).
REQUIRED_TIER: dict[str, str] = {"methan plume": "fuzzy"}


def _first_hit(rows: list[dict], pattern: str) -> tuple[int | None, str | None]:
    """(1-based rank, tier) of the first row (within top-K) whose id/title matches."""
    rx = re.compile(pattern, re.IGNORECASE)
    for i, r in enumerate(rows[:TOP_K], start=1):
        if rx.search(r.get("id") or "") or rx.search(r.get("title") or ""):
            return i, r.get("match_tier")
    return None, None


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
        rank, hit_tier = _first_hit(rows, pattern)
        result = _judge(mode, rank)
        # A pass at the wrong tier is a FAIL — guards against a semantic fluke
        # masquerading as fuzzy coverage.
        need = REQUIRED_TIER.get(query)
        if result == "PASS" and need and hit_tier != need:
            result = "FAIL"
        tally[result] += 1
        top1 = f"{(rows[0].get('id') if rows else '—')} ({_tier(rows[0]) if rows else '—'})"
        via = f"  via tier={hit_tier}" if need else ""
        print(f"{query:<24}{mode:<7}{str(rank or '-'):<6}{result:<8}{top1}{via}")

    print("-" * 84)
    print(
        f"PASS {tally['PASS']}  FAIL {tally['FAIL']}  "
        f"xfail {tally['xfail']} (expected)  XPASS {tally['XPASS']} (spurious semantic — not fuzzy)"
    )
    await close_db_pool()


if __name__ == "__main__":
    asyncio.run(main())
