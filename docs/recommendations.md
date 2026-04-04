# Recommendations (v1)

Contexts:
- `home`
- `pdp`
- `cart`
- `account`

Candidate sources:
- recently viewed
- category affinity
- tag affinity
- recent orders
- co-view
- co-purchase
- content similarity
- session recency (anonymous)
- current category similarity (PDP/anonymous)
- trending fallback

Scoring:
- additive hybrid score: `source_weight + affinity + similarity + popularity + recency - penalties`
- optional online LightGBM rerank when model artifact is ready
- fallback to pure rule-based score when model is missing/invalid
- hard filters: inactive products, unavailable stock, current PDP product, duplicates

Events used:
- `product_view`, `product_dwell`, `add_to_cart`, `remove_from_cart`, `purchase`, `search`
- `listing_view`, `listing_impression`, `listing_click`, `variant_select`, `filter_apply`, `sort_change`, `checkout_start`, `wishlist_add`, `wishlist_remove`
- `recommendation_impression`, `recommendation_click`
