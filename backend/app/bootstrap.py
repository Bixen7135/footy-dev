from __future__ import annotations

import csv
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

from sqlmodel import Session, func, select

from .media_gallery import GalleryImageCandidate, normalize_image_url, sanitize_gallery_candidates
from .models import (
    Category,
    ImportRun,
    Product,
    ProductImage,
    ProductStatus,
    ProductTag,
    ProductVariant,
    RunStatus,
    RunType,
    SourceCatalogItem,
    SourceProductLink,
    SyncStatus,
    Tag,
)
from .taxonomy import (
    CANONICAL_CATEGORIES,
    LEGACY_TECHNICAL_CATEGORY_SLUGS,
    resolve_canonical_category,
    resolve_gender_key,
)
from .size_utils import decode_shoe_size_with_quantity

DEFAULT_VARIANT_COLOR = "Other"

field_limit = sys.maxsize
while True:
    try:
        csv.field_size_limit(field_limit)
        break
    except OverflowError:
        field_limit //= 10


def slugify(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned or "item"


def _model_key(brand_name: Optional[str], product_name: Optional[str]) -> str:
    brand = (brand_name or "").strip().lower()
    name = (product_name or "").strip().lower()
    return f"{brand}|{name}"


def _clean_color_value(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    value = raw.strip()
    if not value:
        return None
    if "|" in value:
        value = value.split("|", 1)[0].strip()
    value = re.sub(r"\s*-\s*купить.*$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*-\s*buy.*$", "", value, flags=re.IGNORECASE)
    value = value.strip(" -")
    return value or None


def _now() -> datetime:
    return datetime.utcnow()


def _parse_json(raw: Optional[str], fallback):
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return fallback


def _parse_datetime(raw: Optional[str], fallback: Optional[datetime] = None) -> datetime:
    if raw:
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            pass
    return fallback or _now()


def _parse_decimal(raw: Optional[str]) -> Optional[Decimal]:
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return None


def _parse_bool(raw: Optional[str], default: bool = False) -> bool:
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "y"}:
        return True
    if value in {"0", "false", "no", "n"}:
        return False
    return default


def _resolve_gender_from_source_row(row: dict[str, str], attributes: dict[str, Any]) -> Optional[str]:
    details = attributes.get("details")
    if not isinstance(details, dict):
        details = {}
    return resolve_gender_key(
        row.get("root_gender")
        or attributes.get("root_gender")
        or details.get("root_gender")
        or details.get("pol")
        or attributes.get("pol"),
        row.get("source_category_path"),
    )


def _parse_int(raw: Optional[str], default: int = 0) -> int:
    if raw is None:
        return default
    value = raw.strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _source_media_to_urls(source_media_raw: Optional[str], normalized_payload_raw: Optional[str]) -> list[str]:
    urls: list[str] = []

    source_media = _parse_json(source_media_raw, None)
    if isinstance(source_media, list):
        for item in source_media:
            if isinstance(item, str) and item.strip():
                urls.append(item.strip())
    elif isinstance(source_media, dict):
        for key in ("card_image_urls", "gallery_urls"):
            for item in source_media.get(key, []) or []:
                if isinstance(item, str) and item.strip():
                    urls.append(item.strip())

    normalized_payload = _parse_json(normalized_payload_raw, {})
    if isinstance(normalized_payload, dict):
        for item in normalized_payload.get("media_urls", []) or []:
            if isinstance(item, str) and item.strip():
                urls.append(item.strip())

    seen: set[str] = set()
    deduped: list[str] = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        deduped.append(url)
    return deduped


def _source_stock_to_map(source_stock_raw: Optional[str]) -> dict[str, int]:
    payload = _parse_json(source_stock_raw, {})
    if not isinstance(payload, dict):
        return {}

    def _parse_quantity(raw_value: Any) -> Optional[int]:
        if raw_value is None:
            return None
        if isinstance(raw_value, bool):
            return 1 if raw_value else 0
        if isinstance(raw_value, (int, float)):
            return max(int(raw_value), 0)
        if isinstance(raw_value, str):
            cleaned = raw_value.strip()
            if not cleaned:
                return None
            try:
                return max(int(float(cleaned.replace(",", "."))), 0)
            except ValueError:
                return None
        if isinstance(raw_value, dict):
            for key in ("qty", "quantity", "stock", "value", "count"):
                if key in raw_value:
                    return _parse_quantity(raw_value.get(key))
        return None

    mapped: dict[str, int] = {}
    def _add_size(raw_size: Any, raw_qty: Any) -> None:
        normalized_size, encoded_quantity = decode_shoe_size_with_quantity(raw_size)
        if not normalized_size:
            return
        quantity = _parse_quantity(raw_qty)
        if quantity is None:
            quantity = encoded_quantity
        if quantity is None:
            quantity = 1
        current = mapped.get(normalized_size)
        if current is None or quantity > current:
            mapped[normalized_size] = quantity

    if isinstance(payload.get("sizes"), list):
        stock_by_size = payload.get("stock_by_size") if isinstance(payload.get("stock_by_size"), dict) else {}
        for raw_size in payload["sizes"]:
            raw_key = str(raw_size).strip() if raw_size is not None else ""
            raw_quantity = stock_by_size.get(raw_key)
            if raw_quantity is None:
                normalized_key, _ = decode_shoe_size_with_quantity(raw_size)
                if normalized_key:
                    raw_quantity = stock_by_size.get(normalized_key)
            _add_size(raw_size, raw_quantity)
        return mapped

    for key, value in payload.items():
        _add_size(key, value)
    return mapped


def _map_sync_status(raw: Optional[str]) -> SyncStatus:
    value = (raw or "").strip().lower()
    mapping = {
        "new": SyncStatus.NEW,
        "mapped": SyncStatus.MAPPED,
        "synced": SyncStatus.SYNCED,
        "stale": SyncStatus.STALE,
        "failed": SyncStatus.FAILED,
    }
    return mapping.get(value, SyncStatus.SYNCED)


def _map_run_status(raw: Optional[str]) -> RunStatus:
    value = (raw or "").strip().lower()
    mapping = {
        "running": RunStatus.RUNNING,
        "completed": RunStatus.SUCCEEDED,
        "succeeded": RunStatus.SUCCEEDED,
        "failed": RunStatus.FAILED,
        "partial": RunStatus.PARTIAL,
    }
    return mapping.get(value, RunStatus.PARTIAL)


def _map_run_type(raw: Optional[str]) -> RunType:
    value = (raw or "").strip().lower()
    mapping = {
        "import": RunType.IMPORT,
        "crawl": RunType.IMPORT,
        "normalize": RunType.NORMALIZE,
        "sync": RunType.SYNC,
        "media": RunType.MEDIA,
    }
    return mapping.get(value, RunType.IMPORT)


@dataclass
class ExportBootstrapService:
    exports_dir: Path
    report_dir: Path

    REQUIRED_FILES = {
        "source_catalog_items.csv",
        "source_product_links.csv",
        "products.csv",
        "product_images.csv",
        "product_variants.csv",
        "product_tags.csv",
        "import_runs.csv",
    }
    REQUIRED_COLUMNS = {
        "source_catalog_items.csv": {
            "source_system",
            "source_product_id",
            "source_url",
            "source_title",
            "source_category_path",
            "source_currency",
            "last_seen_at",
        },
        "source_product_links.csv": {
            "source_system",
            "source_product_id",
            "product_id",
            "sync_status",
        },
        "products.csv": {
            "id",
            "title",
            "brand_name",
            "category_slug",
            "current_price",
            "currency",
            "is_active",
        },
        "product_images.csv": {
            "product_id",
            "storage_mode",
            "source_url",
            "file_path",
            "sort_order",
            "is_primary",
        },
        "product_variants.csv": {
            "id",
            "product_id",
            "stock_status",
        },
        "product_tags.csv": {
            "product_id",
            "tag",
        },
        "import_runs.csv": {
            "source_system",
            "run_type",
            "status",
            "items_seen_count",
            "started_at",
        },
    }

    def _load_csv(self, filename: str) -> tuple[list[dict[str, str]], list[str]]:
        path = self.exports_dir / filename
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = [dict(row) for row in reader]
            return rows, reader.fieldnames or []

    def _validate(self) -> list[str]:
        errors: list[str] = []
        for filename in sorted(self.REQUIRED_FILES):
            path = self.exports_dir / filename
            if not path.exists():
                errors.append(f"Missing file: {filename}")
                continue
            with path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                fields = set(reader.fieldnames or [])
            missing_columns = sorted(self.REQUIRED_COLUMNS.get(filename, set()) - fields)
            if missing_columns:
                errors.append(f"Missing columns in {filename}: {', '.join(missing_columns)}")
        return errors

    def _ensure_category(self, session: Session, slug: str, fallback_name: str) -> Category:
        normalized_slug = slugify(slug)
        existing = session.exec(select(Category).where(Category.slug == normalized_slug)).first()
        if existing:
            normalized_name = fallback_name.strip() or existing.name or normalized_slug.title()
            if existing.name != normalized_name:
                existing.name = normalized_name
                session.add(existing)
            if not existing.is_active:
                existing.is_active = True
                session.add(existing)
            return existing
        row = Category(name=fallback_name.strip() or normalized_slug.title(), slug=normalized_slug, is_active=True)
        session.add(row)
        session.flush()
        return row

    def bootstrap(self, session: Session) -> dict[str, Any]:
        report: dict[str, Any] = {
            "ok": False,
            "started_at": _now().isoformat(),
            "exports_dir": str(self.exports_dir),
            "rows": {},
            "created": {},
            "updated": {},
            "skipped": {},
            "errors": [],
            "reconciliation": {},
        }

        validation_errors = self._validate()
        if validation_errors:
            report["errors"].extend(validation_errors)
            report["finished_at"] = _now().isoformat()
            return report

        source_rows, _ = self._load_csv("source_catalog_items.csv")
        source_link_rows, _ = self._load_csv("source_product_links.csv")
        product_rows, _ = self._load_csv("products.csv")
        product_image_rows, _ = self._load_csv("product_images.csv")
        product_variant_rows, _ = self._load_csv("product_variants.csv")
        product_tag_rows, _ = self._load_csv("product_tags.csv")
        import_run_rows, _ = self._load_csv("import_runs.csv")

        report["rows"] = {
            "source_catalog_items": len(source_rows),
            "source_product_links": len(source_link_rows),
            "products": len(product_rows),
            "product_images": len(product_image_rows),
            "product_variants": len(product_variant_rows),
            "product_tags": len(product_tag_rows),
            "import_runs": len(import_run_rows),
        }

        created = report["created"]
        updated = report["updated"]
        skipped = report["skipped"]
        created.update(
            {
                "source_catalog_items": 0,
                "products": 0,
                "source_product_links": 0,
                "product_images": 0,
                "product_variants": 0,
                "tags": 0,
                "product_tags": 0,
                "import_runs": 0,
                "categories": 0,
                "variant_color_backfilled": 0,
            }
        )
        updated.update(
            {
                "source_catalog_items": 0,
                "products": 0,
                "source_product_links": 0,
                "product_images": 0,
                "product_variants": 0,
                "import_runs": 0,
                "categories": 0,
            }
        )
        skipped.update({"rows": 0})

        canonical_categories_by_key: dict[str, Category] = {}
        for canonical in CANONICAL_CATEGORIES:
            existing = session.exec(select(Category).where(Category.slug == canonical.slug)).first()
            category = self._ensure_category(session, canonical.slug, canonical.name)
            if existing is None:
                created["categories"] += 1
            elif category.name != canonical.name or not category.is_active:
                updated["categories"] += 1
            canonical_categories_by_key[canonical.key] = category

        session.flush()

        source_key_to_item_id: dict[tuple[str, str], int] = {}
        source_key_to_category_key: dict[tuple[str, str], str] = {}
        source_key_to_gender_key: dict[tuple[str, str], str] = {}
        source_url_to_category_key: dict[str, str] = {}
        source_url_to_gender_key: dict[str, str] = {}
        source_product_id_to_gender_key: dict[tuple[str, str], str] = {}
        category_fallback_examples: list[str] = []
        category_fallback_seen: set[str] = set()
        category_fallback_count = 0
        for row in source_rows:
            source_system = (row.get("source_system") or "").strip()
            source_product_id = (row.get("source_product_id") or "").strip()
            if not source_system or not source_product_id:
                skipped["rows"] += 1
                continue

            key = (source_system, source_product_id)
            existing = session.exec(
                select(SourceCatalogItem).where(
                    SourceCatalogItem.source_system == source_system,
                    SourceCatalogItem.source_product_id == source_product_id,
                )
            ).first()

            attributes = _parse_json(row.get("source_attributes_json"), {})
            if not isinstance(attributes, dict):
                attributes = {}
            attributes["root_gender"] = row.get("root_gender")
            attributes["root_category"] = row.get("root_category")
            attributes["listing_page"] = row.get("listing_page")
            attributes["listing_position"] = row.get("listing_position")

            normalized_status = (row.get("normalized_status") or "pending").strip().lower() or "pending"
            if normalized_status not in {"pending", "normalized", "failed"}:
                normalized_status = "pending"

            payload = {
                "source_url": (row.get("source_url") or "").strip(),
                "source_title": (row.get("source_title") or "").strip() or source_product_id,
                "source_description": row.get("source_description"),
                "source_brand": row.get("source_brand"),
                "source_category_path": (row.get("source_category_path") or "uncategorized").strip(),
                "source_attributes_json": attributes,
                "source_price": _parse_decimal(row.get("source_price")),
                "source_compare_at_price": _parse_decimal(row.get("source_compare_at_price")),
                "source_currency": (row.get("source_currency") or "KZT").strip().upper(),
                "source_stock_json": _source_stock_to_map(row.get("source_stock_json")),
                "source_media_json": _source_media_to_urls(
                    row.get("source_media_json"),
                    row.get("normalized_payload_json"),
                ),
                "normalized_status": normalized_status,
                "last_seen_at": _parse_datetime(row.get("last_seen_at")),
            }
            category_resolution = resolve_canonical_category(payload["source_category_path"])
            if not category_resolution.matched:
                category_fallback_count += 1
                if category_resolution.source_leaf and category_resolution.source_leaf not in category_fallback_seen:
                    category_fallback_seen.add(category_resolution.source_leaf)
                    if len(category_fallback_examples) < 20:
                        category_fallback_examples.append(category_resolution.source_leaf)
            source_key_to_category_key[key] = category_resolution.key
            source_url = payload["source_url"]
            if source_url:
                source_url_to_category_key[source_url] = category_resolution.key
            gender_key = _resolve_gender_from_source_row(row, attributes)
            if gender_key:
                source_key_to_gender_key[key] = gender_key
                source_product_id_to_gender_key[key] = gender_key
                if source_url:
                    source_url_to_gender_key[source_url] = gender_key

            if existing:
                for key_name, value in payload.items():
                    setattr(existing, key_name, value)
                existing.updated_at = _now()
                session.add(existing)
                source_item = existing
                updated["source_catalog_items"] += 1
            else:
                source_item = SourceCatalogItem(
                    source_system=source_system,
                    source_product_id=source_product_id,
                    **payload,
                )
                session.add(source_item)
                created["source_catalog_items"] += 1
            session.flush()
            source_key_to_item_id[key] = source_item.id

        export_product_id_to_internal_id: dict[int, int] = {}
        source_key_to_internal_product_id: dict[tuple[str, str], int] = {}
        for row in product_rows:
            source_system = (row.get("external_source_system") or "").strip()
            source_product_id = (row.get("external_source_product_id") or "").strip()
            source_key = (source_system, source_product_id)
            source_item_id = source_key_to_item_id.get(source_key)

            linked_product: Optional[Product] = None
            if source_item_id:
                link = session.exec(
                    select(SourceProductLink).where(SourceProductLink.source_catalog_item_id == source_item_id)
                ).first()
                if link:
                    linked_product = session.get(Product, link.product_id)

            slug_candidate = slugify(f"{row.get('title') or source_product_id}-{source_product_id}")
            product = linked_product or session.exec(select(Product).where(Product.slug == slug_candidate)).first()

            canonical_url = (row.get("canonical_url") or "").strip()
            category_key = source_key_to_category_key.get(source_key) or source_url_to_category_key.get(canonical_url)
            if not category_key:
                fallback_resolution = resolve_canonical_category(
                    None,
                    fallback_text=row.get("category_slug") or row.get("title") or "shoes",
                )
                category_key = fallback_resolution.key
            category = canonical_categories_by_key.get(category_key)
            if not category:
                fallback = resolve_canonical_category(None)
                category = self._ensure_category(session, fallback.slug, fallback.name)
                canonical_categories_by_key[fallback.key] = category
            price = _parse_decimal(row.get("current_price")) or Decimal("0")
            compare_price = _parse_decimal(row.get("compare_at_price"))
            has_active_price = price > 0
            is_active = _parse_bool(row.get("is_active"), True) and has_active_price
            gender_key = (
                source_key_to_gender_key.get(source_key)
                or source_url_to_gender_key.get(canonical_url)
                or source_product_id_to_gender_key.get((source_system, source_product_id))
                or resolve_gender_key(row.get("gender") or row.get("pol"))
            )

            if product:
                product.name = (row.get("title") or "").strip() or product.name
                product.short_description = row.get("description") or product.short_description
                product.description = row.get("description") or product.description
                product.brand_name = row.get("brand_name") or product.brand_name
                product.price = price
                product.compare_at_price = compare_price
                product.currency = (row.get("currency") or product.currency or "KZT").upper()
                product.category_id = category.id
                product.gender = gender_key
                product.is_active = is_active
                product.status = ProductStatus.ACTIVE if is_active else ProductStatus.INACTIVE
                product.source_managed = True
                product.updated_at = _now()
                session.add(product)
                updated["products"] += 1
            else:
                product = Product(
                    slug=slug_candidate,
                    name=(row.get("title") or source_product_id or "Imported product").strip(),
                    short_description=row.get("description"),
                    description=row.get("description"),
                    brand_name=row.get("brand_name"),
                    price=price,
                    compare_at_price=compare_price,
                    currency=(row.get("currency") or "KZT").upper(),
                    category_id=category.id,
                    gender=gender_key,
                    is_active=is_active,
                    status=ProductStatus.ACTIVE if is_active else ProductStatus.INACTIVE,
                    source_managed=True,
                )
                session.add(product)
                created["products"] += 1
            session.flush()

            if source_item_id:
                link = session.exec(
                    select(SourceProductLink).where(SourceProductLink.source_catalog_item_id == source_item_id)
                ).first()
                if link:
                    link.product_id = product.id
                    link.sync_status = SyncStatus.SYNCED
                    link.last_sync_at = _now()
                    session.add(link)
                    updated["source_product_links"] += 1
                else:
                    session.add(
                        SourceProductLink(
                            source_catalog_item_id=source_item_id,
                            product_id=product.id,
                            sync_status=SyncStatus.SYNCED,
                            last_sync_at=_now(),
                        )
                    )
                    created["source_product_links"] += 1

            raw_export_product_id = row.get("id")
            if raw_export_product_id and raw_export_product_id.isdigit():
                export_product_id_to_internal_id[int(raw_export_product_id)] = product.id
            source_key_to_internal_product_id[source_key] = product.id

        legacy_categories_deactivated = 0
        for legacy_slug in sorted(LEGACY_TECHNICAL_CATEGORY_SLUGS):
            legacy_category = session.exec(select(Category).where(Category.slug == legacy_slug)).first()
            if not legacy_category or not legacy_category.is_active:
                continue
            legacy_category.is_active = False
            session.add(legacy_category)
            legacy_categories_deactivated += 1

        for row in source_link_rows:
            source_system = (row.get("source_system") or "").strip()
            source_product_id = (row.get("source_product_id") or "").strip()
            if not source_system or not source_product_id:
                continue
            source_key = (source_system, source_product_id)
            source_item_id = source_key_to_item_id.get(source_key)
            if not source_item_id:
                continue

            product_id = None
            raw_export_product_id = row.get("product_id")
            if raw_export_product_id and raw_export_product_id.isdigit():
                product_id = export_product_id_to_internal_id.get(int(raw_export_product_id))
            if not product_id:
                product_id = source_key_to_internal_product_id.get(source_key)
            if not product_id:
                continue

            link = session.exec(
                select(SourceProductLink).where(SourceProductLink.source_catalog_item_id == source_item_id)
            ).first()
            if link:
                link.product_id = product_id
                link.sync_status = _map_sync_status(row.get("sync_status"))
                link.last_sync_at = _parse_datetime(row.get("last_synced_at"), fallback=_now())
                session.add(link)
                updated["source_product_links"] += 1
            else:
                session.add(
                    SourceProductLink(
                        source_catalog_item_id=source_item_id,
                        product_id=product_id,
                        sync_status=_map_sync_status(row.get("sync_status")),
                        last_sync_at=_parse_datetime(row.get("last_synced_at"), fallback=_now()),
                    )
                )
                created["source_product_links"] += 1

            source_item = session.get(SourceCatalogItem, source_item_id)
            if source_item:
                missing_runs_count = row.get("missing_runs_count") or "0"
                try:
                    source_item.miss_count = max(int(missing_runs_count), 0)
                except ValueError:
                    source_item.miss_count = 0
                session.add(source_item)

        existing_images = {
            (
                image.product_id,
                image.source_url or "",
                image.file_path or "",
                image.sort_order,
            )
            for image in session.exec(select(ProductImage)).all()
        }
        image_candidates_by_product: dict[int, list[GalleryImageCandidate]] = {}
        for row in product_image_rows:
            raw_export_product_id = row.get("product_id") or ""
            if not raw_export_product_id.isdigit():
                skipped["rows"] += 1
                continue
            product_id = export_product_id_to_internal_id.get(int(raw_export_product_id))
            if not product_id:
                skipped["rows"] += 1
                continue

            source_url_raw = (row.get("source_url") or "").strip()
            file_path_raw = (row.get("file_path") or "").strip()
            resolved_url = normalize_image_url(source_url_raw)
            if not resolved_url and file_path_raw:
                normalized_path = file_path_raw.lstrip("/\\").replace("\\", "/")
                resolved_url = normalize_image_url(f"/uploads/{normalized_path}")
            if not resolved_url:
                skipped["rows"] += 1
                continue

            try:
                sort_order = int((row.get("sort_order") or "0").strip())
            except ValueError:
                sort_order = 0

            image_candidates_by_product.setdefault(product_id, []).append(
                GalleryImageCandidate(
                    url=resolved_url,
                    sort_order=sort_order,
                    is_primary=_parse_bool(row.get("is_primary"), False),
                    payload=row,
                )
            )

        for product_id, candidates in image_candidates_by_product.items():
            sanitized_candidates = sanitize_gallery_candidates(candidates, limit=30)
            for index, candidate in enumerate(sanitized_candidates):
                row = candidate.payload or {}
                source_url_raw = (row.get("source_url") or "").strip()
                file_path_raw = (row.get("file_path") or "").strip()
                source_url = normalize_image_url(source_url_raw) or None
                file_path = file_path_raw.lstrip("/\\").replace("\\", "/") if file_path_raw else None

                key = (product_id, source_url or "", file_path or "", index)
                if key in existing_images:
                    updated["product_images"] += 1
                    continue

                storage_mode = (row.get("storage_mode") or "external_cdn").strip().lower()
                if storage_mode not in {"external_cdn", "local_copy", "uploaded"}:
                    storage_mode = "external_cdn"

                image = ProductImage(
                    product_id=product_id,
                    storage_mode=storage_mode,
                    source_url=source_url,
                    file_path=file_path,
                    cdn_asset_id=(row.get("cdn_asset_id") or "").strip() or None,
                    is_primary=index == 0,
                    sort_order=index,
                    created_at=_parse_datetime(row.get("created_at")),
                )
                session.add(image)
                existing_images.add(key)
                created["product_images"] += 1

        existing_variants = {
            variant.sku: variant for variant in session.exec(select(ProductVariant)).all()
        }
        for row in product_variant_rows:
            raw_export_product_id = row.get("product_id") or ""
            if not raw_export_product_id.isdigit():
                skipped["rows"] += 1
                continue
            product_id = export_product_id_to_internal_id.get(int(raw_export_product_id))
            if not product_id:
                skipped["rows"] += 1
                continue

            export_variant_id = (row.get("id") or "").strip()
            raw_size = (row.get("size") or "").strip()
            size, encoded_quantity = decode_shoe_size_with_quantity(raw_size) if raw_size else (None, None)
            sku = (
                f"expv-{export_variant_id}"
                if export_variant_id
                else f"expv-{product_id}-{slugify(size or 'na')}-{slugify(row.get('color') or 'na')}"
            )
            stock_status = (row.get("stock_status") or "").strip().lower()
            stock_quantity = 1 if stock_status == "in_stock" else 0
            if encoded_quantity is not None and encoded_quantity > stock_quantity:
                stock_quantity = encoded_quantity
            is_active = stock_status != "archived"
            color = (row.get("color") or "").strip() or None

            existing_variant = existing_variants.get(sku)
            if existing_variant:
                existing_variant.product_id = product_id
                existing_variant.size = size
                existing_variant.color = color
                existing_variant.stock_quantity = stock_quantity
                existing_variant.is_active = is_active
                existing_variant.updated_at = _now()
                session.add(existing_variant)
                updated["product_variants"] += 1
                continue

            variant = ProductVariant(
                product_id=product_id,
                sku=sku[:64],
                size=size,
                color=color,
                stock_quantity=stock_quantity,
                is_active=is_active,
            )
            session.add(variant)
            session.flush()
            existing_variants[variant.sku] = variant
            created["product_variants"] += 1

        # Backfill missing variant colors.
        # Prefer deterministic model-level fallback (brand+name -> single known color).
        # If model-level fallback is not available, use the default "Other" color.
        all_products = session.exec(select(Product)).all()
        product_by_id = {product.id: product for product in all_products if product.id is not None}

        model_colors: dict[str, set[str]] = {}
        for variant in session.exec(
            select(ProductVariant).where(
                ProductVariant.color.is_not(None),
                func.length(func.trim(ProductVariant.color)) > 0,
            )
        ).all():
            product = product_by_id.get(variant.product_id)
            if not product:
                continue
            cleaned_color = _clean_color_value(variant.color)
            if not cleaned_color:
                continue
            key = _model_key(product.brand_name, product.name)
            model_colors.setdefault(key, set()).add(cleaned_color)

        single_color_models = {key: next(iter(colors)) for key, colors in model_colors.items() if len(colors) == 1}
        for variant in session.exec(
            select(ProductVariant).where(
                (ProductVariant.color.is_(None)) | (func.length(func.trim(ProductVariant.color)) == 0)
            )
        ).all():
            product = product_by_id.get(variant.product_id)
            fallback_color = (
                single_color_models.get(_model_key(product.brand_name, product.name)) if product else None
            )
            variant.color = fallback_color or DEFAULT_VARIANT_COLOR
            variant.updated_at = _now()
            session.add(variant)
            created["variant_color_backfilled"] += 1

        tag_slug_to_id: dict[str, int] = {}
        existing_tags = session.exec(select(Tag)).all()
        for tag in existing_tags:
            tag_slug_to_id[tag.slug] = tag.id

        existing_product_tag_pairs = {
            (pt.product_id, pt.tag_id) for pt in session.exec(select(ProductTag)).all()
        }
        for row in product_tag_rows:
            raw_export_product_id = row.get("product_id") or ""
            if not raw_export_product_id.isdigit():
                skipped["rows"] += 1
                continue
            product_id = export_product_id_to_internal_id.get(int(raw_export_product_id))
            if not product_id:
                skipped["rows"] += 1
                continue
            raw_tag = (row.get("tag") or "").strip()
            if not raw_tag:
                skipped["rows"] += 1
                continue

            tag_slug = slugify(raw_tag)
            tag_id = tag_slug_to_id.get(tag_slug)
            if not tag_id:
                new_tag = Tag(name=raw_tag, slug=tag_slug, is_active=True)
                session.add(new_tag)
                session.flush()
                tag_id = new_tag.id
                tag_slug_to_id[tag_slug] = tag_id
                created["tags"] += 1

            if (product_id, tag_id) in existing_product_tag_pairs:
                continue
            session.add(ProductTag(product_id=product_id, tag_id=tag_id))
            existing_product_tag_pairs.add((product_id, tag_id))
            created["product_tags"] += 1

        for row in import_run_rows:
            source_system = (row.get("source_system") or "").strip() or "unknown"
            started_at = _parse_datetime(row.get("started_at"))
            existing = session.exec(
                select(ImportRun).where(
                    ImportRun.source_system == source_system,
                    ImportRun.started_at == started_at,
                )
            ).first()
            run_type = _map_run_type(row.get("run_type"))
            status = _map_run_status(row.get("status"))

            fields = {
                "run_type": run_type,
                "status": status,
                "started_at": started_at,
                "finished_at": _parse_datetime(row.get("finished_at"), fallback=started_at),
                "items_seen_count": _parse_int(row.get("items_seen_count"), 0),
                "items_created_count": _parse_int(row.get("items_created_count"), 0),
                "items_updated_count": _parse_int(row.get("items_updated_count"), 0),
                "items_failed_count": _parse_int(row.get("items_failed_count"), 0),
                "error_summary": (row.get("error_summary") or "").strip() or None,
            }
            if existing:
                for key_name, value in fields.items():
                    setattr(existing, key_name, value)
                session.add(existing)
                updated["import_runs"] += 1
            else:
                session.add(ImportRun(source_system=source_system, **fields))
                created["import_runs"] += 1

        session.flush()

        report["reconciliation"] = {
            "source_catalog_items_db": int(session.exec(select(func.count(SourceCatalogItem.id))).one()),
            "products_db": int(session.exec(select(func.count(Product.id))).one()),
            "product_images_db": int(session.exec(select(func.count(ProductImage.id))).one()),
            "product_variants_db": int(session.exec(select(func.count(ProductVariant.id))).one()),
            "product_variants_with_color_db": int(
                session.exec(
                    select(func.count(ProductVariant.id)).where(
                        ProductVariant.color.is_not(None),
                        func.length(func.trim(ProductVariant.color)) > 0,
                    )
                ).one()
            ),
            "product_tags_db": int(session.exec(select(func.count(ProductTag.id))).one()),
            "import_runs_db": int(session.exec(select(func.count(ImportRun.id))).one()),
            "canonical_categories_active_db": int(
                session.exec(
                    select(func.count(Category.id)).where(
                        Category.slug.in_([category.slug for category in CANONICAL_CATEGORIES]),
                        Category.is_active.is_(True),
                    )
                ).one()
            ),
            "products_with_gender_db": int(
                session.exec(
                    select(func.count(Product.id)).where(
                        Product.gender.is_not(None),
                        func.length(func.trim(Product.gender)) > 0,
                    )
                ).one()
            ),
            "category_fallback_to_sneakers": category_fallback_count,
            "legacy_categories_deactivated": legacy_categories_deactivated,
            "category_fallback_examples": category_fallback_examples,
        }
        report["ok"] = len(report["errors"]) == 0
        report["finished_at"] = _now().isoformat()

        self.report_dir.mkdir(parents=True, exist_ok=True)
        stamp = _now().strftime("%Y%m%d-%H%M%S")
        report_path = self.report_dir / f"bootstrap-exports-{stamp}.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["report_path"] = str(report_path)
        return report
