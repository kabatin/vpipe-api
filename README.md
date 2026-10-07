# vpipe-api

**Run [vpipe](https://github.com/tgo-app-dev/vpipe) generative pipelines on your Apple Silicon Mac as an HTTP job API.**

English | [日本語](README.ja.md)

vpipe runs large generative models (MiniMax H3 video, Qwen-Image, FLUX, …) on-device through its own Metal kernels.
`vpipe-api` wraps its CLI in a small, safe job server so other tools — an editor, a render farm script,
a web app on the same LAN — can submit work and fetch results:

```
POST /v1/workflows/minimax-h3-turbo-video/jobs   → 202 {id}
GET  /v1/jobs/{id}                               → queued → running (progress) → succeeded
GET  /v1/jobs/{id}/output                        → video/mp4
```

- **Workflow registry.** Clients call named, validated recipes (`minimax-h3-turbo-video`), never raw pipeline JSON,
  so a LAN-exposed server cannot be told to read or write arbitrary files.
- **One GPU slot, honest backpressure.** Jobs run one at a time. When the running slot and the small waiting queue
  are full, `POST` answers `429 busy` with `Retry-After` instead of silently piling up hours of work.
- **Real failure detection.** vpipe exits `0` even when a stage fails at runtime; vpipe-api reads the log and checks
  that the output was actually written before calling a job successful.
- **Restart-safe.** Jobs are persisted on disk; queued jobs resume after a restart, interrupted ones are reported as
  retryable failures.
- **`doctor` and `setup`.** Check a machine, build vpipe, and download/prepare models with a download watchdog.

## Workflow: `minimax-h3-turbo-video`

[MiniMax H3](https://huggingface.co/MiniMaxAI/MiniMax-H3) (FL2VA, 8-bit) with the community
[Turbo LoRA](https://huggingface.co/larryvrh/MiniMax-H3-Turbo-Lora): text → video, optionally anchored to a
first frame and a last frame. The clip is generated at a practical size and scaled (lanczos, cover + center crop)
to exactly the resolution you ask for; audio is dropped.

| | |
|---|---|
| Output | any size 64–4096 px, aspect 16:9 … 9:16; H.264 MP4 (BT.709), 24 fps, no audio |
| Length | `frames` = 17n+5: 56 (2.33 s) … 243 (10.125 s) |
| Quality tiers | `draft` (e.g. 832×480 for 16:9) · `standard` (1024×576) · `final` (1344×768, H3's training size) |
| Anchors | `start_image`, `end_image` (base64 PNG/JPEG/WebP ≤ 20 MB; end needs start) |

Measured on an M5 MacBook Pro (10-core GPU, 32 GB), 6 steps: draft 124 frames ≈ 8 min,
standard 124 frames ≈ 10.5 min, final 124 frames ≈ 22 min, standard 243 frames ≈ 24 min,
final 243 frames ≈ 54 min.

> **License note.** The MiniMax H3 weights are under the *MiniMax H3 Community License*, which does not permit use
> in the United States, the European Union, the United Kingdom or South Korea, and has its own terms for
> commercial use. Check the license of every model you download before using its output. vpipe-api itself is
> Apache-2.0 and redistributes no weights.

## Requirements

- Apple Silicon Mac, macOS 26+ (vpipe's generative stack), ~65 GB disk for H3 (185 GB peak while preparing)
- Python 3.12+ and [uv](https://docs.astral.sh/uv/), `ffmpeg`/`ffprobe`, Xcode (for building vpipe), `cmake`
- 16 GB RAM works; more RAM mostly means less weight streaming

## Install

```sh
uv tool install git+https://github.com/kabatin/vpipe-api
```

### 1. vpipe (skip if you already built it)

```sh
vpipe-api setup vpipe --dir ~/vpipe/src          # clone v0.1.80 + cmake build (~20 min)
```

It prints the two lines to put in `~/.config/vpipe-api/config.toml`:

```toml
vpipe_bin = "/Users/you/vpipe/src/build/apps/vpipe/vpipe"
work_dir  = "/Users/you/vpipe/work"              # model registry + models live here
```

### 2. Models

```sh
vpipe-api setup models minimax-h3-turbo-video     # ~118 GB download, then 8-bit quantize
```

Downloads resume where they stopped. If the model directory stops growing for 10 minutes (e.g. after switching
networks, when vpipe's connection stays bound to the old IP), the fetch is restarted automatically.

### 3. Check and serve

```sh
vpipe-api doctor --smoke     # environment + one tiny real generation
vpipe-api serve              # http://127.0.0.1:8765  (docs at /docs)
```

## Configuration

`~/.config/vpipe-api/config.toml` (or `$VPIPE_API_CONFIG`); every scalar can also be set as `VPIPE_API_<NAME>`.

| key | default | |
|---|---|---|
| `vpipe_bin`, `work_dir` | — | required to serve |
| `host` / `port` | `127.0.0.1` / `8765` | a non-loopback host **requires** `token` |
| `token` | — | when set, every request needs `Authorization: Bearer <token>` |
| `max_waiting` | `1` | jobs allowed to wait behind the running one |
| `retention_days` | `7` | finished jobs and outputs are deleted after this |
| `data_dir` | `~/.local/share/vpipe-api` | job records and outputs |
| `job_timeout_factor` | `3.0` | a job is stopped after `estimate × factor + 5 min` |
| `max_body_mb` | `64` | request size limit |

Workflow options:

```toml
[workflows."minimax-h3-turbo-video"]
sol_attn = false          # exact attention for final renders (slower)
i8_gemm  = true           # M5+ matrix cores
lora     = "larryvrh/MiniMax-H3-Turbo-Lora-v4-600-ema"
```

### Serving on a LAN

```sh
export VPIPE_API_HOST=0.0.0.0
export VPIPE_API_TOKEN="$(openssl rand -hex 32)"
vpipe-api serve
```

- The server refuses to bind a non-loopback address without a token. Tokens must be ≥ 32 printable ASCII
  characters. Keep the config file private: `chmod 600 ~/.config/vpipe-api/config.toml` (`doctor` warns).
- There is no TLS — the token travels in clear text. Use a trusted network, or a TLS reverse proxy.
  **Keep the token set when proxying**: a token-less server refuses proxied requests (and requests for
  foreign `Host` names, which stops DNS-rebinding web pages) rather than trusting them.
- With a token, `/docs` needs the header too; browse it from a token-less local instance or read
  `/openapi.json` with curl.
- `examples/com.github.kabatin.vpipe-api.plist` runs the server at login via launchd.

## API

See [docs/api.md](docs/api.md) and [examples/curl.md](examples/curl.md). The OpenAPI document is at `/openapi.json`.
Send an `Idempotency-Key` with every submit so a retry after a timeout returns the same job instead of
generating twice.

## Adding a workflow

1. Subclass `vpipe_api.workflows.base.Workflow` — declare `id`, `params_model` (pydantic; it becomes the JSON
   Schema and the typed POST route), `required_models`, and implement `store_inputs`, `estimate_seconds`,
   `prepare` (return the vpipe pipeline spec and the raw output path), `finalize` and `smoke_params`.
2. Register it in `vpipe_api/workflows/__init__.py::build_registry`.
3. Add tests using the fake `vpipe` in `tests/conftest.py`.

Keep workflows closed: clients choose parameters, never file paths or stage graphs.

## Operational notes

- One heavy job at a time. Metal memory is wired; running other large GPU apps (renderers, local LLMs) during a
  generation slows both down or exhausts memory.
- Use AC power for long batches — on battery a Mac throttles and drains quickly.
- Fanless Macs throttle on long clips; expect longer times than the table above.

## Development

```sh
uv sync
uv run pytest            # 80%+ coverage gate; uses a fake vpipe, needs ffmpeg for some tests
uv run ruff check src tests && uv run ruff format --check src tests && uv run pyright
```

## License

Apache-2.0. vpipe is © its authors under Apache-2.0; model weights carry their own licenses.
