;; Adversarial guest (Step 34.3): tries two classic SSRF-shaped requests —
;; a disguised local-file read via the file:// scheme, and a cloud
;; metadata-endpoint host — through the same host_fetch capability a
;; well-behaved connector guest would use. Both must be denied by the host
;; (scheme check + allowlist check), never reaching the fetcher.
(module
  (import "env" "host_fetch_len" (func $fetch_len (param i32 i32) (result i32)))
  (import "env" "host_fetch_read" (func $fetch_read (param i32 i32) (result i32)))
  (memory (export "memory") 1)
  (data (i32.const 0) "file:///etc/passwd")
  (data (i32.const 32) "http://169.254.169.254/latest/meta-data/")
  (func (export "try_file_scheme") (result i32)
    (call $fetch_len (i32.const 0) (i32.const 18)))
  (func (export "try_metadata_host") (result i32)
    (call $fetch_len (i32.const 32) (i32.const 40))))
