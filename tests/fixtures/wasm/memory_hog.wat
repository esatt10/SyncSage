;; Requests a huge memory growth (denied by Store.set_limits, so
;; memory.grow returns -1 and the actual page count is unchanged), then
;; ignores that failure and stores into the requested-but-never-granted
;; region anyway. Proves an out-of-bounds write past the real cap fails
;; closed (SandboxMemoryLimitExceeded) rather than corrupting host memory.
(module
  (memory (export "memory") 1)
  (func (export "blow_up") (result i32)
    (drop (memory.grow (i32.const 100000)))
    (i32.store (i32.const 50000000) (i32.const 1))
    (i32.const 0)))
