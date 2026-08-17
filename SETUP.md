# MIRRA Setup

## Prerequisites

- Node.js 22+
- Python 3.12+
- FFmpeg and FFprobe on `PATH`
- A Supabase project
- A Gemini API key with access to the configured campaign model
- A YouCam API key with AI Clothes v3 access

Do not paste service-role or provider keys into chat, source files, browser code, or commits.

## 1. Install

From the repository root in PowerShell:

```powershell
npm ci
uv venv --python 3.12 --clear services/api/.venv
uv pip install --python services/api/.venv/Scripts/python.exe --link-mode copy -r services/api/requirements.txt
```

If `uv` is unavailable, create a Python 3.12 virtual environment and install `services/api/requirements.txt` with pip.

## 2. Create the Supabase schema

Open the Supabase SQL editor and run:

```text
supabase/migrations/0001_mirra_core.sql
```

The migration creates the product tables, row-level security, immutable manifests, private storage buckets, the 45 MB campaign-object cap, and durable job RPCs.

In Supabase Authentication, enable Email and create the first brand user. Then add its profile, brand, and brand-membership row in the SQL editor:

```sql
insert into public.profiles (id, display_name)
values ('AUTH_USER_UUID', 'Brand operator');

insert into public.brands (id, slug, name)
values ('BRAND_UUID', 'studio-name', 'Studio Name');

insert into public.brand_members (brand_id, user_id, role)
values ('BRAND_UUID', 'AUTH_USER_UUID', 'owner');
```

Use real UUIDs in place of the uppercase placeholders.

## 3. Configure local environment

Copy `.env.example` for the API, then create a separate web environment containing only public values:

```powershell
Copy-Item .env.example services/api/.env
```

`apps/web/.env.local` must contain only:

```text
NEXT_PUBLIC_API_URL=http://localhost:8000
NEXT_PUBLIC_SUPABASE_URL=
NEXT_PUBLIC_SUPABASE_ANON_KEY=
NEXT_PUBLIC_DEMO_MEDIA_BASE_URL=/demo-media
```

Fill the API values in `services/api/.env`:

```text
SUPABASE_URL
SUPABASE_ANON_KEY
SUPABASE_SERVICE_ROLE_KEY
GEMINI_API_KEY
YOUCAM_API_KEY
```

Keep these routing defaults for the target release:

```text
GEMINI_CAMPAIGN_MODEL=gemini-3.7-flash
GEMINI_UTILITY_MODEL=gemini-3.5-flash-lite
GEMINI_INTERACTIONS_API_VERSION=v1beta
GEMINI_UTILITY_INTERACTIONS_API_VERSION=v1
GEMINI_CAMPAIGN_DAILY_LIMIT=18
YOUCAM_DAILY_USER_LIMIT=25
CAMPAIGN_MAX_BYTES=47185920
CAMPAIGN_MAX_SECONDS=30
```

Supabase's current publishable key can be used for the two `*_ANON_KEY` variables; its server-only secret key belongs only in `SUPABASE_SERVICE_ROLE_KEY`. During pipeline development only, you may set `GEMINI_CAMPAIGN_MODEL=gemini-3.5-flash-lite`. Restore 3.7 Flash before the real feasibility run. The application makes one normal campaign-analysis call and does not automatically retry 3.7 Flash. Provider reservations are atomic and idempotent in Supabase: the campaign allowance is global, while the YouCam allowance is enforced per shopper per UTC day.

## 4. Optional local editorial media

The local preview can use files in `apps/web/public/demo-media/`; this folder is intentionally ignored. Add:

```text
campaign-hero.png
look-01.png
look-02.png
look-03.png
```

These files are a development-only visual fallback. Production discovery reads the latest public, published campaign from Supabase; it does not depend on local demo media. Do not commit generated campaign media.

## 5. Run

Use three PowerShell terminals from the repository root.

Web:

```powershell
npm run dev
```

API:

```powershell
services/api/.venv/Scripts/python.exe -m uvicorn app.main:app --app-dir services/api --reload --port 8000
```

Worker:

```powershell
$env:PYTHONPATH = 'services/api'
services/api/.venv/Scripts/python.exe -m app.worker
```

Open `http://localhost:3000`. The API health endpoint is `http://localhost:8000/health`.

## 6. Verify before a live provider run

```powershell
npm run typecheck
npm run lint
npm test
npm run build
services/api/.venv/Scripts/python.exe -m pytest services/api/tests -q
```

Confirm FFmpeg separately:

```powershell
ffmpeg -version
ffprobe -version
```

## 7. Provider feasibility gate

Sign in through the web app, obtain the current Supabase access token from the authenticated browser session, and use it as `BEARER_TOKEN` locally. Never commit or share it.

For Gemini, first create a real campaign through the brand flow, then run the campaign analysis once. Verify the `campaign_analyses` row contains the configured model, cache key, schema version, interaction ID, and latency. Repeating the identical input must reuse the stored successful analysis.

For YouCam, use two reachable test image URLs and call the feasibility route:

```powershell
$headers = @{ Authorization = 'Bearer BEARER_TOKEN' }
$body = @{
  source_url = 'HTTPS_URL_TO_VALID_SHOPPER_IMAGE'
  reference_url = 'HTTPS_URL_TO_VALID_GARMENT_REFERENCE'
  garment_category = 'outerwear'
} | ConvertTo-Json

$run = Invoke-RestMethod -Method Post -Uri http://localhost:8000/v1/providers/feasibility/youcam -Headers $headers -ContentType 'application/json' -Body $body
$run
```

Leave the worker running and poll without blocking it:

```powershell
Invoke-RestMethod -Uri "http://localhost:8000/v1/providers/feasibility/youcam/$($run.run_id)" -Headers $headers
```

Accept the gate only when the task reaches `success`, the output exists in the `mirror-results` bucket, and the stored run includes task ID, attempts, and measured latency. Also confirm the chosen reference image and explicit garment category produce a usable result.

## 8. Production deployment

- Deploy `apps/web` to a Node host such as Vercel, or build `apps/web/Dockerfile` with the repository root as its Docker build context. Supply all `NEXT_PUBLIC_*` values as build variables because Next.js embeds them in the browser bundle.
- Deploy `services/api/Dockerfile` once as the API service.
- Deploy the same API image as a separate worker service with command `python -m app.worker`.
- Set `APP_ENV=production`, the public `WEB_ORIGIN`, all provider variables, and Supabase variables in the host secret manager.
- Keep at least one worker replica. Multiple replicas are safe because jobs are claimed with row locks.
- Use separate development and production Supabase projects and provider keys.

After deployment, run the complete real brand-to-shopper flow and verify stored manifests and provider results before exposing the campaign publicly.

Local Docker build commands are:

```powershell
docker build -f apps/web/Dockerfile -t mirra-web .
docker build -f services/api/Dockerfile -t mirra-api services/api
```

The worker uses the same `mirra-api` image with its command changed to `python -m app.worker`.
