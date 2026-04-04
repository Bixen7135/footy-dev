# AGENTS.md

## Purpose
Project instructions for Codex.
Keep changes small, safe, and token-efficient.
If a detail is not needed for the ([developers.openai.com](https://developers.openai.com/codex/guides/agents-md/?utm_source=chatgpt.com))---

## Project Summary
FOOTY is an e-commerce platform built **after the parsing stage**.
An external parser sends parsed catalog data into this system.
This system must ingest, normalize, map, sync, publish, sell, analyze, and recommend products.

Core domains:
- source intake after parsing
- catalog normalization and sync
- storefront
- cart and checkout
- customer account
- admin panel
- analytics
- recommendations
- offline ranking pipeline

Out of scope:
- crawler/parser internals
- anti-bot logic
- direct rendering of raw source data

---

## Tech Stack
### Frontend
- Next.js
- React
- TypeScript
- Tailwind CSS
- React Hook Form + Zod

### Backend
- Python 3.12+
- FastAPI
- SQLAlchemy / SQLModel
- Pydantic
- Alembic
- JWT auth

### Data
- SQLite
- pandas or polars
- scikit-learn
- LightGBM or XGBoost

### JS Tooling
- **Always use Bun**
- Use `bun install`, `bun run <script>`, `bunx <tool>`
- Do not switch to npm/yarn/pnpm unless explicitly required

---

## Architecture Rules
- Storefront reads only from the **internal catalog**
- Raw source data is never rendered directly in UI
- Expensive processing must not run in request paths
- Parser and storefront stay loosely coupled through ingestion contract
- Keep handlers/routes thin
- Put business logic in services
- Keep DB logic out of route handlers
- Recommendation and analytics logic must stay modular
- Preserve API contracts unless task explicitly requires change

---

## What the System Must Support
### Catalog pipeline
- accept parsed source items
- normalize fields
- map source taxonomy to internal taxonomy
- sync into internal catalog
- publish only valid products

### Commerce
- catalog browsing
- product pages
- search and filters
- guest/auth cart
- checkout
- orders
- wishlist
- customer account

### Admin
- products
- categories
- tags
- variants
- media
- imports
- source mappings
- orders
- analytics
- recommendations
- jobs

### Intelligence
- event tracking
- recommendation blocks
- training dataset generation
- offline ranking pipeline

---

## Critical Business Rules
- Deduplicate imports by `(source_system, source_product_id)`
- Products without required mapping must not auto-publish
- Storefront publication requires mapped category, active price, and usable image
- Missing products become suspect/stale/deactivated based on staleness policy
- Checkout must recheck stock before order creation
- Orders must store item and price snapshots
- Recommendations must exclude inactive or unavailable items when relevant
- Manual admin overrides must not be blindly overwritten by sync jobs

---

## Minimal Domain Model Codex Should Remember
### Source/import side
- `source_catalog_items`
- `source_product_links`
- `source_categories_map`
- `source_brands_map`
- `source_attributes_map`
- `import_runs`

### Store catalog
- `products`
- `product_images`
- `product_variants`
- `categories`
- `tags`
- `product_tags`

### Commerce
- `users`
- `user_addresses`
- `carts`
- `cart_items`
- `wishlist_items`
- `orders`
- `order_items`
- `order_status_history`

### Analytics/recommendations
- user/session event storage
- aggregates for trending / affinity / similarity / ranking

Do not recreate the full schema from memory unless the task explicitly needs it.
Use the tech spec or migrations when exact fields matter.

---

## Required Analytics Events
At minimum, preserve support for:
- `product_view`
- `product_dwell`
- `add_to_cart`
- `remove_from_cart`
- `purchase`
- `search`
- `recommendation_impression`
- `recommendation_click`

Useful extended events when relevant:
- listing view / impression / click
- variant select
- filter apply
- sort change
- checkout start
- wishlist add/remove

Minimum event payload mindset:
- actor: `user_id` or `anonymous_id`
- `session_id`
- `event_type`
- `product_id` / `variant_id` / `category_id` if applicable
- page/context metadata
- dwell or timing data when applicable
- `created_at`

Dwell tracking rule:
- start on PDP mount
- pause on hidden tab
- resume on visible tab
- flush on unload or route change

---

## Recommendation Rules
Contexts:
- Home
- Product page
- Cart
- Account

Candidate sources may include:
- recently viewed
- category affinity
- tag affinity
- recent orders
- co-view
- co-purchase
- content similarity
- session-based recency
- trending fallback

MVP scoring idea:
`final_score = source_weight + affinity_score + similarity_score + popularity_score + recency_bonus - penalties`

Implicit feedback strength, from weaker to stronger:
- impression
- click
- view
- dwell
- wishlist
- add_to_cart
- purchase

Recommendation constraints:
- exclude inactive items
- exclude unavailable items when relevant
- exclude current PDP item from same slot
- avoid duplicates per response
- graceful fallback for low-data users

Prefer deterministic, inspectable logic before adding ML complexity.

---

## API Surface Codex Should Preserve
Main groups:
- auth
- catalog
- cart
- wishlist
- orders
- admin products/media/variants
- imports and source mapping
- events batch ingestion
- recommendations
- analytics admin views
- CMS pages

Do not change endpoint behavior or payload shape unless the task explicitly requires it.

---

## Implementation Rules for Codex
When given a task:
1. inspect only relevant files
2. identify smallest safe change set
3. keep changes local to touched feature
4. update tests if logic changes
5. summarize assumptions briefly

Default coding behavior:
- prefer targeted diffs over full rewrites
- prefer existing patterns over introducing new abstractions
- avoid renaming unrelated files
- avoid formatting churn
- avoid speculative refactors
- avoid changing infra or package manager
- keep comments short and useful

---

## Testing Rules
Whenever logic changes, add or update tests for the touched area.
Priority order:
1. service-level tests
2. API/integration tests for critical flows
3. regression tests for bugs

High-value test areas:
- import deduplication
- normalization and mapping
- publication rules
- stock recheck at checkout
- order snapshot correctness
- event persistence
- recommendation filtering and fallback
- manual override protection during sync

---

## Token Efficiency Rules for Codex
Keep this file lean.
Target: roughly **2k–4k tokens**, hard cap around **5k**.
If context grows, move details into `docs/` and load only when needed.

Recommended supporting files:
- `docs/progress.md` — running progress log
- `session_summary.md` — latest compact summary
- `docs/api.md` — exact API shapes if needed
- `docs/data-model.md` — exact schema if needed
- `docs/recommendations.md` — ranking details if needed
- `docs/imports.md` — ingestion and mapping details if needed

Rules:
- do not paste long code blocks into prompts unless necessary
- do not ask Codex to scan the whole repo by default
- do not load build artifacts, generated files, logs, or lockfiles unless needed
- load only the files relevant to the task
- prefer precise, spec-like prompts
- ask for diff-oriented output when possible

---

## Session Workflow Adapted for Codex
### Start of session
Load only what is needed:
- `AGENTS.md`
- `docs/progress.md` if recent work matters
- `session_summary.md` if continuing previous task
- any one or two task-specific docs

### During long work
Every ~30–40 messages or after a major milestone:
- create a short summary of changes, decisions, risks, and next steps
- save/update `session_summary.md`
- append durable progress to `docs/progress.md`

### End of session
Write down:
- what changed
- files touched
- tests run / not run
- current blockers
- exact next step

Do not keep giant conversational history as the only memory source.
Prefer durable markdown summaries.

---

## Prompt Style for Codex
Bad:
- vague requests
- “fix everything”
- pasting full repo context every time

Good:
- name exact file(s)
- name exact bug or feature
- state constraints
- state what must not change
- ask for concise output

Example:
`Update checkout stock validation in backend/services/orders.py. Preserve API behavior. Add tests. Return focused diff only.`

---

## What Not to Do
- do not implement parser internals
- do not replace SQLite unless explicitly requested
- do not add ML training pipelines before baseline rule-based recommendations are stable
- do not move expensive jobs into request handlers
- do not bypass mapping/review rules for source data
- do not overwrite admin-owned fields during sync
- do not introduce npm/yarn/pnpm defaults

---

## Default Decision Rule
If the request is ambiguous, choose the smallest maintainable solution that:
- preserves current behavior
- respects internal catalog boundaries
- is Bun-first on JS side
- is SQLite-compatible
- keeps analytics and recommendation logic modular
- minimizes token usage and repo churn