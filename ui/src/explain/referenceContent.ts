// Markdown reference for the Explain panel. Numerical behavior is written in
// LaTeX (rendered by KaTeX) and transcribes logic that exists in the SyncSage
// codebase, so the documentation stays faithful to the implementation.

export const referenceMarkdown = `# How SyncSage works

SyncSage turns project sources into a queryable **knowledge graph**. This panel
explains each part of the UI and the numerical behavior behind it. Toggle
**Explain mode** to also get hover tooltips on individual components.

## Knowledge graph

Nodes are \`source\`, \`file\`/\`document\`, \`chunk\`, \`symbol\`, \`entity\`,
\`concept\` and \`external_reference\`; edges include \`contains\`, \`indexes\`,
\`has_chunk\`, \`mentions\`, \`imports\`, \`calls\`, \`references\` and
\`similar_to\`. You navigate top-down through abstraction levels:

$$\\text{knowledge\\_base} \\to \\text{source} \\to \\text{file} \\to \\{\\text{chunk}, \\text{symbol}, \\text{concept}\\}$$

### Traversal depth

Expanding a node walks the graph breadth-first to a bounded depth:

$$d \\in [1, 10], \\qquad d = \\max\\!\\big(1, \\min(\\text{depth}, 10)\\big)$$

The frontier at depth \`d\` from node \`v\` is the set of nodes reachable by at
most \`d\` matching edges, optionally filtered to an allowed edge-type set
\`E\`.

## Chunking

Each artifact is split into overlapping chunks. With chunk size \`C\`
(\`max_chars\`) and overlap \`O\` (\`overlap_chars\`), the stride and the chunk
count for a document of length \`L\` characters are:

$$\\text{stride} = C - O, \\qquad N_{\\text{chunks}} = \\left\\lceil \\frac{L - O}{C - O} \\right\\rceil$$

Larger overlap \`O\` increases redundancy and total chunk count for the same
\`L\`.

## Idempotent sync

A file is reprocessed only when its identity tuple changes — size \`s\`, mtime
\`m\`, and content hash \`H\`:

$$\\text{reindex} \\iff (s, m, H) \\neq (s', m', H'), \\qquad H = \\mathrm{SHA\\text{-}256}$$

This is the \`compare_size_mtime_hash\` + \`skip_unchanged_files\` behavior.

## Watcher debounce and batching

For a burst of filesystem events, a change is dispatched after the debounce
window \`τ_d\` (\`debounce_ms\`) of quiet, and events are coalesced within the
batch window \`τ_b\` (\`batch_window_ms\`):

$$t_{\\text{dispatch}} = t_{\\text{last event}} + \\tau_d, \\qquad \\text{batch} = \\{\\, e_i : t_{e_i} \\le t_0 + \\tau_b \\,\\}$$

## Hybrid ranking

Search blends keyword relevance with graph-derived term expansion and the
configured boosts:

$$\\mathrm{score}(d) = \\alpha\\,\\mathrm{kw}(d) + \\beta\\!\\!\\sum_{t \\in \\mathcal{G}(q)}\\!\\!\\mathrm{w}(t, d) + \\gamma\\,b_{\\text{path}}(d) + \\delta\\,b_{\\text{recent}}(d) + \\varepsilon\\,b_{\\text{neighbor}}(d)$$

where \`G(q)\` is the set of graph-expanded terms for query \`q\`, and the boost
terms map to \`prefer_exact_path_matches\`, \`prefer_recent_commits\` and
\`graph_neighbor_boost\`.

## Concept similarity

Two artifacts \`a\` and \`b\` are linked with a \`similar_to\` edge when the
Jaccard overlap of their normalized concept sets clears a threshold \`θ\`:

$$J(a, b) = \\frac{|\\,\\mathcal{C}_a \\cap \\mathcal{C}_b\\,|}{|\\,\\mathcal{C}_a \\cup \\mathcal{C}_b\\,|}, \\qquad a \\xrightarrow{\\;\\text{similar\\_to}\\;} b \\iff J(a, b) \\ge \\theta$$

## Configuration

The effective config is layered:

$$\\text{effective} = \\text{base defaults} \\oplus \\text{profile} \\oplus \\text{YAML} \\oplus \\text{overrides}$$

Every section — instance, server, storage, search, sync, obsidian, security and
sources — is editable here, and you can always drop to raw YAML. Adding a source
from a local directory registers it at runtime; **Promote to YAML** makes it
durable.
`;
