# MIRRA

MIRRA turns a fashion campaign into a shoppable virtual mirror. A brand uploads one short campaign, maps each detected look to a validated product reference, and publishes an immutable campaign manifest. A shopper adds one photo and receives each approved look progressively through YouCam AI Clothes v3.

The experience has two intentionally different surfaces:

- A warm, editorial brand flow for upload, products, AI direction, detected-look review, and publishing.
- A cinematic black Mirror mode with an equal 50/50 `CAMPAIGN | YOUR MIRROR` player and only Remix, Zoom, and Save as primary actions.

## What is implemented

- Supabase email/password authentication and brand membership checks.
- A 45 MB source-video limit with a configurable 30-second default.
- Immediate FFprobe/FFmpeg validation and deterministic scene candidates.
- One schema-constrained Gemini campaign-analysis request for semantic understanding, garment interpretation, hero selection, transitions, product matching, and brand direction.
- Analysis caching by campaign scope, video checksum, campaign input version, model, and schema version.
- Gemini 3.7 Flash campaign routing and Flash-Lite utility/repair routing.
- Brand review with a dynamic look filmstrip, selected-look controls, and garment category kept under Advanced controls.
- Immutable published manifests.
- Validated shopper-photo upload and real YouCam task creation.
- Durable YouCam polling with persisted task ID, provider state, attempts, and `next_poll_at`; workers never sleep while a provider task runs.
- Progressive Mirror results, seek-aware priority, save/My Mirrors, and brand-approved remix constraints.
- Atomic daily provider reservations, atomic YouCam submission slots, double-click-safe publishing/remix, and privacy-scoped YouCam caching.
- Responsive editorial and Mirror interfaces based on the approved references.

## Architecture

```text
Next.js web
  |-- Supabase Auth token
  |-- campaign + mirror API calls
  v
FastAPI API --------------------> Supabase Postgres + private Storage
  |                                      ^
  | enqueue durable jobs                 | persisted task/result state
  v                                      |
Worker --> FFprobe/FFmpeg --> Gemini Interactions API
  |
  +------ create/poll YouCam Clothes v3 --> save result immediately
```

The browser never receives provider secrets or the Supabase service-role key. Provider responses are not presented as complete until the output has been copied into controlled Supabase storage.

## Readiness gate

Provider credentials and acceptance artifacts are intentionally never tracked in this repository. MIRRA does not substitute fixtures for provider output, so release readiness must be decided from a live acceptance run rather than the presence of adapters or passing unit tests alone.

For each release environment, run and record:

1. One real Gemini 3.7 Flash video analysis through the Interactions API.
2. One real YouCam Clothes v3 task, including reference compatibility, completion latency, and persisted output.
3. One complete brand-to-shopper flow against that environment's Supabase project, including seek reprioritization, real Remix, Save, and My Mirrors.

See [SETUP.md](SETUP.md) for exact setup and verification commands.

## Repository policy

Only this README and SETUP are maintained as committed Markdown documentation. Demo media, screenshots, traces, uploads, generated outputs, debug payloads, recordings, one-off scripts, and implementation artifacts are ignored and must not be committed.

## License

MIRRA is available under the [Apache License 2.0](LICENSE).
