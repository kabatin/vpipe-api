# vpipe-api HTTP API (v1)

All endpoints live under `/v1`. Request and response bodies are JSON (UTF-8) unless noted.
The machine-readable schema is served at `/openapi.json`; interactive docs at `/docs`.

## Authentication

- If the server has a token configured (`VPIPE_API_TOKEN`, ≥ 32 printable ASCII characters), **every**
  request — including `/docs` and `/openapi.json` — must send `Authorization: Bearer <token>`;
  otherwise the server answers `401`.
- A server bound to a non-loopback address refuses to start without a token.
- Without a token the server only answers requests whose `Host` is `127.0.0.1`, `localhost` or `[::1]`
  (blocks DNS-rebinding pages) and that carry no proxy headers (`Forwarded`, `X-Forwarded-*`,
  `X-Real-IP`) — put a token on it before publishing it through a reverse proxy.

## Error envelope

Every non-2xx response uses the same shape:

```json
{ "error": { "code": "busy", "message": "human readable", "retryable": true, "details": null } }
```

| HTTP | `code` | `retryable` | When |
|---|---|---|---|
| 401 | `unauthorized` | false | missing / wrong bearer token |
| 403 | `forbidden_host` / `proxy_requires_token` | false | token-less server reached via a foreign Host or a proxy |
| 404 | `not_found` | false | unknown workflow or job |
| 409 | `conflict` | false | e.g. output requested before the job succeeded, cancel of a finished job |
| 409 | `idempotency_conflict` | false | `Idempotency-Key` reused with different params |
| 409 | `idempotency_in_flight` | true | another request with the same key is being accepted right now |
| 409 | `model_not_installed` | false | the workflow's model is not downloaded on the server (`vpipe-api setup models <workflow>`); answered before the body is read |
| 411 | `length_required` | false | chunked upload without `Content-Length` |
| 413 | `payload_too_large` | false | request body over the limit (`max_body_mb`, default 96 MB) |
| 422 | `invalid_params` | false | params failed validation; `details` lists the problems |
| 429 | `busy` | true | the GPU slot and the waiting queue are full. `Retry-After` header (seconds) is set |
| 500 | `internal` | true | unexpected server error |

## Endpoints

### `GET /v1/health`

```json
{ "status": "ok", "version": "0.1.1", "running": 1, "waiting": 0, "max_waiting": 1 }
```

### `GET /v1/workflows`

```json
{
  "workflows": [
    {
      "id": "minimax-h3-turbo-video",
      "description": "MiniMax H3 (FL2VA, 8-bit) + Turbo LoRA: text or first/last-frame to video, scaled to the requested size, no audio",
      "params_schema": { "...": "JSON Schema of the POST body" },
      "output_media_type": "video/mp4"
    }
  ]
}
```

### `POST /v1/workflows/{workflow_id}/jobs`

Body = the workflow's params (see below), `Content-Type: application/json`.
Optional header `Idempotency-Key: <1–128 of [A-Za-z0-9._:-]>`.

Success is `202 Accepted`:

```json
{ "id": "job_01J9ZK3N2Q8V7W6X5Y4Z3A2B1C", "workflow": "minimax-h3-turbo-video", "status": "queued", "created_at": "2026-09-30T12:00:00Z", "estimate_seconds": 637.2 }
```

`429 busy` means "try again later" — nothing was queued. It is answered before the body is read,
and so is `409 model_not_installed`. Every workflow shares the one GPU slot and its waiting queue: an
upscale never runs next to a generation, and `/v1/health` counts both.

**Idempotency.** Send an `Idempotency-Key` (e.g. your own job id) to make retries safe. Resubmitting
the same key with the same params returns the job it already created with `200 OK` (same body shape)
— never a second job and never `429`. The same key with different params is `409
idempotency_conflict`; a concurrent request with the same key gets `409 idempotency_in_flight`
(retryable — try again shortly). Keys are remembered as long as the job record (`retention_days`).

### `GET /v1/jobs/{job_id}`

