from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from sqlmodel import Session, select

from app.database import session_scope
from app.models import Category, Product, SourceCatalogItem, SourceProductLink
from app.taxonomy import (
    CANONICAL_CATEGORIES,
    LEGACY_TECHNICAL_CATEGORY_SLUGS,
    normalize_gender_key,
    resolve_canonical_category,
    resolve_gender_key,
)


def _ensure_canonical_categories(session: Session) -> tuple[dict[str, Category], int, int]:
    created = 0
    updated = 0
    by_key: dict[str, Category] = {}
    for canonical in CANONICAL_CATEGORIES:
        existing = session.exec(select(Category).where(Category.slug == canonical.slug)).first()
        if existing is None:
            existing = Category(name=canonical.name, slug=canonical.slug, is_active=True)
            session.add(existing)
            session.flush()
            created += 1
        else:
            changed = False
            if existing.name != canonical.name:
                existing.name = canonical.name
                changed = True
            if not existing.is_active:
                existing.is_active = True
                changed = True
            if changed:
                session.add(existing)
                updated += 1
        by_key[canonical.key] = existing
    return by_key, created, updated


def _extract_root_gender(source_attributes_json: Any) -> Optional[str]:
    if isinstance(source_attributes_json, dict):
        value = source_attributes_json.get("root_gender")
        if isinstance(value, str):
            return value
    return None


def remap_catalog_taxonomy(session: Session) -> dict[str, Any]:
    category_by_key, categories_created, categories_updated = _ensure_canonical_categories(session)
    categories_by_id = {category.id: category for category in session.exec(select(Category)).all() if category.id is not None}

    source_rows = session.exec(
        select(
            SourceProductLink.product_id,
            SourceCatalogItem.source_category_path,
            SourceCatalogItem.source_attributes_json,
        )
        .select_from(SourceProductLink)
        .join(SourceCatalogItem, SourceCatalogItem.id == SourceProductLink.source_catalog_item_id)
    ).all()

    source_meta_by_product_id: dict[int, tuple[Optional[str], Optional[str]]] = {}
    for product_id, source_category_path, source_attributes_json in source_rows:
        if product_id is None:
            continue
        if product_id in source_meta_by_product_id:
            continue
        source_meta_by_product_id[int(product_id)] = (
            source_category_path,
            _extract_root_gender(source_attributes_json),
        )

    updated_products = 0
    updated_category = 0
    updated_gender = 0
    unresolved_count = 0
    unresolved_examples: list[str] = []
    unresolved_seen: set[str] = set()

    products = session.exec(select(Product)).all()
    for product in products:
        if product.id is None:
            continue
        source_meta = source_meta_by_product_id.get(int(product.id))

        fallback_text = ""
        source_path: Optional[str] = None
        root_gender: Optional[str] = None
        if source_meta:
            source_path, root_gender = source_meta
            fallback_text = source_path or ""
        else:
            category = categories_by_id.get(product.category_id)
            fallback_text = " ".join(part for part in [category.name if category else "", category.slug if category else ""] if part)

        resolution = resolve_canonical_category(source_path, fallback_text=fallback_text or product.name)
        if not resolution.matched:
            unresolved_count += 1
            if resolution.source_leaf and resolution.source_leaf not in unresolved_seen and len(unresolved_examples) < 20:
                unresolved_seen.add(resolution.source_leaf)
                unresolved_examples.append(resolution.source_leaf)

        target_category = category_by_key.get(resolution.key) or category_by_key[CANONICAL_CATEGORIES[0].key]
        target_gender = resolve_gender_key(root_gender, source_path) if source_meta else normalize_gender_key(product.gender)
        current_gender = normalize_gender_key(product.gender)

        changed = False
        if product.category_id != target_category.id:
            product.category_id = target_category.id
            updated_category += 1
            changed = True
        if current_gender != target_gender:
            product.gender = target_gender
            updated_gender += 1
            changed = True

        if changed:
            product.updated_at = datetime.utcnow()
            session.add(product)
            updated_products += 1

    legacy_categories_deactivated = 0
    for legacy_slug in sorted(LEGACY_TECHNICAL_CATEGORY_SLUGS):
        legacy = session.exec(select(Category).where(Category.slug == legacy_slug)).first()
        if legacy is None or not legacy.is_active:
            continue
        legacy.is_active = False
        session.add(legacy)
        legacy_categories_deactivated += 1

    session.commit()

    return {
        "ok": True,
        "categories_created": categories_created,
        "categories_updated": categories_updated,
        "updated_products": updated_products,
        "updated_category": updated_category,
        "updated_gender": updated_gender,
        "legacy_categories_deactivated": legacy_categories_deactivated,
        "unmapped_count": unresolved_count,
        "unmapped_examples": unresolved_examples,
        "canonical_categories": [
            {"key": category.key, "slug": category.slug, "name": category.name}
            for category in CANONICAL_CATEGORIES
        ],
    }


def main() -> None:
    with session_scope() as session:
        report = remap_catalog_taxonomy(session)
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
