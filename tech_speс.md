# FOOTY — TECH SPEC (Post-Parsing Scope)

Version: 2.1  
Document language: EN  
Goal: define **everything that must be built after the parsing stage** in order to launch a complete e-commerce website with analytics and recommendations.

---

# 1. Purpose of this document

This document describes the architecture and requirements for a system where **catalog data is already being parsed by an external process**, or the parser is being developed separately, while this TECH SPEC covers:
- receiving parsed data
- catalog normalization and synchronization
- storefront
- customer account
- admin panel
- cart and checkout
- user behavior analytics
- recommendations
- offline ML ranking
- operations, testing, and roadmap

This document does **not** specify how to implement the crawler/parser itself. Instead, it defines the contract that the parser must satisfy when sending data into the system.

---

# 2. Product scope

## 2.1 Included in scope
- source intake contract after parsing
- catalog normalization and taxonomy mapping
- internal catalog storage
- e-commerce storefront
- product detail pages
- search, filters, and categories
- guest and authenticated cart
- checkout and order management
- customer account
- admin panel
- product, media, tag, category, and variant management
- user analytics
- recommendation engine
- offline ML ranking pipeline
- storage, deployment, backup, retention, and monitoring

## 2.2 Out of scope
- parser/crawler implementation details
- anti-bot bypass strategies
- scraping legality questions
- real-time external marketplace synchronization

---

# 3. System concept

The system is built around the following flow:

1. An external parser extracts product cards and media from the source
2. The parsed results are sent to the ingestion layer
3. The ingestion layer stores source items and normalizes them
4. Normalized data is synchronized into the internal storefront catalog
5. The storefront uses the internal catalog as the source of truth
6. Users interact with catalog, cart, and orders
7. The system logs user behavior
8. Recommendations and ranking models are built from these events

---

# 4. Parser input contract

## 4.1 General requirement
The parser must send data in a stable, repeatable format suitable for incremental synchronization.

## 4.2 Minimum source item payload
Each source item must contain:
- `source_system`
- `source_product_id`
- `source_url`
- `title`
- `price` or an explicit unavailable marker
- `currency`
- `category_path` or equivalent taxonomy hint
- `images[]`
- `last_seen_at`

## 4.3 Extended payload
When available, the parser should also send:
- `description`
- `brand`
- `attributes`
- `sizes`
- `colors`
- `stock`
- `breadcrumbs`
- `compare_at_price`
- `gender`
- `product_type`
- `material`
- `season`
- `tags`, if derivable from source data

## 4.4 Supported delivery formats
Accepted integration modes:
- JSON files
- batch import from local storage
- API POST ingestion
- message queue or event stream (optional later)

Preferred MVP integration:
- JSON batch import or direct POST into a backend ingestion endpoint

## 4.5 Import idempotency
The same `source_product_id` under the same `source_system` must not create duplicates.

Primary deduplication key:
- `unique(source_system, source_product_id)`

---

# 5. Catalog normalization and synchronization

## 5.1 Purpose of the normalization layer
The parser outputs raw source items. The normalization layer transforms them into the internal catalog model.

It must:
- standardize field formats and naming
- map external categories to internal categories
- map brands
- map attributes and tags
- extract or construct variants
- process images
- decide whether to create a new product or update an existing one

## 5.2 Processing stages
1. `raw source item accepted`
2. `normalized`
3. `mapped to internal taxonomy`
4. `synced to storefront catalog`
5. `published`

## 5.3 Data quality contract
### Required fields at list-import stage
- `source_system`
- `source_product_id`
- `source_url`
- `title`
- `price` or unavailable marker
- `currency` when price exists
- `category_path`
- at least one image or a documented placeholder strategy

### Required fields for storefront publication
- internal category mapped
- product name
- active price
- at least one usable image reference
- resolved active/inactive product status

### Fields allowed to arrive later
- description
- detailed attributes
- stock by size
- extended gallery
- enriched tags

## 5.4 Taxonomy mapping
The system must support the following mapping layers:
- `source_categories_map`
- `source_brands_map`
- `source_attributes_map`

If mapping is missing:
- the product must not be auto-published
- it must appear in an admin review queue

## 5.5 Variant merge rules
If the source provides multiple sizes or colors:
- same model + different size/color should preferably become one internal product with variants
- if the differences are too large, separate internal products are allowed