```json
{
  "id": "job_...",
  "workflow": "minimax-h3-turbo-video",
  "status": "succeeded",
  "progress": 1.0,
  "queue_position": null,
  "created_at": "2026-09-30T12:00:00Z",
  "started_at": "2026-09-30T12:00:01Z",
  "finished_at": "2026-09-30T12:07:10Z",
  "result": {
    "output": { "media_type": "video/mp4", "width": 1920, "height": 1080, "frames": 124, "fps": 24, "duration_sec": 5.167 },
    "seed_used": 12345,
    "details": { "generation": { "width": 1024, "height": 576, "frames": 124, "steps": 6, "quality": "standard" } }
  },
  "error": null,
  "estimate_seconds": 637.2,
  "timings": { "queue_seconds": 0.8, "backend_seconds": 421.6, "postprocess_seconds": 7.2, "total_seconds": 429.7 }
}
```

- `status`: `queued` → `running` → `succeeded` | `failed` | `canceled`.
- `progress`: `0.0..1.0` while running (denoise progress), `null` when unknown.
- `queue_position`: 1-based position while `queued`, else `null`.
- `result` is non-null only when `succeeded`; `error` is non-null only when `failed`
  (`{code, message, retryable}`; e.g. `generation_failed`, `timeout`, `server_restarted`,
  `preprocess_failed` — the upload could not be read for an upscale, not retryable).
- `estimate_seconds`: how long the run should take once it starts — the workflow's own estimate, set at
  submit (also in the `202` body). Use it with `started_at` for "minutes left"; it is what the job
  timeout is based on (`estimate × job_timeout_factor + 5 min`). `null` on jobs from older versions.
- `timings`: wall-clock seconds measured by the server (same keys as wan-api). `queue_seconds` appears
  when the job starts; `backend_seconds` (the vpipe run), `postprocess_seconds` (ffmpeg) and
  `total_seconds` (created → finished) when it ends. `{}` while queued.

### `GET /v1/jobs/{job_id}/output`

The output file (`video/mp4` for video workflows). `409 conflict` unless the job has succeeded.
Outputs are kept for `retention_days` (default 7) and then deleted with the job record.

### `DELETE /v1/jobs/{job_id}`

Cancels a queued or running job and returns the job object (`status: "canceled"`).
`409 conflict` if the job already finished.

## Workflow `minimax-h3-turbo-video`

POST body:

| field | type | default | notes |
|---|---|---|---|
| `prompt` | string, 1–4000 chars | — | Describe the picture (audio is discarded). |
| `output` | `{width, height}` ints | — | Final size; 64–4096 each. Aspect must be within 16:9 … 9:16. The clip is generated smaller and scaled (cover + center crop, lanczos). |
| `frames` | int | `124` | Must be `17n+5` in `56..243` (56 = 2.33 s … 243 = 10.125 s at 24 fps). Delivered as-is, never trimmed. |
| `quality` | `"draft"` \| `"standard"` \| `"final"` | `"standard"` | Generation size tier (see table). |
| `seed` | int ≥ 0 \| null | random | Echoed back as `result.seed_used`. |
| `steps` | int 4–8 | `6` | Turbo LoRA denoise steps. |
| `start_image` | image \| null | null | First frame anchor. |
| `end_image` | image \| null | null | Last frame anchor. Requires `start_image`. |

`image` = `{ "data": "<base64>", "media_type": "image/png" | "image/jpeg" | "image/webp" }`, decoded size ≤ 20 MB.

Generation size (chosen from the output aspect ratio; the closest ratio row is used):

| aspect | draft | standard | final |
|---|---|---|---|
| 16:9 | 832×480 | 1024×576 | 1344×768 |
| 9:16 | 480×832 | 576×1024 | 768×1344 |
| 1:1 | 640×640 | 768×768 | 768×768 |
| 4:5 | 512×640 | 640×800 | 768×960 |

