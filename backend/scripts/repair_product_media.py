from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from sqlmodel import Session, select

from app.database import session_scope
from app.media_gallery import GalleryImageCandidate, normalize_image_url, sanitize_gallery_candidates
from app.models import ProductImage


def _resolve_image_url(image: ProductImage) -> str:
    if image.source_url and image.source_url.strip():
        return normalize_image_url(image.source_url)
    if image.file_path and image.file_path.strip():
        normalized = image.file_path.strip().lstrip("/\\").replace("\\", "/")
        return normalize_image_url(f"/uploads/{normalized}")
    return ""


def repair_product_media(session: Session, limit_per_product: int = 30) -> dict[str, Any]:
    rows = session.exec(
        select(ProductImage).order_by(
            ProductImage.product_id.asc(),
            ProductImage.is_primary.desc(),
            ProductImage.sort_order.asc(),
            ProductImage.id.asc(),
        )
    ).all()
    rows_by_product_id: dict[int, list[ProductImage]] = defaultdict(list)
    for row in rows:
        rows_by_product_id[int(row.product_id)].append(row)

    total_before = len(rows)
    products_processed = len(rows_by_product_id)
    products_updated = 0
    rows_deleted = 0
    rows_created = 0
    products_without_images = 0

    for product_id, product_rows in rows_by_product_id.items():
        before_urls = [url for url in (_resolve_image_url(row) for row in product_rows) if url]
        candidates: list[GalleryImageCandidate] = []
        for row in product_rows:
            resolved_url = _resolve_image_url(row)
            if not resolved_url:
                continue
            candidates.append(
                GalleryImageCandidate(
                    url=resolved_url,
                    sort_order=row.sort_order,
                    is_primary=row.is_primary,
                    payload=row,
                )
            )

        sanitized = sanitize_gallery_candidates(candidates, limit=limit_per_product)
        after_urls = [candidate.url for candidate in sanitized]
        if before_urls != after_urls or len(product_rows) != len(sanitized):
            products_updated += 1

        for row in product_rows:
            session.delete(row)
            rows_deleted += 1

        if not sanitized:
            products_without_images += 1
            continue

        for index, candidate in enumerate(sanitized):
            source_row: ProductImage = candidate.payload
            source_url = normalize_image_url(source_row.source_url) if source_row.source_url else None
            file_path = (
                source_row.file_path.strip().lstrip("/\\").replace("\\", "/")
                if source_row.file_path and source_row.file_path.strip()
                else None
            )
            session.add(
                ProductImage(
                    product_id=product_id,
                    storage_mode=source_row.storage_mode,
                    source_url=source_url,
                    file_path=file_path,
                    alt_text=source_row.alt_text or "",
                    sort_order=index,
                    is_primary=index == 0,
                    cdn_asset_id=source_row.cdn_asset_id,
                    created_at=source_row.created_at,
                )
            )
            rows_created += 1

    session.commit()

    return {
        "ok": True,
        "products_processed": products_processed,
        "products_updated": products_updated,
        "products_without_images": products_without_images,
        "rows_before": total_before,
        "rows_after": rows_created,
        "rows_deleted": rows_deleted,
        "rows_created": rows_created,
        "rows_dropped": max(total_before - rows_created, 0),
    }


def main() -> None:
    with session_scope() as session:
        report = repair_product_media(session=session)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
