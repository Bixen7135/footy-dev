# API Surface (v1)

## Auth
- POST `/auth/register`
- POST `/auth/login`
- POST `/auth/refresh`
- POST `/auth/logout`
- GET `/auth/me`
- POST `/auth/change-password`

## Catalog
- GET `/products`
- GET `/products/{slug_or_id}`
- GET `/products/{id}/similar`
- GET `/categories`
- GET `/tags`

## Cart/Wishlist/Orders
- GET `/cart`
- POST `/cart/items`
- PATCH `/cart/items/{item_id}`
- DELETE `/cart/items/{item_id}`
- DELETE `/cart`
- POST `/cart/merge`
- GET `/wishlist`
- POST `/wishlist`
- DELETE `/wishlist/{product_id}`
- POST `/orders`
- GET `/orders`
- GET `/orders/{id}`

## Admin imports/mappings
- POST `/admin/imports/run`
- GET `/admin/imports`
- GET `/admin/imports/{id}`
- POST `/admin/imports/{id}/retry`
- POST `/admin/imports/sync-products`
- POST `/admin/imports/bootstrap-exports`
- GET `/admin/source-products`
- POST `/admin/source-products/{id}/map`
- GET `/admin/source-systems`
- POST `/admin/source-systems`
- PATCH `/admin/source-systems/{id}`

## Admin catalog/media/orders/pages/jobs
- POST `/admin/products`
- PATCH `/admin/products/{id}`
- POST `/admin/products/{id}/tags`
- DELETE `/admin/products/{id}/tags/{tag_id}`
- POST `/admin/products/{id}/variants`
- PATCH `/admin/variants/{variant_id}`
- POST `/admin/media/upload`
- POST `/admin/products/{id}/images`
- DELETE `/admin/products/{id}/images/{image_id}`
- PATCH `/admin/orders/{id}/status`
- POST `/admin/orders/{id}/note`
- GET `/pages/{slug}`
- POST `/admin/pages`
- PATCH `/admin/pages/{id}`
- GET `/admin/jobs`
- POST `/admin/jobs/{job_name}/run`

## Events/Recommendations/Analytics
- POST `/events/batch`
- GET `/recommendations`
- GET `/me/recently-viewed`
- GET `/admin/recommendations/metrics`
- GET `/admin/recommendations/model`
- POST `/admin/recommendations/train`
- GET `/admin/recommendations/jobs`
- GET `/admin/analytics/overview`
- GET `/admin/analytics/products/{product_id}`
- GET `/admin/analytics/search-terms`
- GET `/admin/analytics/categories`
- GET `/admin/analytics/recommendations`
- GET `/health/ready`
