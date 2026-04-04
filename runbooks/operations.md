# Operations Runbook

## Import retry
1. Login as admin.
2. Check `/admin/imports`.
3. Retry failed run with `POST /admin/imports/{id}/retry`.
4. Run sync with `POST /admin/imports/sync-products`.

## Bootstrap from exports (one-time)
1. Run `bun run dev:data:reset-bootstrap` for full local reset + bootstrap from `exports/` into `backend/footy.db`.
2. (Alternative) Trigger `POST /admin/imports/bootstrap-exports` or run `python scripts/bootstrap_from_exports.py` if you do not need full DB reset.
3. Check `job_runs` entry `bootstrap_exports_job` and verify `details_json.ok=true`.
4. Run `POST /admin/jobs/refresh_product_primary_images_job/run` and verify `details_json` counters (`candidates_total`, `validated_ok`, `updated_products`, `skipped_no_main`, `errors`).
5. Validate report file in `runbooks/reports/bootstrap-exports-*.json`.
6. Run storefront/admin smoke and recommendations checks (`home`, `pdp`, `cart`, `account`).
7. Only after green validation, remove `exports/` as a separate ops step (never auto-delete).

## Remap categories/gender in existing DB
1. Run `bun run dev:data:remap-taxonomy`.
2. Validate output metrics: `updated_products`, `updated_gender`, `legacy_categories_deactivated`, `unmapped_examples`.
3. Start shared stack: `bun run dev:all`.

## Repair product galleries in existing DB
1. Run `bun run dev:data:repair-media`.
2. Validate output metrics: `products_updated`, `rows_before`, `rows_after`, `rows_dropped`.
3. Start shared stack: `bun run dev:all`.

Working DB for local dev stack:
- `backend/footy.db`
- Root `footy.db` is not used by `dev:all`.

## Import from exports (without reset)
1. Trigger `POST /admin/imports/bootstrap-exports` or run `python scripts/bootstrap_from_exports.py`.
2. Check `job_runs` entry `bootstrap_exports_job` and verify `details_json.ok=true`.
3. Run `POST /admin/jobs/refresh_product_primary_images_job/run` and verify `details_json` counters (`candidates_total`, `validated_ok`, `updated_products`, `skipped_no_main`, `errors`).
4. Validate report file in `runbooks/reports/bootstrap-exports-*.json`.
5. Run storefront/admin smoke and recommendations checks (`home`, `pdp`, `cart`, `account`).
6. Only after green validation, remove `exports/` as a separate ops step (never auto-delete).

## Recommendation retrain
1. Build dataset: `POST /admin/jobs/build_training_dataset_job/run`.
2. Train model: `POST /admin/recommendations/train` (or `/admin/jobs/train_ranker_job/run`).
3. Check model status: `GET /admin/recommendations/model`.

## Stale cleanup
1. Trigger `cleanup_stale_source_products_job` from `/admin/jobs/{job_name}/run`.
2. Review product statuses in admin products list.

## Backup
- Run `scripts/backup_db.ps1` daily via Task Scheduler.

## Restore
- Run `scripts/restore_db.ps1 -BackupDb <path>`.

## Health checks
- API: `GET /health`
- API readiness: `GET /health/ready`
- Job monitor: `GET /admin/jobs`

## Auth refresh check
1. Login via `POST /auth/login`.
2. Validate `GET /auth/me`.
3. Rotate tokens with `POST /auth/refresh`.
4. Validate `GET /auth/me` again.
5. Logout with `POST /auth/logout`.

## Media upload (hybrid)
1. URL mode: `POST /admin/media/upload` with `source_url` form field.
2. Local mode: `POST /admin/media/upload` with multipart `file`.
3. Attach image to product via `POST /admin/products/{id}/images`.

## Local smoke
One-command local workflow:
1. Run smoke: `bun run dev:all:smoke`.
2. Smoke runs on isolated ports/DB and does not modify `backend/footy.db`.
3. If shared stack is running, smoke stops it first to avoid Next.js dev lock conflicts.
4. Logs are written to `.run/smoke-isolated/*`.
5. Start shared stack when needed: `bun run dev:all`.