`final` is H3's training resolution (short side 768): the most detail, about 3× the time of `draft` at 16:9 or
9:16 (about 2.2× at 4:5). At 1:1 it is the same size as `standard`. The same seed and prompt at a different
generation size give a different clip, not an upscale of it.

Output: H.264 MP4 (yuv420p, limited-range BT.709), 24 fps, **no audio track**, metadata
`comment=vpipe-job:<job_id>`. vpipe writes a lossless FFV1 intermediate, so this encode is the only lossy step.

Typical time on an M5 (10-core GPU, 32 GB), 6 steps: draft 124 frames ≈ 8 min, standard 124 frames ≈ 10.5 min,
final 124 frames ≈ 22 min, standard 243 frames ≈ 24 min, final 243 frames ≈ 54 min. One job runs at a time.

## Workflow `flashvsr-upscale`

[FlashVSR v1.1](https://huggingface.co/JunhaoZhuang/FlashVSR-v1.1) video super-resolution of a clip you
upload — typically a take from `minimax-h3-turbo-video`. Download the model first
(`vpipe-api setup models flashvsr-upscale`, ≈ 6.8 GB); until then a submit is refused with
`409 model_not_installed`.

POST body:

| field | type | default | notes |
|---|---|---|---|
| `source_video` | `{ "data": "<base64>", "media_type": "video/mp4" }` | — | MP4 with 8-bit SDR H.264 or HEVC video and, optionally, AAC audio — no subtitles. Decoded size ≤ 64 MB, length ≤ 40 s, ≤ 60 fps, 16–4096 px a side. A phone's rotation tag is applied; HDR (HLG/PQ, BT.2020) and 10-bit are refused. |
| `output` | `{width, height}` ints \| null | the source's shape, long side 1920 | Final size; 64–4096 each, aspect within 16:9 … 9:16. |

```sh
# API and AUTH as in examples/curl.md; --rawfile because --arg would exceed the argument limit
base64 -i take.mp4 > take.b64        # Linux: base64 -w0 take.mp4 > take.b64
jq -n --rawfile v take.b64 '{source_video: {data: ($v | rtrimstr("\n")), media_type: "video/mp4"},
                             output: {width: 1920, height: 1080}}' > body.json
curl -s "${AUTH[@]}" -X POST "$API/v1/workflows/flashvsr-upscale/jobs" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: take-123-upscale' --data-binary @body.json
```

What comes back:

- Exactly the source's **frame count and frame rate** (24 stays 24, 24000/1001 stays 24000/1001), so the
  clip drops into the same place on a timeline. The source's audio track is copied unchanged.
- Exactly `output` in size. The source is centre-cropped to the output's shape (1344×768 → 1920×1080 trims
  6 rows top and bottom), processed on FlashVSR's 128-pixel grid — the output rounded up, e.g.
  1920×1080 → 1920×1152 — and resized to the output. Larger outputs (e.g. 3840×2160) are processed at
  most at 1920×1152's pixel count and resized up from there.
- H.264 MP4 (yuv420p, limited-range BT.709), `comment=vpipe-job:<job_id>`, `seed_used: null`.
  `details.generation` gives the processing size and the frames vpipe ran (see below).

FlashVSR returns 21 frames per 25-frame group, output frame *n* being source frame *n*, and never
returns the last 4 frames of a clip or a partial group (measured with frame numbers burned into a
clip). The server clones the last frame up to whole groups plus those 4, runs that, and keeps the
first frames — exactly the source's: every output frame is generated from a real source frame,
none is a still copy.

Time is per group of 21 source frames: about 104 s each on an M5 (10-core GPU, 32 GB) at 1920×1152
(42 frames = 2 groups ≈ 3.5 min, 56 frames = 3 groups ≈ 5.2 min); smaller processing sizes take
proportionally less. vpipe reloads the model for every group to make room for the VAE decode; free
memory went down to 16 % and swap grew by about 9.5 GB in a 3-group run, so like `final` this is a
32 GB-class job — and, like every job, it never runs beside another one.
