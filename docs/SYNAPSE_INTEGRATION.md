# pheasant as a Synapse region

!!! abstract "For consumers — start here"
    pheasant can act as a **region** in a **Synapse** fleet: it publishes a
    bounded **semantic contract** describing what it knows, and a Synapse
    router uses that contract to route global, cross-region queries to it.

    - **Want to attach your KB to a fleet?** Read the task-focused guide:
      [Attach to a Synapse fleet](how-to/attach-to-synapse.md). That's all most
      consumers need.
    - **Standalone is the default.** Every Synapse setting is off unless you opt
      in via the `synapse:` config block; a router-less pheasant is unchanged.
    - **The global search experience** (routing, fan-out, merge, cross-region
      "white matter") lives on the router —
      [pheasant-flock](https://github.com/esatt10/pheasant-flock).

    Everything **below this box is the internal, contributor-facing spec**: the
    region-side contract obligations and the Phase-21 region-hardening step
    contracts. Consumers can safely stop here.

---


## Decision 2026-08-03 — contract vocabulary now comes from the FTS index

`vocabulary.top_concepts` and its MinHash used to be read from
`artifact_terms` rows of `node_type='concept'`. Concept extraction was retired
this day (see `docs/graph_model.md` and `graph.enrichment._add_concept`), so
that source no longer exists.

**The wire format is unchanged.** `top_concepts` keeps its shape and weight
scale, the MinHash is computed over the same kind of term set, and the router
scores contracts exactly as before — so the vendored schema and fixtures are
untouched, there is no schema bump, and `tests/test_contract_parity.py` stays
green without a re-vendor. This is a change of *source*, not of contract.

Terms now come from `chunks_vocab`, an `fts5vocab` view over the FTS index.
That is strictly better provenance for a region advertising what it knows: the
vocabulary is literally what is searchable in the region, it cannot drift from
the index, and it costs no storage — SQLite already maintains the term →
document-frequency table. Weights are document frequencies normalized to
(0, 1] against the most common term, so they stay comparable across regions of
different sizes. Ordering by *document* frequency rather than raw count is
deliberate: a term repeated 400 times in one file describes that file, while a
term appearing once in 400 files describes the corpus.

Rule 6 note: the contract schema remains canonical in pheasant-flock and
nothing under `contracts/` was hand-edited.

## Internal spec — pheasant as a Brain Region

**Status:** authoritative pheasant-side spec (2026-06-10). The system-wide
design lives in the **pheasant-flock** repository:
`docs/SYNAPSE_ARCHITECTURE.md` (architecture) and
`docs/SYNAPSE_FRAMEWORK.md` (execution plan, phases 20–26). This document
mirrors **Phase 21 — region hardening**, which executes *here*, plus the
cross-repo contract obligations.

---

## 1. pheasant's role in Synapse

Synapse is a hyperfast federated knowledge-base system. In its brain
metaphor:

- **pheasant instances are the regions**: each container owns one
  specialized knowledge base — sync engine, SQLite/FTS5 state, knowledge
  graph, self-search, MCP/HTTP surfaces. Sizes range from single-digit MB
  to multi-TB. Regions are fully self-contained and deploy standalone.
- **Pheasant Flock is the nervous system**: a router that decides
  *which regions to ask* by scoring each region's published **semantic
  contract**, fans the query out to the chosen regions' self-search, and
  merges/re-ranks the answers.
- **The semantic contract** is the artifact a region derives from its own
  content and publishes after every successful sync: embedding-space
  signature (centroid, covariance diagonal, ≤32 cluster centroids),
  concept vocabulary (from this repo's graph concepts) + MinHash,
  capability descriptor, freshness watermark. Bounded ≤ 256 KB.

Two integration invariants:

1. **The contract schema is owned by pheasant-flock.** Its Pydantic
   model is canonical; this repo vendors only the exported JSON Schema
   under `contracts/` plus golden fixtures, with a CI parity test
   (sha256 equality with the other repo's fixtures). Never hand-edit the
   vendored files.
2. **No Python dependency between the repos.** The boundary is the
   contract JSON + HTTP. A region must never import pheasant-flock;
   the router never imports pheasant. Regions must keep working with no
   router configured (all Synapse behavior is a no-op when
   `synapse.router_url` is unset).

---

## 2. Contract obligations (quick reference)

| Obligation | Where |
|---|---|
| Vendored JSON Schema | `contracts/semantic_contract.v<N>.schema.json` (do not edit; re-vendor from pheasant-flock) |
| Golden fixtures | `contracts/fixtures/*.json` — byte-identical with the other repo (`tests/test_contract_parity.py`) |
| Publisher | `src/pheasant/synapse/publisher.py` (Step 21.5) |
| Serving | `GET /contract` + MCP resource |
| Push | `POST <router>/v1/synapse/events` on `sync.completed` |
| Embedding space | The region publishes its own `embedding_space` (model/dim) in the contract; since 2026-07-11 the router routes **heterogeneous fleets** by partitioning per space, so regions with different models/dims coexist. Only when the router opts into the `synapse.embedding_space` pin must `search.embeddings.model` equal it (HTTP 409 otherwise) |
| Signing (optional, Step 24.4) | `synapse.signing_key_ref` → `src/pheasant/synapse/signing.py` Ed25519-signs `integrity.signature`; router rejects (HTTP 403) under `require_signed` |

### Heterogeneous embedding spaces (2026-07-11, [x-repo] — router-side; pheasant docs-only)

The fleet no longer requires all regions to share one embedding model/dim.
The router (pheasant-flock, ADR 2026-07-11 in its `docs/DECISIONS.md`)
now **partitions** registered contracts by `embedding_space
(model_id, dim, normalized)` and scores each partition with a query vector in
that space (per-space query embedders under its `synapse.spaces` config, or
explicit per-space `query_vecs`); regions whose space cannot be resolved for a
query are excluded with `embedding_space_unresolved` in the routing report.
Cross-space math remains forbidden — white-matter edges are confirmed within
one space only.

**Region-side impact: none.** pheasant already embeds with its own configured
model (`search.embeddings`) and publishes its own `embedding_space` in the
contract; the **wire format is unchanged** (no schema bump, no re-vendor,
`tests/test_contract_parity.py` green). Operators may now mix regions on
local, OpenAI-spec, or Gemini(-compatible) embedding models in one fleet —
just make sure the router config carries a `synapse.spaces` entry (or a
matching default provider) for each `model_id` the fleet's regions report, so
text queries can be embedded into every space.

### Step 24.4 — Ed25519-signed contracts + A2A (2026-06-20, [x-repo])

A region can **optionally sign** its semantic contract so the router can verify
authenticity/integrity beyond the `content_hash`:

- **Config:** set `synapse.signing_key_ref` to a secret *reference*
  (`env://NAME` or a bare env-var name). The referenced value is the base64 of a
  32-byte raw Ed25519 private seed — the plaintext key never lands in YAML or on
  disk. **Unset (default) → unsigned** (`integrity.signature: null`); a
  **standalone, router-less pheasant is entirely unchanged**.
- **What gets signed:** the *exact same canonical body bytes* the
  `integrity.content_hash` covers (body with `integrity` excluded,
  `sort_keys=True`, compact separators, `ensure_ascii=False`). The signature
  lives outside the hashed body, so signing never perturbs the content hash.
  `src/pheasant/synapse/signing.py` (`sign_body`/`signing_bytes`) is the
  region-side codec; it is byte-compatible with the router's
  `SemanticContract.verify_signature`, guarded by the cross-repo signing-parity
  fixture `contracts/fixtures/signed-demo-region.v1.contract.json` (+ PARITY).
- **Out-of-band public key (decision):** the router holds the kb_id→public-key
  trust store in *its own* config (`synapse.trust.keys`) and enforces
  `synapse.require_signed`. The public key is **not** added to the contract, so
  the **contract wire format / vendored JSON Schema are unchanged** — no
  schema-version bump, no re-vendor.
- **Optional dependency:** the `cryptography` import is gated behind the new
  `[a2a]` extra (`pip install 'pheasant-kb[a2a]'`). A region without
  `signing_key_ref` needs no crypto dep; the offline suite passes without it.

### Step 25.4 session A — multi-modal: image ingest (2026-06-21, [x-repo])

A region can ingest **images** (`.png`/`.jpg`/`.jpeg`/`.webp`/`.gif`) by
**captioning** them into indexable text (architecture §8: project everything
into the *one* fleet-pinned text embedding space; modality-native vectors like
CLIP would stay region-local and are out of scope). The caption becomes the
artifact's text and flows through the normal chunk → embed → graph path like
any other document. **Image only this session — audio (transcribe-then-index)
is session B.**

- **Captioner abstraction:** `src/pheasant/ingestion/captioner.py`.
  `StubCaptioner` is the **default + offline** path (deterministic caption from
  the file name + a blake2b digest of the image bytes, so the same image always
  captions identically and different images differ; tests use it, no network /
  no decoder / no model). `OpenAISpecVisionCaptioner` is the gated production
  path — OpenAI-spec `POST {base_url}/chat/completions` with an `image_url`
  content part (data-URI base64), caption read from
  `choices[0].message.content`. An authored sidecar `<image>.caption.txt`
  always wins (offline real captions for fixtures/demos). Captioning is the
  **only** sanctioned indexing-path network call besides the 21.4 embedder, and
  like it must keep the stub path.
- **Config:** `ingestion.captioner.{provider,model,base_url,api_key_env,prompt}`
  — `provider: stub` (default) or `openai-spec`. The API key is read from the
  named env var at call time, never stored. The captioner is **only built when
  a source's `include` globs admit an image extension** (e.g. `**/*.png`), so a
  text-only region is byte-identical to pre-25.4 (no captioner, no possible
  network call). The `stub` default needs no extra dependency.
- **Idempotency:** the engine's pre-read sha256 skip
  (`_can_skip_before_read`) compares the image's content hash *before* reading
  bytes, so an unchanged image in an incremental sync is **never re-captioned**
  — the same zero-work guarantee the embedder gets (21.4).
- **Modalities wiring (contract):** the 21.5 publisher's `_capabilities()`
  appends `"image"` to `capabilities.modalities` when an image source is
  configured. The router (pheasant-flock) already filters by
  `--modality image` *before* scoring (22.1), so an image query routes only to
  image-capable regions. **`modalities` is existing contract data — the wire
  format / vendored JSON Schema are UNCHANGED** (no schema bump, no re-vendor,
  parity test green).
- **Tests:** `tests/test_image_ingestion.py` (caption searchable; artifact
  typed `image`; zero re-caption on unchanged re-sync; text-only region builds
  no captioner; contract advertises `image`). Router-filter test on the Flock side
  (`tests/synapse/test_router.py::test_modality_image_routes_only_to_image_capable_regions`).

### Step 25.4 session B — multi-modal: audio ingest (2026-06-21, [x-repo]) — COMPLETES Step 25.4 + Phase 25

A region can ingest **audio** (`.wav`/`.mp3`/`.m4a`/`.flac`/`.ogg`) by
**transcribing** it into indexable text — the audio twin of session A's image
captioning, same architecture §8 principle (project into the *one* fleet-pinned
text embedding space; modality-native audio vectors stay region-local, out of
scope). The transcript becomes the artifact's text and flows through the normal
chunk → embed → graph path. The transcriber and captioner share a tiny additive
helper `src/pheasant/ingestion/_modal.py` (`sidecar_text` + `stub_fingerprint`)
so they stay in lock-step; session A's observable behavior is unchanged.

- **Transcriber abstraction:** `src/pheasant/ingestion/transcriber.py`.
  `StubTranscriber` is the **default + offline** path (deterministic transcript
  from the file name + a blake2b digest of the audio bytes — same file always
  transcribes identically, different audio differs; tests use it, **no network /
  no audio decoder / no ASR model / no audio library**). `OpenAISpecTranscriber`
  is the gated production path — OpenAI-spec `POST {base_url}/audio/transcriptions`
  as a stdlib-urllib multipart upload (`model` + raw `file` bytes), transcript
  read from the response `text` field. An authored sidecar
  `<audio>.transcript.txt` always wins (offline real transcripts for
  fixtures/demos). Transcription is a sanctioned indexing-path network call
  alongside the 21.4 embedder and the 25.4A captioner, and like them keeps the
  stub path so the suite is network-free.
- **Config:** `ingestion.transcriber.{provider,model,base_url,api_key_env}` —
  `provider: stub` (default; `model: whisper-1`) or `openai-spec`. The API key is
  read from the named env var at call time, never stored. The transcriber is
  **only built when a source's `include` globs admit an audio extension** (e.g.
  `**/*.wav`), so a text-only / standalone region is byte-identical to pre-25.4
  (no transcriber, no possible network call). The `stub` default needs **no
  extra dependency**.
- **Idempotency:** the engine's pre-read sha256 skip (`_can_skip_before_read`)
  compares the audio's content hash *before* reading bytes, so an unchanged audio
  file in an incremental sync is **never re-transcribed** — the same zero-work
  guarantee the embedder (21.4) and image captioner (25.4A) get.
- **Modalities wiring (contract):** the 21.5 publisher's `_capabilities()`
  appends `"audio"` to `capabilities.modalities` when an audio source is
  configured. The router (pheasant-flock) already filters by
  `--modality audio` *before* scoring (22.1), so an audio query routes only to
  audio-capable regions. **`modalities` is existing contract data — the wire
  format / vendored JSON Schema are UNCHANGED** (no schema bump, no re-vendor,
  parity test green).
- **Tests:** `tests/test_audio_ingestion.py` (transcript searchable; artifact
  typed `audio`; zero re-transcribe on unchanged re-sync; text-only region builds
  no transcriber; contract advertises `audio`); fixture
  `tests/fixtures/sample_workspace/audio/briefing.wav` + `.transcript.txt`
  sidecar (a few bytes, no real decoder). Router-filter test on the Flock side
  (`tests/synapse/test_router.py::test_modality_audio_routes_only_to_audio_capable_regions`).

### Decision note 2026-08-06 — `"document"` modality (PDF/DOCX extraction)

Document text extraction (`src/pheasant/ingestion/extractor.py`) closed a gap
where `.pdf`/`.docx` were accepted by the pipeline and then produced no text at
all — see the 2026-08-06 entry in `CLAUDE.md` and
`runs/2026-08-06-pdf-extraction/SUMMARY.md`. The only Synapse-visible
consequence is one more entry in an existing contract field:

- The 21.5 publisher's `_capabilities()` appends `"document"` to
  `capabilities.modalities` when a source's `include` globs admit any of the
  **seven** extractable document extensions — `.pdf`, `.docx`, `.doc`,
  `.pptx`, `.xlsx`, `.rtf`, `.epub` — so a router's `--modality document`
  filter (22.1) routes document questions only to regions that can actually
  read them. One modality covers all seven deliberately: from the router's
  point of view "can this region read a document?" is the routable question,
  and a per-format modality (`"pptx"`, `"epub"`, …) would push format
  dispatch into the fleet contract for no routing benefit. A region that gains
  a format therefore needs **no** contract or router change.
- **`modalities` is existing contract data — the wire format / vendored JSON
  Schema are UNCHANGED** (no schema bump, no re-vendor, parity test green).
  This follows the 25.4 image/audio and 33.1 memory precedents exactly, so it
  carries **no `[x-repo]` obligation**: the router needs no change to honor it,
  because `--modality` already filters on whatever strings a contract declares.
- Extraction adds **no network call** to the indexing path — unlike the
  captioner/transcriber, every provider is offline and deterministic, so the
  rule-1 determinism guarantee is unaffected and there is nothing new to gate.
- Regions ingesting PDFs from untrusted connector sources can set
  `ingestion.extractor.provider: sandboxed` to run the PDF tokenizer inside the
  Phase-34 WASM sandbox (fuel + memory cap, zero host capabilities). This is a
  region-local hardening choice with no contract or routing impact.

## 3. Deployment notes

A region remains the existing container (`Dockerfile`, port 8765, PVC on
`/state`). In a Synapse fleet:

- **Compose** (Synapse Step 25.1, **landed 2026-06-20** in the router repo):
  the `docker compose --profile synapse` topology runs 1 router + 3 demo
  pheasant regions, each with `synapse.publish: true` +
  `synapse.router_url` → the router. **No pheasant code change** was needed:
  the region image is built unmodified from this repo's `Dockerfile` (sibling
  build context `../pheasant`, or `PHEASANT_IMAGE` pinned tag), and the three
  fleet-demo region configs + fixture workspaces are **vendored on the router
  side** (`pheasant-flock/deploy/synapse-demo/`) and mounted into the
  container at `/config/pheasant.yaml` + `/workspace`. Regions sync on startup
  (21.1) and publish their contract over the 21.5 webhook; the router's
  file-backed registry fills over HTTP (no shared volume). Standalone pheasant
  is unchanged — drop `synapse.publish`/`router_url` and the region is
  router-less again. See `pheasant-flock/docs/DEPLOY.md` §11.
- **Kubernetes** (Synapse Step 25.2, **landed 2026-06-20** in the router
  repo): the router repo's sibling Helm chart
  `pheasant-flock/deploy/helm/synapse/` renders the whole fleet — one
  router (`Deployment` + HPA + `Service`) plus a values-driven `regions:`
  list where **each entry becomes one `StatefulSet` + a `/state` PVC
  (`volumeClaimTemplate`, so each region owns its own volume — independent
  scale-up) + a headless `Service` + a `ConfigMap`**. **No pheasant code
  change** was needed: the region pod spec in the chart **mirrors this
  repo's `deploy/kubernetes/` manifests** (port 8765, `/health`+`/ready`
  probes, `/state`+`/config`+`/workspace`+`/exports` mounts,
  non-root 10001, read-only rootfs) — those manifests remain the pod-spec
  baseline; the chart vendors that shape on the router side (chart values),
  the same boundary as the 25.1 compose configs. Regions publish their
  contract to the router webhook (21.5) over the headless Service DNS name;
  standalone pheasant is unchanged. The live `helm template | kubeconform`
  + kind smoke is a runbook in `pheasant-flock/docs/DEPLOY.md` §12
  (the router-repo build env had no helm binary).
- Auth: regions accept a bearer token (`security` settings) minted by the
  router's tenancy layer; local/demo fleets may run open.

---
