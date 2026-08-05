//! Synapse Step 34.5: WASM-accelerated hot loops.
//!
//! Faithful Rust ports of two pure-Python functions, compiled to
//! `wasm32-unknown-unknown` and run inside the existing 34.1 sandbox
//! harness. Both are pure, deterministic, input->output transforms with
//! **no knowledge-base-specific data baked in** — the same compiled
//! `.wasm` binary works for every knowledge base; graph data is marshaled
//! in as a function argument on every call, the same way any compiled
//! Python extension (e.g. a C accelerator module) is a single generic
//! binary shared across all callers. This module ships as a vendored
//! package asset (`accel.wasm`, built from this crate), not as
//! deployment-specific configuration — see `docs/SYNAPSE_INTEGRATION.md`
//! §5 Step 34.5 for the full rationale and `loader.py` for how a fresh
//! process avoids paying full JIT-compilation cost on every call (AOT
//! module serialization, `wasmtime.Module.serialize`/`deserialize`).
//!
//! - `resolve_cross_source` mirrors `graph.enrichment.resolve_cross_source_edges`
//!   exactly, including both the `python_import` and `document_link`/`url`
//!   reference-type paths (the 34.4 spike ported only the former; this is
//!   the full production port — silently dropping the link-resolution path
//!   behind a config flag would be a correctness regression, not just a
//!   performance change).
//! - `scan_edges` mirrors `search.graph_search._scan_edges` exactly.
//!
//! Any change here must stay byte-for-byte equivalent to its Python
//! counterpart — `tests/test_wasm_accel.py` asserts parity on every
//! release, the same discipline the 34.4 benchmark spike used to validate
//! this port in the first place.

use std::alloc::{alloc, Layout};
use std::collections::{HashMap, HashSet};
use std::mem::forget;
use std::slice;

// ---- shared little-endian binary reader/writer -----------------------------

struct Reader<'a> {
    data: &'a [u8],
    pos: usize,
}

impl<'a> Reader<'a> {
    fn new(data: &'a [u8]) -> Self {
        Reader { data, pos: 0 }
    }
    fn u32(&mut self) -> u32 {
        let v = u32::from_le_bytes(self.data[self.pos..self.pos + 4].try_into().unwrap());
        self.pos += 4;
        v
    }
    fn string(&mut self) -> String {
        let len = self.u32() as usize;
        let s = String::from_utf8_lossy(&self.data[self.pos..self.pos + len]).into_owned();
        self.pos += len;
        s
    }
    fn opt_string(&mut self) -> Option<String> {
        let s = self.string();
        if s.is_empty() {
            None
        } else {
            Some(s)
        }
    }
}

struct Writer {
    buf: Vec<u8>,
}

impl Writer {
    fn new() -> Self {
        Writer { buf: Vec::new() }
    }
    fn u32(&mut self, v: u32) {
        self.buf.extend_from_slice(&v.to_le_bytes());
    }
    fn u8(&mut self, v: u8) {
        self.buf.push(v);
    }
    fn f64(&mut self, v: f64) {
        self.buf.extend_from_slice(&v.to_le_bytes());
    }
    fn string(&mut self, s: &str) {
        self.u32(s.len() as u32);
        self.buf.extend_from_slice(s.as_bytes());
    }
    fn opt_string(&mut self, s: &Option<String>) {
        self.string(s.as_deref().unwrap_or(""));
    }
    fn into_leaked(self) -> (*const u8, u32) {
        let len = self.buf.len() as u32;
        let boxed = self.buf.into_boxed_slice();
        let ptr = boxed.as_ptr();
        forget(boxed);
        (ptr, len)
    }
}

#[no_mangle]
pub extern "C" fn wasm_alloc(len: usize) -> *mut u8 {
    if len == 0 {
        return std::ptr::null_mut();
    }
    let layout = Layout::from_size_align(len, 1).unwrap();
    unsafe { alloc(layout) }
}

fn norm_rel(value: &str) -> String {
    value.replace('\\', "/").trim_matches('/').to_lowercase()
}

// ---- resolve_cross_source_edges --------------------------------------------

#[derive(Clone)]
struct ArtifactRef {
    node_id: String,
    source_id: String,
    relative_path: String,
}

