# Imports & Sync (v1)

Flow:
1. `POST /admin/imports/run` upserts source items by `(source_system, source_product_id)`.
2. One-time historical bootstrap is available via `POST /admin/imports/bootstrap-exports` (or `python scripts/bootstrap_from_exports.py`).
3. Normalization marks items `pending -> normalized` (worker job or sync path).
4. Mapping is applied in `/admin/source-products/{id}/map`.
5. `POST /admin/imports/sync-products` creates/updates internal products.
6. Publication requires mapping + active price + usable image.
7. Manual admin overrides are tracked by field name and protected from sync overwrite.

Bootstrap notes:
- CSV schema is validated before load.
- Import is idempotent on key entities (source composite key, links, tags/variants/images dedup strategy).
- Report is saved in `runbooks/reports/bootstrap-exports-*.json` and echoed in `job_runs.details_json`.
- `exports/` deletion is manual and only after green validation/smoke.
- After bootstrap, run `POST /admin/jobs/refresh_product_primary_images_job/run` to backfill main (`big/MAIN.*`) primary images for card previews.
- For a clean local reset + bootstrap, run `bun run dev:data:reset-bootstrap` (targets `backend/footy.db`).
- `dev:data:reset-bootstrap` now runs strict post-checks and fails if test/demo categories or products are found.
- You can run a standalone DB cleanliness check with `bun run dev:data:assert-clean`.
- Smoke/tests must use isolated DBs only; shared `backend/footy.db` is not allowed for test data seeding.

Staleness:
- worker job `cleanup_stale_source_products_job` increments `miss_count`
- states transition to suspect/stale/deactivated based on threshold
