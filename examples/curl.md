# curl examples

```sh
API=http://127.0.0.1:8765
AUTH=()                                   # or: AUTH=(-H "Authorization: Bearer $VPIPE_API_TOKEN")
```

## Text to video

```sh
curl -s "${AUTH[@]}" -X POST "$API/v1/workflows/minimax-h3-turbo-video/jobs" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: my-shot-001' \
  -d '{"prompt": "A red paper lantern swaying over a quiet alley at night, soft bokeh lights.",
       "output": {"width": 1920, "height": 1080}, "frames": 124, "quality": "draft", "seed": 7}'
# → 202 {"id":"job_…","workflow":"minimax-h3-turbo-video","status":"queued",…}
# Sending the same Idempotency-Key + body again returns the same job (200), never a duplicate.
```

## First (and last) frame

```sh
jq -n --arg start "$(base64 -i start.png)" --arg end "$(base64 -i end.jpg)" '{
  prompt: "A surfer rides through a turquoise barrel wave, spray flying.",
  output: {width: 1920, height: 1080}, frames: 73, quality: "draft",
  start_image: {data: $start, media_type: "image/png"},
  end_image:   {data: $end,   media_type: "image/jpeg"}
}' > body.json

curl -s "${AUTH[@]}" -X POST "$API/v1/workflows/minimax-h3-turbo-video/jobs" \
  -H 'Content-Type: application/json' --data-binary @body.json
```

## Poll, download, cancel

```sh
JOB=job_…
curl -s "${AUTH[@]}" "$API/v1/jobs/$JOB" | jq '{status, progress, queue_position, error}'
curl -s "${AUTH[@]}" -o clip.mp4 "$API/v1/jobs/$JOB/output"     # 409 until succeeded
curl -s "${AUTH[@]}" -X DELETE "$API/v1/jobs/$JOB"
```

## Handling `429 busy`

The server keeps one job running and `max_waiting` (default 1) queued. When full, it answers:

```
HTTP/1.1 429 Too Many Requests
Retry-After: 166
{"error":{"code":"busy","message":"the GPU slot and the waiting queue are full","retryable":true,"details":null}}
```

Wait `Retry-After` seconds and submit again. Nothing was queued.