Merge priority:
1. explicit source parent-child relationship
2. exact source model signature
3. normalized title + brand + category + attribute fingerprint

## 5.6 Staleness policy
If a product stops appearing in source data:
- after 1 miss → `suspect_missing`
- after 3 consecutive misses → `stale`
- after 7 misses or a configured age threshold → auto-deactivate in storefront unless manual override exists

---

# 6. System architecture

## 6.1 High-level components
- Frontend storefront/admin: Next.js
- Backend API: FastAPI
- Database: SQLite
- File storage: local uploads + external CDN references
- Ingest worker: import / normalize / sync
- Analytics jobs
- Recommendation jobs
- ML training jobs

## 6.2 Architecture principles
- the storefront reads only from the internal catalog
- source data is never rendered directly in the UI
- expensive work must not run in request paths
- parser and storefront remain loosely coupled via the ingestion contract

## 6.3 Main subsystems
1. Source intake
2. Catalog normalization
3. Internal catalog storage
4. Storefront
5. Cart and checkout
6. Customer account
7. Admin panel
8. Analytics
9. Recommendation engine
10. Offline ML pipeline
11. Operations and monitoring

---

# 7. Technical stack

## 7.1 Frontend
- TypeScript
- Next.js
- React
- Tailwind CSS or equivalent UI layer
- React Hook Form + Zod

## 7.2 Backend
- Python 3.12+
- FastAPI
- SQLAlchemy / SQLModel
- Pydantic
- Alembic
- JWT authentication
- passlib + bcrypt/argon2

## 7.3 Data and ML
- pandas or polars
- scikit-learn
- LightGBM or XGBoost

## 7.4 Storage
- SQLite as primary database
- uploads directory on persistent volume
- support for external CDN image URLs in image model

---

# 8. Core data model

## 8.1 Source-side entities

### source_system_configs
- id
- source_system
- base_url
- allowed_roots_json
- rate_limit_rps
- concurrency
- headers_json nullable
- storage_mode_default (`external_cdn`, `local_copy`, `uploaded`)
- is_active
- created_at
- updated_at

### source_catalog_items
- id
- source_system
- source_product_id
- source_url
- source_title
- source_description nullable
- source_brand nullable
- source_category_path
- source_attributes_json nullable
- source_price nullable
- source_compare_at_price nullable
- source_currency nullable
- source_stock_json nullable
- source_media_json nullable
- normalized_status (`pending`, `normalized`, `failed`)
- miss_count default 0
- last_seen_at
- created_at
- updated_at

### source_product_links
- id
- source_catalog_item_id
- product_id
- sync_status (`new`, `mapped`, `synced`, `stale`, `failed`)
- manual_override default false
- last_sync_at nullable
- error_message nullable
- created_at
- updated_at

### source_categories_map
- id
- source_system
- source_category_key
- internal_category_id
- confidence_score nullable
- created_at
- updated_at

### source_brands_map
- id
- source_system
- source_brand_key
- internal_brand_name
- confidence_score nullable
- created_at
- updated_at

### source_attributes_map
- id
- source_system
- source_attribute_key
- source_attribute_value
- internal_attribute_name
- internal_attribute_value
- confidence_score nullable
- created_at
- updated_at

### import_runs
- id
- source_system
- run_type (`import`, `normalize`, `sync`, `media`)
- status (`running`, `succeeded`, `failed`, `partial`)
- started_at
- finished_at nullable
- page_cursor nullable
- last_success_url nullable
- resume_token nullable
- retry_count default 0
- items_seen_count default 0
- items_created_count default 0
- items_updated_count default 0
- items_failed_count default 0
- error_summary nullable
- created_at
- updated_at

## 8.2 Internal catalog entities

### categories
- id
- name
- slug
- parent_id nullable
- is_active
- sort_order
- created_at
- updated_at

### tags
- id
- name
- slug
- is_active
- created_at
- updated_at

### products
- id
- slug
- name
- short_description
- description
- brand_name nullable
- price
- compare_at_price nullable
- currency
- category_id
- gender nullable
- product_type nullable
- material nullable
- season nullable
- is_active
- is_featured
- source_managed default true
- created_at
- updated_at

### product_images
- id
- product_id
- storage_mode (`external_cdn`, `local_copy`, `uploaded`)
- source_url nullable
- file_path nullable
- alt_text
- sort_order
- is_primary
- cdn_asset_id nullable
- created_at

