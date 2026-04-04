# Requires backend virtualenv activated
python -m worker.main --job normalize_source_catalog_job
python -m worker.main --job sync_source_to_store_job
python -m worker.main --job cleanup_stale_source_products_job
python -m worker.main --job aggregate_events_job
python -m worker.main --job recompute_trending_job
python -m worker.main --job cleanup_old_events_job
