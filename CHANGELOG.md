# Changelog

## Unreleased

- `vpipe-api setup [--models h3,flashvsr] [--check] [--yes] [--json] [--lan] [--service] [--port N]
  [--prefix DIR] [--label-suffix S] [--coordinate-with NAME=HEALTH_URL]`: one command for what took
  `setup vpipe` + `setup models` + editing the config by hand, the same as the other AI servers.
  `h3` builds vpipe (under `--prefix`, default `~/.local/opt/vpipe-api`) and prepares H3 Turbo; `flashvsr`
  prepares FlashVSR. `--check` changes nothing and reports each component as `installed`, `missing` (with
  its download size) or `unsupported` with the reason (not Apple Silicon, macOS older than 26, no Xcode /
  cmake). It asks once before downloading (exit 3 without a terminal and without `--yes`) and stops before
  anything when the disk cannot hold the peak (185 GB for H3). The config gets `vpipe_bin`,
  `vpipe_src_dir` and `work_dir` (existing keys are kept); `--lan`, `--port`, `--coordinate-with` and
  `--service` (a launchd agent, checked through `/v1/health`) are optional.
- `setup vpipe` writes `vpipe_bin`, `vpipe_src_dir` and `work_dir` to the config instead of printing them.
  `setup vpipe` and `setup models` otherwise work as before.

## 0.1.4 — 2026-10-19

- `minimax-h3-turbo-video` takes `audio: true`: the clip keeps H3's soundtrack (dialogue, sound effects,
  music), which the model generates with the picture anyway, as AAC ending with the last frame. Without it,
  the clip stays silent as before. A clip vpipe returns without a soundtrack fails with `unexpected_output`.
- `coordinate_with`: other GPU servers on the Mac (ace-step-api, sfx-api, klein-api) whose `/v1/health`
  the next job waits for. It starts only once every one reports `running` and `waiting` at 0 (and
  `outside_runs`, when present), and not within `settle_seconds` (30) of seeing one busy. A server that
  cannot be asked counts as idle; a running job is never stopped. While the head job waits, its job shows
  `waiting_for` and `/v1/health` counts it in `waiting_for_peer` instead of `waiting`, so the other servers
  go first rather than all waiting for each other. `doctor` asks each server; `config` lists them without
  their tokens. Without `coordinate_with`, nothing changes.
- A job whose record cannot be saved as it starts (a full disk) fails as `internal` (retryable) instead
  of stopping the worker, which left `running: 1` and the queue stuck until a restart.

## 0.1.3 — 2026-10-09

- `minimax-h3-turbo-video` takes `native: true`: the clip comes back at its generation size, only
  center-cropped to the shape of `output` (1920×1080 at `final`: 1344×756), for clients that scale it
  themselves. `result.output` gives the returned size; `details.generation` the size before the crop. Without
  it, nothing changes.

## 0.1.2 — 2026-10-09

- Workflow `flashvsr-upscale`: FlashVSR v1.1 super-resolution of an uploaded MP4 (≤ 64 MB, ≤ 40 s) to an
  exact size, keeping the source's frame count, frame rate and audio. It shares the GPU slot and the
  waiting queue with every other workflow. Model: `vpipe-api setup models flashvsr-upscale` (≈ 6.8 GB).
- `estimate_seconds` on every job (and in the submit's `202` body): the workflow's estimate of the run, as
  in wan-api, so clients need no formula of their own.
- `timings` on every job (`queue_seconds`, `backend_seconds`, `postprocess_seconds`, `total_seconds`),
  measured by the server, same keys as wan-api.
- A submit to a workflow whose model is not downloaded is refused before its body is read:
  `409 model_not_installed` (not retryable). `doctor` warns instead of failing when another workflow can
  still run, and `doctor --smoke` skips that workflow.
- `setup models` can fetch a model vpipe uses as published (no prepare pipeline), with one download
  stream: vpipe's default eight stalled at 0 % on every try.
- `max_body_mb` defaults to 96 (a 64 MB video is about 86 MB as base64).
- `/v1/health` reports `outside_runs`: `vpipe` command-line runs on the Mac that the server did not start
  (an experiment, `doctor --smoke`). The next queued job waits while there are any.
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