### product_tags
- id
- product_id
- tag_id

### product_variants
- id
- product_id
- sku
- size nullable
- color nullable
- stock_quantity
- is_active
- created_at
- updated_at

---

# 9. Commerce model

### users
- id
- email
- password_hash
- full_name
- phone
- role (`customer`, `admin`)
- is_active
- created_at
- updated_at

### user_addresses
- id
- user_id
- full_name
- phone
- address_line1
- address_line2
- city
- postal_code
- country
- is_default
- created_at
- updated_at

### carts
- id
- user_id nullable
- anonymous_id nullable
- status (`active`, `converted`, `abandoned`)
- merged_into_cart_id nullable
- created_at
- updated_at

### cart_items
- id
- cart_id
- product_id
- variant_id nullable
- quantity
- unit_price_snapshot
- created_at
- updated_at

### wishlist_items
- id
- user_id
- product_id
- variant_id nullable
- created_at

### orders
- id
- order_number
- user_id nullable
- email
- full_name
- phone
- address_line1
- address_line2
- city
- postal_code
- country
- notes nullable
- subtotal_amount
- total_amount
- status
- idempotency_key nullable
- created_at
- updated_at

### order_items
- id
- order_id
- product_id
- variant_id nullable
- product_name_snapshot
- variant_snapshot_json
- unit_price_snapshot
- quantity
- line_total

### order_status_history
- id
- order_id
- previous_status nullable
- new_status
- changed_by_user_id nullable
- admin_note nullable
- created_at

---

# 10. Storefront specification

## 10.1 Public pages
Required pages:
- Home
- Catalog
- Category page
- Search results
- Product detail page
- Cart
- Checkout
- Login
- Register
- About
- Contact
- Delivery / Returns / FAQ
- Privacy Policy
- Terms of Service

## 10.2 Home page
Required blocks:
- hero
- featured products
- categories
- trending
- personalized recommendations
- recently viewed

## 10.3 Catalog and search
Required capabilities:
- filters
- sorting
- pagination or infinite scroll
- brand/category/tag filtering
- price range
- stock availability

## 10.4 Product detail page
Must display:
- gallery
- title
- price
- compare-at price
- description
- category
- tags
- variants
- availability
- add to cart
- wishlist
- similar and recommended products

## 10.5 Cart
Required features:
- line items
- quantity update
- remove item
- subtotal
- recommendations

## 10.6 Checkout
Required fields:
- full_name
- email
- phone
- address_line1
- city
- postal_code
- country

Rules:
- final stock recheck required
- order idempotency required
- guest checkout supported

---

# 11. Customer account

## 11.1 Account pages
- account overview
- profile
- address management
- order history
- order detail
- wishlist
- recently viewed

## 11.2 Required behavior
- authenticated users must have a persistent cart
- wishlist must stay synchronized across pages
- recommendations must use user history when available

---

# 12. Admin panel

## 12.1 Admin modules
- Dashboard
- Products
- Categories
- Tags
- Variants
- Media
- Orders
- Source imports
- Source mappings
- Analytics
- Recommendations
- Jobs
- CMS pages

## 12.2 Import admin
Must support:
- import run list
- sync status
- source products pending review
- mapping UI for categories, brands, and attributes
- retry failed imports
- stale product review

## 12.3 Product admin
Required features:
- create and edit product
- activate and deactivate product
- manage tags
- manage variants
- manage stock
- manage images
- manual override on source-managed products

## 12.4 Manual override policy
If an admin manually edits a source-managed product:
- overridden fields must be tracked
- sync jobs must not blindly overwrite admin-owned fields

A field ownership policy is required:
- source-owned fields
- admin-owned fields
- mergeable fields

---

# 13. API specification

## 13.1 Ingestion and admin import APIs
- `POST /admin/imports/run`
- `GET /admin/imports`
- `GET /admin/imports/{id}`
- `POST /admin/imports/{id}/retry`
- `POST /admin/imports/sync-products`
- `GET /admin/source-products`
- `POST /admin/source-products/{id}/map`
- `GET /admin/source-systems`
- `POST /admin/source-systems`
- `PATCH /admin/source-systems/{id}`

## 13.2 Auth APIs
- `POST /auth/register`
- `POST /auth/login`
- `POST /auth/logout`
- `GET /auth/me`
- `POST /auth/change-password`

