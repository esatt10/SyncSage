;; Exercises the host_fetch capability surface: asks the host to fetch a
;; URL baked into its own data segment, then copies the result into its
;; own memory at offset 100 and returns the byte count (or a negative
;; host_fetch_len/host_fetch_read error code).
(module
  (import "env" "host_fetch_len" (func $fetch_len (param i32 i32) (result i32)))
  (import "env" "host_fetch_read" (func $fetch_read (param i32 i32) (result i32)))
  (memory (export "memory") 1)
  (data (i32.const 0) "http://allowed.example/data")
  (func (export "fetch_test") (result i32)
    (local $n i32)
    (local.set $n (call $fetch_len (i32.const 0) (i32.const 27)))
    (if (i32.lt_s (local.get $n) (i32.const 0))
      (then (return (local.get $n))))
    (call $fetch_read (i32.const 100) (local.get $n))))
