# Vector backend: LanceDB, and why not turbovec

A decision note, in the shape of § 4.12 (“DuckDB is read-side only”): what was
evaluated, what was measured, and what the answer is until new evidence moves
it.

**Decision: keep LanceDB. Do not replace it with turbovec.** turbovec is a
genuinely good *index*; pheasant's vector layer needs a *store*, and the four
things pheasant asks of that store — payload columns, predicate deletes,
full-precision read-back, and concurrent readers on a read-only mount —
are exactly the four turbovec does not offer. The speed and memory gains it
advertises are, for this codebase, either unavailable behind those gaps or
already reachable inside the incumbent.

---

## 1. What the two things actually are

| | **LanceDB** | **turbovec** |
|---|---|---|
| Category | Embedded vector **database** (Lance columnar format) | Vector **index** (Rust + PyO3), a FAISS-class library |
| Storage unit | Table with typed columns; versioned on disk | `uint64 → quantized code`, in RAM |
| Persistence | Table files, MVCC versions, multi-reader | `write()` snapshot / `sync()` delta to `.tv` / `.tvim` |
| Metadata | Arbitrary Arrow columns, SQL predicates | None. External `uint64` ids only |
| Search | Exhaustive, or ANN (`IVF_PQ`, `IVF_RQ`, HNSW) | Exhaustive SIMD scan over quantized codes |
| Quantization | PQ, SQ, RaBitQ (`IVF_RQ`) | TurboQuant, 2-bit or 4-bit |
| License / backing | Apache-2.0, LanceDB Inc. | MIT, one maintainer |

The category difference is the whole finding. `search/vector_store.py` defines a
nine-method `VectorStore` protocol — `upsert`, `delete`, `search`, `count`,
`existing_ids`, `source_chunk_ids`, `reset`, `flush`, plus the informal
`all_vectors`. LanceDB answers eight of the nine with one call each. turbovec
answers two (`search`, `count`) and cannot answer `all_vectors` at all.

---

## 2. Processing speed

### What turbovec claims

Against FAISS `IndexPQFastScan` at 100 K vectors, k=64, from the project's own
benchmarks: **3.5× faster on ARM** and **3.4× on x86 at 4-bit**; 26% and 20%
respectively at 2-bit. Single-vector insert 6.3–19.7 µs (7.6–13.9× FAISS).
Removal 0.44–1.22 µs against FAISS's 0.19–1.02 **seconds** per 100 K corpus —
FAISS has no O(1) delete, turbovec's `IdMapIndex` does. Indexing has no
training phase at all, which is where the paper's 50,000–235,000× indexing
claim comes from: there is no k-means pass to run.

### What is contested

An independent symmetric comparison from VectorDB-NTU re-ran RaBitQ against
TurboQuant and reports RaBitQ **faster to quantize** (4-bit, d=3072, GPU:
0.152 s vs 0.276 s; 0.013 s for the fast-FWHT variant) and states plainly that
“the runtime results reported in the TurboQuant paper could not be reproduced
from the released implementation.” Treat the 3.5× as the vendor's number, not
a measured one.

### What it would mean here — and the trap it exposes

pheasant **never calls `create_index()`**. Grep the tree: there is no
`create_index`, no `IVF`, no `HNSW` anywhere. `LanceDBVectorStore.search()`
issues `table.search(vec).distance_type("cosine").limit(k)` against an
unindexed table, which is a brute-force scan over **float32**. At a
region's sanctioned ceiling (§ 3) that is ~4.4 GB read per query.

So yes — a quantized SIMD scan would be dramatically faster than what runs
today. But the comparison is not *LanceDB vs turbovec*. It is *an unindexed
LanceDB table vs a quantized index*, and the fix for the former is one method
call to the incumbent.

Worse, the query path is not even the bottleneck. Two membership methods —

```python
def _rows(self, columns):
    return table.to_arrow().select(columns).to_pylist()
```

— materialize **the entire table, vectors included**, into Arrow and only then
project. `existing_ids()` is called once per artifact from
`VectorIndexer.index_artifact`. That is O(files × chunks) with the full vector
payload in the constant: the same shape as the `chunks_fts.artifact_id`
full-table scan recorded in § 6 of `CLAUDE.md`, which cost 6.3× on 8,000
files. Swapping the backend would paper over this; it would not fix it, because
turbovec has no membership query either and the id-mapping side table pheasant
would have to build could reproduce it verbatim.

**Speed verdict:** turbovec is faster than the incumbent *as configured*, and
the incumbent is misconfigured. Fix the configuration first; the measurement
that would justify a swap does not exist until then.

---

## 3. Memory size