fn match_suffix(candidate: &str, by_path: &HashMap<String, Vec<ArtifactRef>>) -> Vec<ArtifactRef> {
    let candidate = norm_rel(candidate);
    if let Some(direct) = by_path.get(&candidate) {
        if !direct.is_empty() {
            return direct.clone();
        }
    }
    let needle = format!("/{candidate}");
    let mut matches: Vec<ArtifactRef> = Vec::new();
    for (path, refs) in by_path.iter() {
        if path == &candidate || path.ends_with(&needle) {
            matches.extend(refs.iter().cloned());
        }
    }
    matches.sort_by(|a, b| {
        (&a.source_id, &a.relative_path, &a.node_id).cmp(&(&b.source_id, &b.relative_path, &b.node_id))
    });
    matches
}

fn resolve_python_import(module: &str, by_path: &HashMap<String, Vec<ArtifactRef>>) -> Vec<ArtifactRef> {
    let normalized = module.replace('\\', "/");
    let parts: Vec<&str> = normalized.split('.').filter(|p| !p.is_empty()).collect();
    if parts.is_empty() {
        return Vec::new();
    }
    let base = parts.join("/");
    for candidate in [format!("{base}.py"), format!("{base}/__init__.py")] {
        let matches = match_suffix(&candidate, by_path);
        if !matches.is_empty() {
            return matches;
        }
    }
    Vec::new()
}

/// Mirrors `_resolve_document_link` exactly, including Python's
/// `str.lstrip("./")` character-SET semantics (strip every leading `.` or
/// `/`, not the two-character literal prefix once) — a naive
/// `trim_start_matches("./")` port would silently under-strip a path like
/// `"../../foo.md"` and mismatch the reference.
fn resolve_document_link(target: &str, by_path: &HashMap<String, Vec<ArtifactRef>>) -> Vec<ArtifactRef> {
    if target.starts_with("http://") || target.starts_with("https://") || target.starts_with("mailto:") {
        return Vec::new();
    }
    let cleaned = target.split('#').next().unwrap_or("");
    let cleaned = cleaned.split('?').next().unwrap_or("").trim();
    if cleaned.is_empty() {
        return Vec::new();
    }
    let cleaned = cleaned.trim_start_matches(|c: char| c == '.' || c == '/');
    let norm = norm_rel(cleaned);
    if let Some(direct) = by_path.get(&norm) {
        if !direct.is_empty() {
            return direct.clone();
        }
    }
    // Obsidian-style wiki link without extension -> try common doc suffixes.
    let name = norm.rsplit('/').next().unwrap_or(&norm);
    if !name.contains('.') {
        for suffix in [".md", ".txt"] {
            let candidate = format!("{norm}{suffix}");
            if let Some(matches) = by_path.get(&candidate) {
                if !matches.is_empty() {
                    return matches.clone();
                }
            }
        }
    }
    match_suffix(&norm, by_path)
}

fn resolve_reference(reference: &str, reference_type: &Option<String>, by_path: &HashMap<String, Vec<ArtifactRef>>) -> Vec<ArtifactRef> {
    match reference_type.as_deref() {
        Some("python_import") => resolve_python_import(reference, by_path),
        Some("document_link") | Some("url") => resolve_document_link(reference, by_path),
        _ => Vec::new(),
    }
}

#[no_mangle]
pub extern "C" fn resolve_cross_source(input_ptr: *const u8, input_len: usize, out_len_ptr: *mut u32) -> *const u8 {
    let input = unsafe { slice::from_raw_parts(input_ptr, input_len) };
    let mut r = Reader::new(input);

    let artifact_count = r.u32();
    let mut by_path: HashMap<String, Vec<ArtifactRef>> = HashMap::new();
    for _ in 0..artifact_count {
        let node_id = r.string();
        let relative_path = r.string();
        let source_id = r.string();
        by_path
            .entry(norm_rel(&relative_path))
            .or_default()
            .push(ArtifactRef { node_id, source_id, relative_path });
    }

    let ext_count = r.u32();
    let mut ext_index: HashMap<String, (String, String)> = HashMap::new();
    for _ in 0..ext_count {
        let ext_id = r.string();
        let source_id = r.string();
        let reference = r.string();
        ext_index.insert(ext_id, (source_id, reference));
    }

    let edge_count = r.u32();
    #[allow(clippy::type_complexity)]
    let mut out: Vec<(String, String, String, String, String, String, Option<String>, bool)> = Vec::new();
    let mut seen: HashSet<(String, String, String)> = HashSet::new();
    for _ in 0..edge_count {
        let artifact_id = r.string();
        let ext_id = r.string();
        let edge_type = r.string();
        let reference_type = r.opt_string();

        let Some((source_id, reference)) = ext_index.get(&ext_id) else { continue };
        let targets = resolve_reference(reference, &reference_type, &by_path);
        for target in targets {
            if target.node_id == artifact_id {
                continue;
            }
            let same_source = &target.source_id == source_id;
            let key = (artifact_id.clone(), target.node_id.clone(), edge_type.clone());
            if seen.contains(&key) {
                continue;
            }
            seen.insert(key);
            out.push((
                artifact_id.clone(),
                target.node_id,
                edge_type.clone(),
                source_id.clone(),
                target.source_id.clone(),
                reference.clone(),
                reference_type.clone(),
                !same_source,
            ));
        }
    }
    out.sort_by(|a, b| (&a.0, &a.1, &a.2).cmp(&(&b.0, &b.1, &b.2)));

    let mut w = Writer::new();
    w.u32(out.len() as u32);
    for (source, target, etype, source_id, target_source_id, reference, reference_type, cross_source) in out {
        w.string(&source);
        w.string(&target);
        w.string(&etype);
        w.string(&source_id);
        w.string(&target_source_id);
        w.string(&reference);
        w.opt_string(&reference_type);
        w.u8(if cross_source { 1 } else { 0 });
    }
    let (ptr, len) = w.into_leaked();
    unsafe {
        *out_len_ptr = len;
    }
    ptr
}

