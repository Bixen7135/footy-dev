# FOOTY v1 MVP Commerce

Monorepo includes:
- `backend/` FastAPI + SQLModel + SQLite
- `frontend/` Next.js + TypeScript + Tailwind (Bun)
- `backend/worker/` scheduled jobs process

## Quickstart

1. Install dependencies once
```bash
cd backend
python -m venv .venv
. .venv/Scripts/activate
pip install -e .[dev]
copy ..\\.env.example .env
cd frontend
bun install
```

2. Reset and load real catalog from `exports/` into `backend/footy.db` (one-time or when you want a clean local DB)
```bash
bun run dev:data:reset-bootstrap
```
This flow now includes post-check assertions and fails if test/demo products or categories are detected.

2.0. Optional manual assert for DB cleanliness:
```bash
bun run dev:data:assert-clean
```

2.1. If DB is already loaded and you need to normalize categories + gender in place (without reset):
```bash
bun run dev:data:remap-taxonomy
```

2.2. If product galleries contain mixed/duplicate images, run one-time media repair for `backend/footy.db`:
```bash
bun run dev:data:repair-media
```

3. Start full local stack (one command)
```bash
bun run dev:all
```
This runs in the same terminal and streams logs. `Ctrl+C` stops backend + frontend + worker.

4. Run local smoke (isolated DB/ports, does not touch `backend/footy.db`)
```bash
bun run dev:all:smoke
```
If shared `dev:all` stack is running, smoke stops it first to avoid Next.js dev lock conflicts.
Direct smoke seeding against shared `backend/footy.db` is blocked.

5. Watch logs (attach mode)
```bash
bun run dev:all:logs
```

6. Stop stack
```bash
bun run dev:all:down
```

Working DB used by dev stack: `backend/footy.db`

## Docker (optional)

Docker is not required for local development.  
If you prefer containers, you can still run:
```bash
docker compose up --build
```
