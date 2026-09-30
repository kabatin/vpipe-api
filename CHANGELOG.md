# Changelog

## 0.1.0 — 2026-09-30

First release.

- HTTP job API (`/v1`): health, workflow listing with JSON Schemas, typed submit route per workflow, job status
  with progress and queue position, output download, cancel. One error envelope for every failure.
- One GPU slot with a bounded waiting queue; `429 busy` + `Retry-After` when full.
- Runtime failure detection for vpipe (log + fresh-output check), cancel/timeout via the process group.
- On-disk job store with restart recovery, retention pruning and a single-instance lock.
- Bearer-token auth (mandatory for non-loopback binds) and a request-size limit.
- Workflow `minimax-h3-turbo-video`: MiniMax H3 FL2VA 8-bit + Turbo LoRA, text / first-last-frame to video,
  exact output size via lanczos cover-crop, audio dropped, per-job metadata tag.
- `vpipe-api doctor [--smoke]`, `setup vpipe`, `setup models <workflow>` (download watchdog), `workflows`, `config`.
- `Idempotency-Key` on submit (same key + params → same job, `200`); busy gate answers `429` before the body.
- Hardening: token-less mode serves loopback Host names only and refuses proxied requests; tokens ≥ 32
  printable ASCII; strict job ids; inputs deleted once a job ends or is canceled; Pillow limited to
  PNG/JPEG(MPO)/WebP decoders; client-facing errors hide paths and internals; `doctor` flags a
  world-readable config with a token; `setup vpipe` verifies the pinned commit of known tags.