// ---- graph_search._scan_edges -----------------------------------------------

const SKIP_KEYS: [&str; 8] = [
    "created_at",
    "updated_at",
    "knowledge_base_id",
    "hash",
    "text_hash",
    "sha256",
    "id",
    "key",
];

fn field_score(text: &str, tokens: &[String], q: &str, base_exact: f64, base_all: f64, base_any: f64) -> f64 {
    if text.is_empty() {
        return 0.0;
    }
    let low = text.to_lowercase();
    if !q.is_empty() && low == q {
        return base_exact;
    }
    if !q.is_empty() && low.contains(q) {
        return (base_exact + base_all) / 2.0;
    }
    if !tokens.is_empty() && tokens.iter().all(|t| low.contains(t.as_str())) {
        return base_all;
    }
    if !tokens.is_empty() && tokens.iter().any(|t| low.contains(t.as_str())) {
        return base_any;
    }
    0.0
}

#[no_mangle]
pub extern "C" fn scan_edges(input_ptr: *const u8, input_len: usize, out_len_ptr: *mut u32) -> *const u8 {
    let input = unsafe { slice::from_raw_parts(input_ptr, input_len) };
    let mut r = Reader::new(input);

    let token_count = r.u32();
    let tokens: Vec<String> = (0..token_count).map(|_| r.string()).collect();
    let q = r.string();
    let has_filter = r.string();
    let source_filter = if has_filter.is_empty() { None } else { Some(has_filter) };

    let edge_count = r.u32();
    let mut hits: Vec<(u32, f64)> = Vec::new();
    for edge_index in 0..edge_count {
        let source_label = r.string();
        let target_label = r.string();
        let edge_type = r.string();
        let edge_source_id = r.string();
        let attr_count = r.u32();
        let mut attrs: Vec<(String, String)> = Vec::with_capacity(attr_count as usize);
        for _ in 0..attr_count {
            let k = r.string();
            let v = r.string();
            attrs.push((k, v));
        }

        if let Some(filter) = &source_filter {
            if &edge_source_id != filter {
                continue;
            }
        }

        let type_score = field_score(&edge_type, &tokens, &q, 0.65, 0.55, 0.45);
        let endpoint_score = field_score(&source_label, &tokens, &q, 0.5, 0.4, 0.3)
            .max(field_score(&target_label, &tokens, &q, 0.5, 0.4, 0.3));
        let mut attr_score = 0.0f64;
        for (key, value) in &attrs {
            if SKIP_KEYS.contains(&key.as_str()) || key == "type" {
                continue;
            }
            attr_score = attr_score.max(field_score(value, &tokens, &q, 0.5, 0.35, 0.3));
        }
        let score = type_score.max(endpoint_score).max(attr_score);
        if score <= 0.0 {
            continue;
        }
        hits.push((edge_index, score));
    }

    let mut w = Writer::new();
    w.u32(hits.len() as u32);
    for (edge_index, score) in hits {
        w.u32(edge_index);
        w.f64(score);
    }
    let (ptr, len) = w.into_leaked();
    unsafe {
        *out_len_ptr = len;
    }
    ptr
}
