# Data Model (v1)

Core table groups:
- Source/import: `source_system_configs`, `source_catalog_items`, `source_product_links`, `source_categories_map`, `source_brands_map`, `source_attributes_map`, `import_runs`
- Catalog: `products`, `product_images`, `product_variants`, `categories`, `tags`, `product_tags`
- Commerce: `users`, `user_addresses`, `carts`, `cart_items`, `wishlist_items`, `orders`, `order_items`, `order_status_history`
- Analytics/recommendations: `user_events`, `job_runs`
- CMS: `cms_pages`

Business-critical constraints:
- Unique source dedup key: `(source_system, source_product_id)`
- Publication gate: mapped category + price + image
- Order idempotency unique key: `orders.idempotency_key`
- Order snapshots stored in `order_items`