## 13.3 Catalog APIs
- `GET /products`
- `GET /products/{slug_or_id}`
- `GET /categories`
- `GET /tags`
- `GET /products/{id}/similar`

## 13.4 Media admin APIs
- `POST /admin/media/upload`
- `POST /admin/products/{id}/images`
- `DELETE /admin/products/{id}/images/{image_id}`

## 13.5 Product admin APIs
- `POST /admin/products`
- `PATCH /admin/products/{id}`
- `POST /admin/products/{id}/tags`
- `DELETE /admin/products/{id}/tags/{tag_id}`
- `POST /admin/products/{id}/variants`
- `PATCH /admin/variants/{variant_id}`

## 13.6 Cart APIs
- `GET /cart`
- `POST /cart/items`
- `PATCH /cart/items/{item_id}`
- `DELETE /cart/items/{item_id}`
- `DELETE /cart`
- `POST /cart/merge`

## 13.7 Wishlist APIs
- `GET /wishlist`
- `POST /wishlist`
- `DELETE /wishlist/{product_id}`

## 13.8 Order APIs
- `POST /orders`
- `GET /orders`
- `GET /orders/{id}`
- `PATCH /admin/orders/{id}/status`
- `POST /admin/orders/{id}/note`

## 13.9 CMS APIs
- `GET /pages/{slug}`
- `POST /admin/pages`
- `PATCH /admin/pages/{id}`

## 13.10 Event APIs
- `POST /events/batch`

## 13.11 Recommendation APIs
- `GET /recommendations`
- `GET /me/recently-viewed`
- `GET /admin/recommendations/metrics`
- `GET /admin/recommendations/jobs`

## 13.12 Analytics admin APIs
- `GET /admin/analytics/overview`
- `GET /admin/analytics/products/{product_id}`
- `GET /admin/analytics/search-terms`
- `GET /admin/analytics/categories`
- `GET /admin/analytics/recommendations`

---

# 14. Analytics specification

## 14.1 Goals
The analytics layer must support:
- behavior tracking
- funnel analysis
- product engagement analysis
- user preference modeling
- recommendation evaluation
- ML training data generation

## 14.2 Required events
- `listing_view`
- `product_impression`
- `product_click`
- `product_view`
- `product_dwell`
- `product_scroll_depth`
- `variant_select`
- `add_to_cart`
- `remove_from_cart`
- `wishlist_add`
- `wishlist_remove`
- `search`
- `filter_apply`
- `sort_change`
- `checkout_start`
- `purchase`
- `recommendation_impression`
- `recommendation_click`

## 14.3 Event fields
Each event should include, where applicable:
- event_type
- user_id or anonymous_id
- session_id
- product_id
- variant_id
- category_id
- source_page
- page_url
- rank_position
- dwell_ms
- recommendation_slot
- recommendation_request_id
- metadata_json
- created_at

## 14.4 Dwell time rules
- timer starts on PDP mount
- timer pauses when tab is hidden
- timer resumes when tab becomes visible
- final dwell event is flushed on unload/route change using `sendBeacon`

---

# 15. Recommendation engine specification

## 15.1 Goal
Provide contextual and personalized product recommendations using user behavior and normalized catalog data.

## 15.2 Contexts
- Home
- Product detail page
- Cart
- Account

## 15.3 Candidate sources
For authenticated users:
- recently viewed
- category affinity
- tag affinity
- recent orders
- co-view products
- co-purchase products
- content similarity

For anonymous users:
- current product similarity
- current category similarity
- recent session views
- trending fallback

## 15.4 Scoring approach
MVP hybrid scoring:

`final_score = source_weight + affinity_score + similarity_score + popularity_score + recency_bonus - penalties`

## 15.5 Implicit feedback weights
- impression = 0.05
- click = 1.0
- view = 2.0
- dwell > 10 seconds = 3.0
- wishlist = 5.0
- add_to_cart = 6.0
- purchase = 10.0

Apply time decay to favor recent actions.

## 15.6 Filters
Recommendations must exclude:
- inactive products
- unavailable or out-of-stock products/variants when relevant
- the current product on PDP
- duplicates in the same slot/request

---

# 16. Offline ML ranking

## 16.1 Goal
Improve ranking quality of recommendation candidates using historical interaction data.

