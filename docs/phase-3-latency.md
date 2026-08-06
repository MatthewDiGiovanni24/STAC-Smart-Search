# Phase 3 — Search Latency (query-plan restructure)

Status: **planned, not started.** This documents the diagnosis, the gate that
governs the work, and the findings that were expensive to discover so nobody
re-derives them from scratch.

## Problem

The collection pre-filter (`get_candidate_collections` in
`app/services/registry.py`) sequential-scans the full ~55k-row `collections`
table on **every** search. Against a populated registry this is ~1.3–2.8s of the
request, and it is now the dominant cost of a query.

The cause is the admission `WHERE`, which ORs a non-indexable vector predicate
with the lexical/fuzzy predicates:

```
WHERE (embedding <=> $q < 0.25            -- vector distance threshold
       OR id ILIKE '%text%'               -- substring
       OR title ILIKE '%text%'
       OR word_similarity($text, lower(title)) >= 0.6)  -- fuzzy tier
```

`embedding <=> $q < 0.25` is a **distance range filter**. pgvector's HNSW index
accelerates `ORDER BY embedding <=> $q LIMIT k` (nearest-neighbour), **not** a
`< threshold` predicate — so Postgres must compute the distance for every row.
The OR then forces a full scan regardless of the other branches, so the GIN
trigram index on `lower(title)` (migration `d4e0b8c2f9a1`) is **never used** by
this query shape.

## EXPLAIN ANALYZE (verbatim; vector literal elided)

Run against the populated 55k registry, query text `"methan plume"`. The 512-dim
query vector is shown as `'[0.01, …512 dims…]'::vector`; everything else is
verbatim. Reproducing this requires a populated registry (see
`python -m app.cli refresh-registry`).

```
 Limit  (cost=9291.97..9303.63 rows=100 width=35) (actual time=1257.844..1263.053 rows=3 loops=1)
   ->  Gather Merge  (cost=9291.97..12267.17 rows=25500 width=35) (actual time=1257.298..1262.393 rows=3 loops=1)
         Workers Planned: 2
         Workers Launched: 2
         ->  Sort  (cost=8291.94..8323.82 rows=12750 width=35) (actual time=1113.348..1113.350 rows=1 loops=3)
               Sort Key: (CASE WHEN ((lower((id)::text) = 'methan plume'::text) OR ...) THEN 0 ... ELSE 4 END)
               Sort Method: quicksort  Memory: 25kB
               ->  Parallel Seq Scan on collections  (cost=0.00..7804.65 rows=12750 width=35) (actual time=728.567..1112.152 rows=1 loops=3)
                     Filter: (((embedding <=> '[0.01, …512 dims…]'::vector) < '0.25'::double precision)
                              OR ((id)::text ~~* '%methan plume%'::text)
                              OR ((title)::text ~~* '%methan plume%'::text)
                              OR (word_similarity('methan plume'::text, lower((title)::text)) >= '0.6'::double precision))
                     Rows Removed by Filter: 18356
 Planning Time: 183.477 ms
 Execution Time: 1285.429 ms
(14 rows)
```

Read: **Parallel Seq Scan on collections**, ~18k rows removed by filter per
worker, no index access. (Execution time varies run-to-run — a separate run of
the same query measured 2780 ms — because the scan also evaluates the vector
distance and `word_similarity` per row.)

## Proposed restructure: UNION of an indexed branch and a vector branch

Split the single OR-scan into two branches whose union is the candidate set:

1. **Lexical/fuzzy branch** — served by the GIN trigram index and plain btree,
   using the operator forms that the planner can push into the index
   (`title % $text`, `$text <% lower(title)`, prefix/substring). Returns only the
   rows that actually match lexically — a small set, no full scan.
2. **Vector branch** — `ORDER BY embedding <=> $q LIMIT k`, which the HNSW index
   *does* serve, returning the top-k nearest by cosine.

`UNION` the two, then apply the tier CASE + ordering on the (small) combined set.
This removes the full-table distance scan from the hot path and lets the trigram
index do real work.

## GATE — result-equivalence before latency

**Do not measure or claim a speedup until the restructured query is proven to
return the same candidate set as the current query.** The UNION is only valid if
it is result-identical; a faster query that returns a different set is a
regression, not a win.

Concretely, before touching timing:

1. Build a result-equivalence harness: for a fixed set of queries (reuse
   `scripts/eval_lexical.py`'s query list), assert the restructured
   `get_candidate_collections` returns the **same (provider_id, id, match_tier)
   set** (order-independent for admission; order-checked separately) as the
   current implementation, against the populated 55k registry.
2. Only once equivalence holds across that set do you compare `EXPLAIN ANALYZE`
   plans and wall-clock. The equivalence harness is the deliverable that makes
   the latency number trustworthy.

Edge cases the harness must cover: the vector-branch `LIMIT k` must be large
enough that no row admitted by the current threshold filter is dropped (or the
union is a superset by construction and you accept that), and the fuzzy tier's
`word_similarity` cutoff must match exactly.

## Findings to NOT re-investigate (they cost real work to establish)

### `cosine_distance_threshold = 0.25` is a real admission gate, ~5–7% — not a no-op

An early microtest (4 short unrelated strings scoring 0.76–0.80 cosine) suggested
the threshold admitted almost everything. **That was wrong.** Measured against the
full 55k registry, the distance distribution is far wider and the 0.25 gate
admits only ~5–7% of rows, rejecting ~93%:

| query | min dist | p1 | p50 | frac passing 0.25 |
| --- | --- | --- | --- | --- |
| flood inundation | 0.172 | 0.226 | 0.307 | 7.0% |
| sea ice concentration | 0.085 | 0.229 | 0.316 | 5.0% |

Keep 0.25. It is doing meaningful admission filtering. The microtest's narrow
sample, not the threshold, was the problem.

### RemoteCLIP's flat 0.76–0.80 cosines on short text are inherent, not broken weights

The RemoteCLIP checkpoint loads correctly (302 tensors, 0 missing / 0 unexpected
keys; verified in `app/services/embeddings.py`, guarded by
`_check_state_dict_applied`). The flat similarity scores across unrelated short
strings are **anisotropy of the text encoder**, an inherent property of the
trained model — NOT randomly-initialized weights. Corroborating detail from the
threshold data above: `min` (0.085) sits far below `p1` (0.229), so cosine
discriminates sharply at the very top of the distribution and goes flat
immediately after. It is a good fine-ranker for the top handful and useless past
it — which is why the lexical/fuzzy tiers, not cosine, carry ordering for short
opaque queries, and why cosine remains the intra-tier order for the semantic
tier only.

### MODSI stays xfail — do not "fix" it with fuzzystrmatch on a whim

`scripts/eval_lexical.py` pre-commits `MODSI` (typo of `MODIS`) as an expected
failure. Trigram similarity **cannot** resolve it: the I↔S transposition drops
`word_similarity('modsi','modis')` to 0.5, which is exactly tied with the "Model"
false-positive wall — no threshold separates the target from the noise.
Resolving transpositions-into-a-real-word needs edit-distance (Levenshtein) or a
phonetic encoder (`fuzzystrmatch`), each a different tool with its own tuning
surface, for one query. Leave it xfail. Revisit only if typo tolerance becomes a
real, recurring complaint — then evaluate `fuzzystrmatch`, not before.
