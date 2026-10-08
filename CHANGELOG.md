# Changelog

## Unreleased

- Outputs are exactly 24 fps again. Since 0.1.1 the frame rate was guessed from the Matroska intermediate's
  millisecond timestamps; with large frames (`final`) the file came out at a guessed rate (23.976 on our server)
  with the right frame count, while the job result still said `fps: 24`.

## 0.1.1 — 2026-10-07

- `quality: "final"`: H3's training resolution (short side 768; 1344×768 for 16:9), about 3× the time of `draft`.
- vpipe now writes a lossless intermediate (FFV1, 4:4:4, full-range BT.709) instead of 2 Mbps H.264, so the final
  encode is the only lossy step. The final encode always runs (it used to be a stream copy at the same size) and
  converts to limited-range BT.709, tagged as such (the output used to carry BT.601 / mixed tags).
- Time estimates refit to ~140 measured jobs, `final` at 124 and 243 frames included (they ran up to ~20 % low).
  Job timeouts and `Retry-After` follow the new estimates.
- WebP start/end images decode (Pillow was handed an MPO decoder that does not exist, which ended the lookup).
- Docs: a first job with curl, how to update, every config key, measured times and memory for each tier.

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
- `Idempotency-Key` on submit (same key + params → same job, `200`; the same key while the first request is still
  being accepted → retryable `409 idempotency_in_flight`); busy gate answers `429` before the body.
- Hardening: token-less mode serves loopback Host names only and refuses proxied requests; tokens ≥ 32
  printable ASCII; strict job ids; inputs deleted once a job ends or is canceled; Pillow limited to
  PNG/JPEG(MPO)/WebP decoders; client-facing errors hide paths and internals; `doctor` flags a
  world-readable config with a token; `setup vpipe` verifies the pinned commit of known tags.
