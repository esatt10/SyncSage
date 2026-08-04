;; Deliberately infinite loop — used to prove Store fuel metering fails a
;; runaway guest closed (SandboxFuelExhausted) instead of hanging the host.
(module
  (func (export "spin")
    (loop $l
      br $l)))