## 16.2 Initial model choice
- candidate generation: hybrid retrieval
- ranker: LightGBM or XGBoost

## 16.3 Training rows
One row per `(user_or_session, context, candidate_product)`.

## 16.4 Feature groups
User features:
- top categories
- top tags
- average viewed price
- average purchased price
- last interaction recency
- order counts

Product features:
- category
- tags
- brand
- price bucket
- trend score
- CTR proxy
- conversion proxy

User-item features:
- category affinity match
- tag overlap
- viewed similar products
- purchased same category
- price distance to preference

Context features:
- slot/context
- current product on PDP
- cart categories
- optional device/time bucket

## 16.5 Labels
Primary labels:
- click
- add_to_cart
- purchase

## 16.6 Split strategy
Time-based only.

## 16.7 Negative sampling
- shown but not clicked
- clicked but not purchased
- random same-category negatives

---

# 17. Jobs and scheduling

Required jobs:
- `normalize_source_catalog_job`
- `sync_source_to_store_job`
- `download_or_proxy_media_job`
- `cleanup_stale_source_products_job`
- `aggregate_events_job`
- `recompute_recently_viewed_job`
- `recompute_trending_job`
- `recompute_similarity_job`
- `build_training_dataset_job`
- `train_ranker_job`
- `cleanup_old_events_job`

Suggested schedule:
- normalization: after each import batch
- source-to-store sync: after normalization or on demand
- media copy/proxy: async after sync
- stale source cleanup: daily
- event aggregation: hourly
- recently viewed: every 30–60 minutes
- trending: hourly or daily
- similarity: daily
- training: daily or weekly depending on traffic
- old-event cleanup: daily

---

# 18. Security and operations

## 18.1 Security requirements
- secure password hashing
- JWT-based auth
- role-based access control
- server-side validation on all writes
- upload validation for media
- rate limiting for sensitive endpoints

## 18.2 Storage and backups
- SQLite database on persistent volume
- uploads directory on persistent volume
- daily DB backup
- daily uploads backup

## 18.3 Retention
- raw user events retained for 180 days by default
- aggregates retained longer
- stale import data retained for audit/debugging

## 18.4 Monitoring
- structured logs
- import run logs
- job status monitoring
- admin status screens for imports, analytics jobs, and ML jobs

---

# 19. Roadmap

## Phase 1 — Foundation
1. SQLite setup and migrations
2. Auth and roles
3. Internal catalog schema
4. Public storefront shell

## Phase 2 — Post-parsing ingestion
5. Source intake endpoints or batch import
6. Source tables and import run tracking
7. Taxonomy mapping layer
8. Normalization pipeline
9. Source-to-store sync
10. Media storage strategy
11. Staleness handling

## Phase 3 — Commerce core
12. Cart guest/auth flows
13. Checkout and order creation
14. Order history and account pages
15. Wishlist
16. Admin product/category/tag management
17. Variants and stock

## Phase 4 — Analytics
18. Event contract implementation
19. Frontend instrumentation
20. Event storage
21. Admin analytics dashboards

## Phase 5 — Recommendations
22. Recently viewed
23. Affinity and trending jobs
24. Similarity jobs
25. Recommendation API
26. Recommendation UI slots
27. Recommendation metrics

## Phase 6 — ML
28. Training dataset builder
29. Ranking model training
30. Model versioning
31. Reranking integration

## Phase 7 — Hardening
32. Full test suite
33. Backup and retention automation
34. Performance tuning
35. Deployment scripts

---

# 20. Acceptance criteria

The implementation is considered complete when:
- parsed catalog data can be ingested, normalized, and synchronized into the internal store
- products are visible in storefront only after meeting publication rules
- users can browse, search, filter, and open product pages
- users can add items to cart and place orders as guest or customer
- customers can manage wishlist, profile, and orders
- admins can manage products, mappings, media, variants, imports, and orders
- user events are logged reliably
- recommendation blocks appear in required contexts
- offline ranking pipeline exists and produces model versions
- the system runs using SQLite as the primary database

---

# 21. Final note

This specification assumes that parsing is already handled outside the core storefront platform. The purpose of the platform is to turn parsed raw catalog data into a maintainable, purchasable, analyzable, and optimizable e-commerce product.

In other words:
- the parser finds products
- this system turns them into a real online store