`capacity.py` fixes the coefficients: `BYTES_PER_CHUNK = 2728`,
`VECTOR_BYTES_PER_CHUNK_PER_DIM = 4`, and `sharding.py` caps one region at
`max_nodes_per_shard = 1_500_000` — with `NODES_PER_FILE = 6.3`, about
**238,000 files**. At `text-embedding-3-small` (d=1536):

| Corpus | Chunks | float32 (today) | turbovec 4-bit | turbovec 2-bit |
|---|---|---|---|---|
| 8 K files (the measured sweep's top) | 16 K | 0.10 GB | 0.013 GB | 0.006 GB |
| 50 K files | 150 K | 0.92 GB | 0.116 GB | 0.058 GB |
| **238 K files (one region's ceiling)** | **715 K** | **4.39 GB** | **0.55 GB** | **0.28 GB** |

The 8–16× is real. What it buys is not.

Those float32 bytes live in `<state>/vectors/<kb_id>/` — **on disk**, in the
state directory `capacity.py` already projects and `pheasant scan` already
warns about. They are not resident RSS. turbovec's 0.28 GB, by contrast, **is**
resident, in every process that opens the index.

That inverts the arithmetic under `docker-compose.scale.yml`, which mounts
`/state:ro` on the API replicas while the indexer writes. LanceDB's answer is
its versioned table format: N replicas share one on-disk copy and read a
consistent version. turbovec's answer is N full copies in N process heaps —
0.28 GB × replicas — plus a reload protocol against a snapshot file being
rewritten underneath them that the project does not document, alongside no
documented thread-safety, no mmap, and no concurrent-reader story. Three
replicas at the region ceiling: LanceDB 4.4 GB on disk / ~0 RSS, turbovec
0.8 GB RSS and an undefined consistency model.

**Memory verdict:** turbovec wins the compression ratio by 8–16× and loses the
deployment question, because it converts a disk cost pheasant already budgets
into an RSS cost multiplied by replica count.

---

## 4. Algorithm compatibility

TurboQuant itself is a fine fit. It is inner-product/cosine on unit-normalized
vectors, which is exactly what `VectorSearcher` does — `search()` already
converts to cosine similarity and maps to `[0, 1]` for RRF. It is data-oblivious
and training-free, so it does not violate the determinism pillar. Quantization
is lossy but reproducible, so idempotency survives: chunk ids stay
content-addressed and re-syncing still embeds nothing.

The integration is where it breaks, in four places:

1. **`all_vectors()` has no implementation.** `synapse/publisher.py` reads full
   chunk vectors to compute the contract's `centroid`, `covariance_diag` and
   k-means clusters. turbovec exposes no reconstruct/decode API — the original
   float32 is discarded at `add()`. You would either keep a parallel float32
   copy (deleting the entire memory argument) or publish a contract computed
   from 2-bit reconstructions, which changes the published signature bytes and
   collides with rule 9's contract-fixture sha256 parity across repos.
2. **No payloads.** `source_id`, `artifact_id`, `text_hash` and `payload_json`
   are columns today. turbovec stores `uint64` and nothing else, so pheasant
   would own a `chunk_id ↔ uint64` mapping table, its allocation, its
   compaction after deletes, and its crash consistency with the `.tv` snapshot.
   Two files that must agree, where there is one today.
3. **No predicate delete.** `delete(artifact_id=...)` is
   `table.delete("artifact_id = '…'")`. Against turbovec it becomes a mapping-table
   lookup plus a `remove()` loop — cheap per call (sub-µs), but only as correct
   as the side table.
4. **`reset()` exists for a reason.** Its docstring records that an emptied
   LanceDB table keeps its `FixedSizeList` width, so a 1536 → 3072 model switch
   fails on the next insert. Any replacement inherits that requirement and its
   test.

There is also a smaller compatibility question in the other direction:
`search(..., allowlist=ids)` filters *inside* the SIMD kernel at 32-vector block
granularity. That is strictly better than what pheasant does today —
over-fetch `k*4` and filter `source_id` in Python. But LanceDB's `.where()`
prefilter offers the same thing, and pheasant doesn't call that either.

**Compatibility verdict:** the algorithm fits; the API does not. Adoption means
pheasant writes the store layer that LanceDB currently provides — id mapping,
payloads, predicate deletes, dimension-change reset, snapshot durability — and
still has no answer for the Synapse contract publisher.

---

## 5. Maturity and who stands behind each

**turbovec.** MIT. First release `0.1.0rc1` on 2026-04-13; `1.0.0` on
2026-08-18 — sixteen releases in four months. One named maintainer (Ryan
Codrai), no company, no published roadmap, no governance model. Wheels for
Windows x64, macOS 11+ arm64, and manylinux 2.28 x64/arm64. GitHub traction is
real (thousands of stars, a Trendshift listing, wide coverage) but is four
months old. The algorithm underneath it is strong — Google Research,
ICLR 2026 — but the *implementation* is a young single-maintainer project, and
the independent reproduction attempt above did not confirm its headline
numbers.

**LanceDB.** Apache-2.0, LanceDB Inc.: $41M raised across three rounds, a $30M
Series A in June 2025, ~54 employees as of mid-2026. 11k+ stars on `lancedb`,
plus the separately-governed `lance` format with a three-tier
contributor/maintainer/PMC structure. Production references include Netflix's
Media Data Lake. Shipping through 2026: Lance-native SQL retrieval via DuckDB,
multi-bucket storage, Hugging Face Hub integration, Git-style branching.

The asymmetry matters more than usual here because of rule 2: **`/state` is
user data.** The vector store owns a directory users back up, restore, migrate
and mount read-only into replicas. A single-maintainer library four months past
its first RC is a defensible dependency for a cache; it is a harder sell for a
format with a `pheasant restore` contract attached to it.

**Maturity verdict:** not close. This is the dimension on which the decision
would be made even if every other dimension favoured turbovec.

---

## 6. Recommendation

**No. Do not replace LanceDB with turbovec.**

Four independent reasons, any one of which is sufficient:

1. **Category mismatch.** turbovec answers two of nine `VectorStore` methods.
   The swap is not a swap; it is pheasant taking ownership of a store layer.
2. **It breaks the Synapse contract publisher.** `all_vectors()` needs
   full-precision vectors that turbovec structurally does not retain, and
   working around it either erases the memory win or moves published contract
   bytes (rule 9).
3. **It regresses the scale story.** Per-replica RSS and an undocumented
   multi-reader model replace one shared, versioned, read-only-mountable
   on-disk table.
4. **The measured gap it would close is self-inflicted.** pheasant runs LanceDB
   unindexed and does two full-table Arrow materializations per artifact. Until
   those are fixed, any benchmark that favours turbovec is measuring pheasant's
   configuration, not LanceDB.

And one that closes the door on the algorithm as a reason to move: LanceDB
already ships RaBitQ as the `IVF_RQ` index type, and the independent NTU
comparison finds RaBitQ matches or beats TurboQuant on inner-product error and
recall at every bit width tested. The quantization benefit is available from
the incumbent, from the vendor, with the competing peer-reviewed algorithm.

### What to do instead

Three changes inside `LanceDBVectorStore`, in descending order of payoff:

1. **Stop materializing the table per artifact.** `existing_ids()` and
   `source_chunk_ids()` should project columns in the scan (`to_arrow(columns=…)`
   or a `where` predicate), or membership should move to SQLite where the chunk
   ids already live. This is the § 6 O(N²) trap, unfixed, in a second place.
2. **Create an ANN index** above a row threshold — `IVF_PQ`, or `IVF_RQ` for
   RaBitQ compression. This is where both the latency win and the 8–32×
   memory win actually live, without a new dependency.
3. **Push the `source_name` filter into `.where()`** instead of over-fetching
   `k*4` and filtering in Python, which silently truncates when one source
   dominates the top-4k.

### What would reopen this

`VECTOR_STORE_PROVIDERS` is a tuple and `build_vector_store` is a dispatch on a
string; a third `provider: turbovec` alongside `numpy` and `lancedb` costs
nothing structurally, and the protocol is exactly the seam for it. Revisit if
**all** of: (a) items 1–3 above are done and a real-corpus benchmark still shows
LanceDB losing materially; (b) turbovec grows a reconstruct API or the contract
publisher stops needing full-precision vectors; (c) it grows a documented
multi-reader/mmap story, or the region ceiling stops mattering; (d) it has
more than one maintainer, or a company. Until then it is a fast index that
solves a problem pheasant has not yet given itself.

---

## Sources

- turbovec — [GitHub](https://github.com/RyanCodrai/turbovec), [PyPI](https://pypi.org/project/turbovec/)
- TurboQuant — [Google Research](https://research.google/blog/turboquant-redefining-ai-efficiency-with-extreme-compression/), [ICLR 2026 paper](https://openreview.net/pdf/6593f484501e295cdbe7efcbc46d7f20fc7e741f.pdf)
- Independent reproduction — [VectorDB-NTU/rabitq-turboquant-comparison](https://github.com/VectorDB-NTU/rabitq-turboquant-comparison)
- LanceDB — [lancedb/lancedb](https://github.com/lancedb/lancedb), [Series A](https://www.lancedb.com/blog/series-a-funding), [RaBitQ quantization](https://www.lancedb.com/blog/feature-rabitq-quantization)
