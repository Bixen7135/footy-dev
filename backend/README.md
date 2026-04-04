# FOOTY Backend

## Setup

```bash
cd backend
python -m venv .venv
. .venv/Scripts/activate
pip install -e .[dev]
copy ..\\.env.example .env
```

## Run API

```bash
uvicorn app.main:app --reload --port 8000
```

## Run worker

```bash
python -m worker.main --scheduler
```

## Run tests

```bash
pytest
```

## Seed demo data

```bash
python scripts/seed_demo_data.py
```

## Alembic

```bash
alembic -c alembic.ini revision --autogenerate -m "change"
alembic -c alembic.ini upgrade head
```
