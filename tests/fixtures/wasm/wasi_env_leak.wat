;; Adversarial guest (Step 34.3): tries to read the process environment via
;; the WASI preview1 import a real ambient-env runtime would provide. The
;; sandbox never wires wasi_snapshot_preview1, so this module must fail to
;; *instantiate* at all (a clear "unknown import" error) — proving there is
;; no ambient environment access to leak from, not merely that a call to
;; leak one is refused at call time.
(module
  (import "wasi_snapshot_preview1" "environ_get"
    (func $environ_get (param i32 i32) (result i32)))
  (memory (export "memory") 1)
  (func (export "leak_env") (result i32)
    (call $environ_get (i32.const 0) (i32.const 4))))
