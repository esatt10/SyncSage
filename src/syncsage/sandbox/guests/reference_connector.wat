;; Reference sandboxed-connector guest (Synapse Step 34.2).
;;
;; The host reads file bytes from disk (listing/reads stay host-side, see
;; SandboxedConnector's docstring) and marshals them in at offset 0; this
;; guest performs the one per-item transform that runs untrusted, inside
;; the fuel+memory-capped sandbox: normalize line endings (drop CR bytes)
;; before the host hashes/stores the result. A deliberately simple,
;; hand-verifiable transform — the point is exercising the harness on a
;; real per-item content operation, not the transform's sophistication.
(module
  (memory (export "memory") 4)
  (func (export "normalize")
        (param $in_ptr i32) (param $in_len i32)
        (param $out_ptr i32) (param $out_cap i32)
        (result i32)
    (local $i i32)
    (local $out_i i32)
    (local $byte i32)
    (local.set $i (i32.const 0))
    (local.set $out_i (i32.const 0))
    (block $done
      (loop $copy
        (br_if $done (i32.ge_u (local.get $i) (local.get $in_len)))
        (local.set $byte
          (i32.load8_u (i32.add (local.get $in_ptr) (local.get $i))))
        (if (i32.ne (local.get $byte) (i32.const 13)) ;; skip '\r' (0x0D)
          (then
            (br_if $done (i32.ge_u (local.get $out_i) (local.get $out_cap)))
            (i32.store8
              (i32.add (local.get $out_ptr) (local.get $out_i))
              (local.get $byte))
            (local.set $out_i (i32.add (local.get $out_i) (i32.const 1)))))
        (local.set $i (i32.add (local.get $i) (i32.const 1)))
        (br $copy)))
    (local.get $out_i)))
