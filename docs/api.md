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
| 411 | `length_required` | false | chunked upload without `Content-Length` |
| 413 | `payload_too_large` | false | request body over the limit (64 MB) |
| 422 | `invalid_params` | false | params failed validation; `details` lists the problems |
| 429 | `busy` | true | the GPU slot and the waiting queue are full. `Retry-After` header (seconds) is set |
| 500 | `internal` | true | unexpected server error |

## Endpoints

### `GET /v1/health`

```json
{ "status": "ok", "version": "0.1.0", "running": 1, "waiting": 0, "max_waiting": 1 }
```

### `GET /v1/workflows`

```json
{
  "workflows": [
    {
      "id": "minimax-h3-turbo-video",
      "description": "MiniMax H3 (FL2VA, 8-bit) + Turbo LoRA: text / first-last-frame to video",
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
{ "id": "job_01J9ZK3N2Q8V7W6X5Y4Z3A2B1C", "workflow": "minimax-h3-turbo-video", "status": "queued", "created_at": "2026-09-30T12:00:00Z" }
```

`429 busy` means "try again later" — nothing was queued. It is answered before the body is read.

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
  "error": null
}
```

- `status`: `queued` → `running` → `succeeded` | `failed` | `canceled`.
- `progress`: `0.0..1.0` while running (denoise progress), `null` when unknown.
- `queue_position`: 1-based position while `queued`, else `null`.
- `result` is non-null only when `succeeded`; `error` is non-null only when `failed`
  (`{code, message, retryable}`; e.g. `generation_failed`, `timeout`, `server_restarted`).

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
