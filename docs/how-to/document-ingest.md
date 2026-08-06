# How to ingest PDFs and Word documents

pheasant indexes `.pdf` and `.docx` by **extracting** their text, which then
flows through the normal chunk → embed → graph path like any other document.

## The symptom this fixes

If PDFs seem to be "indexed" but you can never find anything *inside* them,
that is the behavior you get without an extractor configured. A `.pdf` is
accepted by the pipeline, hashed, typed `document` and given a graph node — and
contributes **zero chunks**. It is findable by its path and invisible by its
content.

Quickest way to tell:

```bash
# Chunk count per artifact. A document with 0 chunks contributed no text.
sqlite3 /state/pheasant.db "
  SELECT a.relative_path, COUNT(c.id) AS chunks
  FROM artifacts a LEFT JOIN chunks c ON c.artifact_id = a.id
  WHERE a.type = 'document'
  GROUP BY a.relative_path;"
```

## Turn it on

Extraction is **opt-in by file extension** — the extractor is only built when a
source's `include` globs admit `.pdf` or `.docx`. The default `include` list is
code/markdown/config only, so add the extensions you want:

```yaml
sources:
  - name: handbooks
    type: document_folder
    path: /workspace/handbooks
    include:
      - "**/*.pdf"      # builds the extractor
      - "**/*.docx"
```

That is enough — `provider: auto` is the default. To be explicit:

```yaml
ingestion:
  extractor:
    provider: auto      # auto | native | builtin | sandboxed
    html_text: false    # strip tags from .html/.htm/.xhtml (off by default)
```

Then sync and search for something only the document says:

```bash
pheasant sync --source handbooks --mode full
```

## Three sources of text (in priority order)

1. **Sidecar file** — if `<file>.extract.txt` exists next to the document, its
   contents are used **verbatim** and no extractor runs. This is how you give a
   scanned, image-only PDF real searchable text with no OCR engine:

   ```
   handbooks/scanned-policy.pdf
   handbooks/scanned-policy.pdf.extract.txt   ← used verbatim
   ```

2. **Configured provider** — otherwise the `provider` runs.
3. **Nothing** — a document that cannot be read contributes no text. It never
   raises, and never aborts a sync.

## Choosing a provider

| Provider | Use when |
|---|---|
| `auto` (default) | Your documents. Best fidelity available, with a pure-stdlib safety net. |
| `native` | You want `pymupdf`/`python-docx` specifically (CID/Type0 fonts, complex layout). |
| `builtin` | You need zero third-party imports in the extraction path. |
| `sandboxed` | The PDFs come from somewhere you don't control. |

### Sandboxed extraction for untrusted PDFs

PDF is a classic hostile-input parser target, and PDFs arriving through
connectors (Google Drive, Slack, Confluence, IMAP attachments) are not authored
by you. Parsed in-process, that work runs with the sync worker's ambient
authority: every configured connector's API token in the environment, a
writable `/state`, and network egress.

```yaml
ingestion:
  extractor:
    provider: sandboxed
```

```bash
pip install 'pheasant-kb[wasm]'
```

This runs the PDF tokenizer inside the WASM sandbox with a fuel cap, a
linear-memory cap, and **no host capabilities at all** — no environment, no
filesystem, no network for a subverted parse to reach. A file that exhausts its
budget yields no text for that file and logs; the rest of the document, and the
rest of the sync, continue.

Two things worth knowing before you choose it:

- **It fails loudly, by design.** Without the `[wasm]` extra it raises with an
  install hint instead of quietly extracting unsandboxed. A security control
  that silently degrades is worse than one that refuses to start.
- **It is a partial sandbox, honestly labelled.** The host still runs `zlib` to
  inflate content streams (bounded against decompression bombs); the guest runs
  the *tokenizer*, which is where attacker-controlled bytes drive unbounded
  loops and index arithmetic. Only PDF is sandboxed — DOCX and HTML use
  memory-safe Python parsers where wrapping would add cost for no gain.

It is also not a speed penalty. The tokenizer is a tight byte loop, so the
compiled guest is roughly **3x faster** than the pure-Python `builtin` reader
on a typical document and ~11x faster on the scan itself. See the fidelity
trade-off in [Configuration](../configuration.md#ingestionextractor-pdfdocx-text)
— `sandboxed`/`builtin` do not handle encrypted PDFs or Type0/CID font CMaps.

## HTML

`.html` and `.xml` have always been indexed as **raw markup** — tags,
`<script>` bodies and CSS included as if they were prose. Turning on
`html_text` strips them:

```yaml
ingestion:
  extractor:
    html_text: true
```

It defaults to off because enabling it changes the indexed text, and therefore
chunk boundaries, of an existing knowledge base. Enable it deliberately and
re-sync with `--mode full`.

## Federation note

A region configured for documents advertises `"document"` in its semantic
contract's `capabilities.modalities`, so a Synapse router's
`--modality document` filter routes document questions only to regions that can
actually read them. This is existing contract data — no wire-format change.

## Troubleshooting

**A PDF still has 0 chunks.** Check the source's `include` actually admits
`**/*.pdf` (the extractor is not built otherwise), then check the logs for
`document extraction failed`. An image-only scan genuinely has no text — use an
`.extract.txt` sidecar.

**Text comes out garbled.** Likely a Type0/CID font under `builtin` or
`sandboxed`. Switch to `auto`/`native`.

**`provider: sandboxed` raises `WasmRuntimeUnavailable`.** Install the extra:
`pip install 'pheasant-kb[wasm]'`. This is intentional, not a bug.

**Re-syncing re-extracts everything.** It should not: the engine's pre-read
sha256 check skips unchanged files before reading their bytes, so an unchanged
document is never re-extracted. If it is re-extracting, the file's content hash
is changing between syncs.
