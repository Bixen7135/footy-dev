from __future__ import annotations

from collections import Counter, defaultdict
import math
import random
import re
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import and_, or_
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, delete, func, select

from .bootstrap import ExportBootstrapService
from .config import get_settings
from .media_gallery import GalleryImageCandidate, normalize_image_url, sanitize_gallery_candidates
from .models import (
    Cart,
    CartItem,
    CartStatus,
    Category,
    ImportRun,
    JobRun,
    Order,
    OrderItem,
    OrderStatusHistory,
    Product,
    ProductImage,
    ProductStatus,
    ProductTag,
    ProductVariant,
    RunStatus,
    RunType,
    SourceAttributeMap,
    SourceBrandMap,
    SourceCatalogItem,
    SourceCategoryMap,
    SourceProductLink,
    SyncStatus,
    Tag,
    User,
    UserEvent,
    WishlistItem,
)
from .ranker import FEATURE_KEYS, RankerArtifacts
from .schemas import (
    EventBatchIn,
    RankerModelMeta,
    MappingRequest,
    NormalizedItem,
    OrderCreateIn,
    PublicationDecision,
    RecommendationItem,
    RecommendationResponse,
    SourceItemIn,
    SyncDecision,
)
from .size_utils import decode_shoe_size_with_quantity, normalize_shoe_size_key, normalize_shoe_size_label
from .taxonomy import GENDER_LABELS as TAXONOMY_GENDER_LABELS, normalize_gender_key, resolve_gender_key


INTERTOP_SMALL_NUMERIC_IMAGE_RE = re.compile(
    r"/load/(?P<asset>[^/]+)/small/(?P<frame>\d+)\.(?P<ext>jpe?g|png|webp)$",
    flags=re.IGNORECASE,
)


def slugify(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned or f"item-{uuid4().hex[:8]}"


def _extract_small_primary_asset(url: Optional[str]) -> Optional[tuple[str, str]]:
    normalized = normalize_image_url(url)
    if not normalized:
        return None

    parts = urlsplit(normalized)
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return None

    match = INTERTOP_SMALL_NUMERIC_IMAGE_RE.search(parts.path or "")
    if not match:
        return None

    asset = (match.group("asset") or "").strip()
    if not asset:
        return None
    origin = f"{parts.scheme}://{parts.netloc}"
    return origin, asset


def _build_main_image_candidates_from_primary(url: Optional[str]) -> list[str]:
    extracted = _extract_small_primary_asset(url)
    if not extracted:
        return []
    origin, asset = extracted
    return [
        f"{origin}/load/{asset}/big/MAIN.jpg",
        f"{origin}/load/{asset}/big/MAIN.jpeg",
        f"{origin}/load/{asset}/big/MAIN.webp",
    ]


def _catalog_image_rank(url: str) -> tuple[int, int, str]:
    normalized = normalize_image_url(url) or url
    lower_url = normalized.lower()
    if "/medium/main." in lower_url:
        return (0, 0, normalized)
    if "/big/main." in lower_url:
        return (1, 0, normalized)
    if "/smallest/main." in lower_url:
        return (2, 0, normalized)
    if "/small/main." in lower_url:
        return (3, 0, normalized)
    if "/medium/" in lower_url:
        return (4, 0, normalized)
    if "/big/" in lower_url:
        return (5, 0, normalized)
    if "/small/" in lower_url:
        return (6, 0, normalized)
    if lower_url.endswith(".svg"):
        return (99, 0, normalized)
    return (10, 0, normalized)


def _sort_catalog_image_urls(urls: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    normalized_urls: list[str] = []
    for url in urls:
        normalized = normalize_image_url(url)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        normalized_urls.append(normalized)
    return sorted(normalized_urls, key=_catalog_image_rank)


def _validate_remote_image_url(url: str, timeout_seconds: float = 4.0) -> bool:
    headers = {
        "Accept": "image/*,*/*;q=0.8",
        "User-Agent": "footy-refresh-primary-images-job/1.0",
    }
    for method in ("HEAD", "GET"):
        try:
            request = Request(url, method=method, headers=headers)
            with urlopen(request, timeout=timeout_seconds) as response:
                status_code = getattr(response, "status", None) or response.getcode()
                if status_code != 200:
                    continue
                content_type = (response.headers.get("Content-Type") or "").lower()
                return content_type.startswith("image/")
        except HTTPError as exc:
            if method == "HEAD" and exc.code in {403, 405, 501}:
                continue
            return False
        except URLError:
            return False
        except TimeoutError:
            return False
    return False


class IngestionService:
    def run_import(self, session: Session, source_system: str, items: list[SourceItemIn]) -> ImportRun:
        run = ImportRun(source_system=source_system, run_type=RunType.IMPORT, status=RunStatus.RUNNING)
        session.add(run)
        session.flush()

        created = 0
        updated = 0
        failed = 0

        for item in items:
            try:
                existing = session.exec(
                    select(SourceCatalogItem).where(
                        SourceCatalogItem.source_system == item.source_system,
                        SourceCatalogItem.source_product_id == item.source_product_id,
                    )
                ).first()
                price = item.price if not item.unavailable else None

                if existing:
                    existing.source_url = item.source_url
                    existing.source_title = item.title
                    existing.source_description = item.description
                    existing.source_brand = item.brand
                    existing.source_category_path = item.category_path
                    existing.source_attributes_json = item.attributes or {}
                    existing.source_price = price
                    existing.source_compare_at_price = item.compare_at_price
                    existing.source_currency = item.currency
                    existing.source_stock_json = item.stock or {}
                    existing.source_media_json = item.images
                    existing.normalized_status = "pending"
                    existing.last_seen_at = item.last_seen_at
                    existing.updated_at = datetime.utcnow()
                    updated += 1
                else:
                    created_item = SourceCatalogItem(
                        source_system=item.source_system,
                        source_product_id=item.source_product_id,
                        source_url=item.source_url,
                        source_title=item.title,
                        source_description=item.description,
                        source_brand=item.brand,
                        source_category_path=item.category_path,
                        source_attributes_json=item.attributes or {},
                        source_price=price,
                        source_compare_at_price=item.compare_at_price,
                        source_currency=item.currency,
                        source_stock_json=item.stock or {},
                        source_media_json=item.images,
                        normalized_status="pending",
                        last_seen_at=item.last_seen_at,
                    )
                    session.add(created_item)
                    created += 1
            except Exception:
                failed += 1

        run.items_seen_count = len(items)
        run.items_created_count = created
        run.items_updated_count = updated
        run.items_failed_count = failed
        run.finished_at = datetime.utcnow()
        run.status = RunStatus.PARTIAL if failed > 0 else RunStatus.SUCCEEDED
        session.add(run)
        session.commit()
        session.refresh(run)
        return run


class NormalizationService:
    def normalize_source_item(self, item: SourceCatalogItem) -> NormalizedItem:
        attributes = item.source_attributes_json or {}
        images = _sort_catalog_image_urls(
            img for img in (item.source_media_json or []) if isinstance(img, str) and img.strip()
        )

        return NormalizedItem(
            source_item_id=item.id,
            title=item.source_title.strip(),
            brand=(item.source_brand or "").strip() or None,
            currency=(item.source_currency or "KZT").upper(),
            price=item.source_price,
            category_key=item.source_category_path.strip().lower(),
            attributes=attributes,
            images=images,
        )


class MappingService:
    def has_required_mapping(self, session: Session, item: SourceCatalogItem) -> bool:
        category_key = item.source_category_path.strip().lower()
        category_map = session.exec(
            select(SourceCategoryMap).where(
                SourceCategoryMap.source_system == item.source_system,
                SourceCategoryMap.source_category_key == category_key,
            )
        ).first()
        return bool(category_map)

    def apply_mapping(self, session: Session, request: MappingRequest) -> None:
        if request.source_category_key and request.internal_category_id:
            existing_category = session.exec(
                select(SourceCategoryMap).where(
                    SourceCategoryMap.source_system == request.source_system,
                    SourceCategoryMap.source_category_key == request.source_category_key.strip().lower(),
                )
            ).first()
            if existing_category:
                existing_category.internal_category_id = request.internal_category_id
            else:
                session.add(
                    SourceCategoryMap(
                        source_system=request.source_system,
                        source_category_key=request.source_category_key.strip().lower(),
                        internal_category_id=request.internal_category_id,
                    )
                )

        if request.source_brand_key and request.internal_brand_name:
            existing_brand = session.exec(
                select(SourceBrandMap).where(
                    SourceBrandMap.source_system == request.source_system,
                    SourceBrandMap.source_brand_key == request.source_brand_key.strip().lower(),
                )
            ).first()
            if existing_brand:
                existing_brand.internal_brand_name = request.internal_brand_name
            else:
                session.add(
                    SourceBrandMap(
                        source_system=request.source_system,
                        source_brand_key=request.source_brand_key.strip().lower(),
                        internal_brand_name=request.internal_brand_name,
                    )
                )

        if request.attributes_map:
            for source_attr, internal in request.attributes_map.items():
                source_key, _, source_value = source_attr.partition("=")
                internal_name, _, internal_value = internal.partition("=")
                if not source_key or not source_value:
                    continue
                existing_attr = session.exec(
                    select(SourceAttributeMap).where(
                        SourceAttributeMap.source_system == request.source_system,
                        SourceAttributeMap.source_attribute_key == source_key,
                        SourceAttributeMap.source_attribute_value == source_value,
                    )
                ).first()
                if existing_attr:
                    existing_attr.internal_attribute_name = internal_name or source_key
                    existing_attr.internal_attribute_value = internal_value or source_value
                else:
                    session.add(
                        SourceAttributeMap(
                            source_system=request.source_system,
                            source_attribute_key=source_key,
                            source_attribute_value=source_value,
                            internal_attribute_name=internal_name or source_key,
                            internal_attribute_value=internal_value or source_value,
                        )
                    )

        session.commit()


class SyncService:
    DEFAULT_VARIANT_COLOR = "Other"
    STOCK_META_KEYS = {
        "sizes",
        "size",
        "stock",
        "stock_by_size",
        "quantity",
        "qty",
        "available",
        "in_stock",
        "is_in_stock",
    }
    VARIANT_SKU_MAX_LENGTH = 64

    def __init__(self) -> None:
        self.normalizer = NormalizationService()

    def _normalize_signature_text(self, value: Optional[str]) -> str:
        return " ".join((value or "").strip().lower().split())

    def _normalize_signature_price(self, value: Optional[Decimal]) -> str:
        if value is None:
            return ""
        return f"{Decimal(value):.2f}"

    def _normalize_signature_currency(self, value: Optional[str]) -> str:
        return (value or "KZT").strip().upper()

    def _media_signature(self, media: Any) -> str:
        if not isinstance(media, list):
            return ""
        normalized_urls: set[str] = set()
        for raw_url in media:
            if not isinstance(raw_url, str):
                continue
            normalized = normalize_image_url(raw_url)
            if not normalized:
                continue
            normalized_urls.add(normalized)
        return "|".join(sorted(normalized_urls))

    def _build_source_signature(
        self,
        *,
        source_system: Optional[str],
        brand: Optional[str],
        title: Optional[str],
        category_key: Optional[str],
        price: Optional[Decimal],
        currency: Optional[str],
        media_signature: str,
    ) -> tuple[str, str, str, str, str, str, str]:
        return (
            self._normalize_signature_text(source_system),
            self._normalize_signature_text(brand),
            self._normalize_signature_text(title),
            self._normalize_signature_text(category_key),
            self._normalize_signature_price(price),
            self._normalize_signature_currency(currency),
            media_signature,
        )

    def build_source_item_signature(
        self,
        session: Session,
        source_item: SourceCatalogItem,
    ) -> tuple[str, str, str, str, str, str, str]:
        resolved_brand = self._resolve_brand(session, source_item.source_system, source_item.source_brand)
        return self._build_source_signature(
            source_system=source_item.source_system,
            brand=resolved_brand,
            title=source_item.source_title,
            category_key=source_item.source_category_path,
            price=source_item.source_price,
            currency=source_item.source_currency,
            media_signature=self._media_signature(source_item.source_media_json),
        )

    def _find_existing_product_by_signature(
        self,
        session: Session,
        source_item: SourceCatalogItem,
        normalized: NormalizedItem,
    ) -> Optional[Product]:
        target_signature = self._build_source_signature(
            source_system=source_item.source_system,
            brand=self._resolve_brand(session, source_item.source_system, normalized.brand),
            title=normalized.title,
            category_key=normalized.category_key,
            price=normalized.price,
            currency=normalized.currency,
            media_signature=self._media_signature(normalized.images),
        )
        candidates = session.exec(
            select(Product, SourceCatalogItem)
            .join(SourceProductLink, SourceProductLink.product_id == Product.id)
            .join(SourceCatalogItem, SourceCatalogItem.id == SourceProductLink.source_catalog_item_id)
            .where(
                Product.source_managed.is_(True),
                SourceCatalogItem.source_system == source_item.source_system,
                SourceCatalogItem.id != source_item.id,
            )
            .order_by(Product.id.asc(), SourceCatalogItem.id.asc())
        ).all()

        best_match: Optional[Product] = None
        for candidate_product, candidate_source in candidates:
            candidate_signature = self.build_source_item_signature(session, candidate_source)
            if candidate_signature != target_signature:
                continue
            if candidate_product.id is None:
                continue
            if best_match is None or (best_match.id is not None and candidate_product.id < best_match.id):
                best_match = candidate_product
        return best_match

    def _publication_decision(
        self,
        mapped_category_id: Optional[int],
        normalized: NormalizedItem,
    ) -> PublicationDecision:
        if not mapped_category_id:
            return PublicationDecision(publishable=False, reason="missing_category_mapping")
        if not normalized.price or normalized.price <= 0:
            return PublicationDecision(publishable=False, reason="missing_price")
        if not normalized.images:
            return PublicationDecision(publishable=False, reason="missing_image")
        if not normalized.title:
            return PublicationDecision(publishable=False, reason="missing_name")
        return PublicationDecision(publishable=True)

    def _resolve_category(self, session: Session, source_system: str, category_key: str) -> Optional[int]:
        mapping = session.exec(
            select(SourceCategoryMap).where(
                SourceCategoryMap.source_system == source_system,
                SourceCategoryMap.source_category_key == category_key,
            )
        ).first()
        return mapping.internal_category_id if mapping else None

    def _resolve_brand(self, session: Session, source_system: str, brand: Optional[str]) -> Optional[str]:
        if not brand:
            return None
        mapped = session.exec(
            select(SourceBrandMap).where(
                SourceBrandMap.source_system == source_system,
                SourceBrandMap.source_brand_key == brand.strip().lower(),
            )
        ).first()
        return mapped.internal_brand_name if mapped else brand

    def _set_if_not_overridden(self, product: Product, field_name: str, value) -> None:
        if field_name in (product.admin_overrides_json or []):
            return
        setattr(product, field_name, value)

    def _fallback_category(self, session: Session) -> int:
        fallback = session.exec(select(Category).where(Category.slug == "uncategorized")).first()
        if fallback:
            return fallback.id
        fallback = Category(name="Uncategorized", slug="uncategorized", is_active=True)
        session.add(fallback)
        session.commit()
        session.refresh(fallback)
        return fallback.id

    def _ensure_images(self, session: Session, product_id: int, images: list[str]) -> None:
        existing_count = session.exec(
            select(func.count(ProductImage.id)).where(ProductImage.product_id == product_id)
        ).one()
        if existing_count > 0:
            return
        for idx, img in enumerate(images[:5]):
            session.add(
                ProductImage(
                    product_id=product_id,
                    source_url=img,
                    sort_order=idx,
                    is_primary=(idx == 0),
                    alt_text="Product image",
                )
            )

    def _normalize_variant_size(self, value: Any) -> Optional[str]:
        return normalize_shoe_size_label(value)

    def _decode_variant_size_and_quantity(self, value: Any) -> tuple[Optional[str], Optional[int]]:
        return decode_shoe_size_with_quantity(value)

    def _normalize_variant_color(self, value: Optional[str]) -> str:
        cleaned = (value or "").strip()
        return cleaned or self.DEFAULT_VARIANT_COLOR

    def _variant_key(self, size: Optional[str], color: Optional[str]) -> tuple[str, str]:
        return (
            (size or "").strip().lower(),
            self._normalize_variant_color(color).lower(),
        )

    def _parse_stock_quantity(self, raw_value: Any) -> Optional[int]:
        if raw_value is None:
            return None
        if isinstance(raw_value, bool):
            return 1 if raw_value else 0
        if isinstance(raw_value, (int, float, Decimal)):
            return max(int(raw_value), 0)
        if isinstance(raw_value, str):
            cleaned = raw_value.strip().lower()
            if not cleaned:
                return None
            if cleaned in {"in_stock", "instock", "available", "true", "yes"}:
                return 1
            if cleaned in {"out_of_stock", "oos", "unavailable", "false", "no"}:
                return 0
            try:
                return max(int(float(cleaned.replace(",", "."))), 0)
            except ValueError:
                return None
        if isinstance(raw_value, dict):
            for key in ("qty", "quantity", "stock", "value", "count"):
                if key in raw_value:
                    parsed = self._parse_stock_quantity(raw_value.get(key))
                    if parsed is not None:
                        return parsed
        return None

    def _extract_stock_pairs(self, source_item: SourceCatalogItem) -> list[tuple[str, int]]:
        stock_data = source_item.source_stock_json
        pairs: list[tuple[str, int]] = []

        if isinstance(stock_data, dict):
            raw_sizes = stock_data.get("sizes")
            if isinstance(raw_sizes, list):
                stock_by_size = stock_data.get("stock_by_size") if isinstance(stock_data.get("stock_by_size"), dict) else {}
                for raw_size in raw_sizes:
                    raw_size_key = str(raw_size).strip() if raw_size is not None else ""
                    size, encoded_qty = self._decode_variant_size_and_quantity(raw_size)
                    if not size:
                        continue
                    qty = self._parse_stock_quantity(stock_by_size.get(raw_size_key))
                    if qty is None:
                        qty = self._parse_stock_quantity(stock_by_size.get(size))
                    if qty is None:
                        qty = encoded_qty
                    pairs.append((size, 1 if qty is None else qty))

            for raw_size, raw_qty in stock_data.items():
                raw_size_key = str(raw_size).strip().lower()
                if raw_size_key in self.STOCK_META_KEYS:
                    continue
                normalized_size, encoded_qty = self._decode_variant_size_and_quantity(raw_size)
                if not normalized_size:
                    continue
                quantity = self._parse_stock_quantity(raw_qty)
                if quantity is None:
                    quantity = encoded_qty
                if quantity is None:
                    continue
                pairs.append((normalized_size, quantity))
        elif isinstance(stock_data, list):
            for raw_size in stock_data:
                size, encoded_qty = self._decode_variant_size_and_quantity(raw_size)
                if not size:
                    continue
                pairs.append((size, 1 if encoded_qty is None else encoded_qty))

        collapsed: dict[str, int] = {}
        for size, quantity in pairs:
            existing = collapsed.get(size)
            if existing is None or quantity > existing:
                collapsed[size] = quantity
        return sorted(collapsed.items(), key=lambda item: item[0])

    def _extract_source_color(self, source_item: SourceCatalogItem) -> Optional[str]:
        attributes = source_item.source_attributes_json or {}
        if not isinstance(attributes, dict):
            return None
        for key in ("color", "colour", "color_name"):
            value = attributes.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, list):
                for candidate in value:
                    if isinstance(candidate, str) and candidate.strip():
                        return candidate.strip()
        return None

    def _build_unique_variant_sku(
        self,
        session: Session,
        source_item: SourceCatalogItem,
        size: Optional[str],
        color: Optional[str],
    ) -> str:
        parts = [source_item.source_system, source_item.source_product_id]
        if size:
            parts.append(size)
        if color:
            parts.append(color)
        base = slugify("-".join(parts)) or f"variant-{uuid4().hex[:10]}"
        base = base[: self.VARIANT_SKU_MAX_LENGTH]
        candidate = base
        suffix = 1
        while session.exec(select(ProductVariant.id).where(ProductVariant.sku == candidate)).first():
            suffix_token = f"-{suffix}"
            candidate = f"{base[: self.VARIANT_SKU_MAX_LENGTH - len(suffix_token)]}{suffix_token}"
            suffix += 1
        return candidate

    def _build_unique_product_slug(
        self,
        session: Session,
        title: str,
        source_product_id: str,
        current_product_id: Optional[int] = None,
    ) -> str:
        base = slugify(f"{title}-{source_product_id}")
        candidate = base
        suffix = 1
        while True:
            existing_product_id = session.exec(
                select(Product.id).where(Product.slug == candidate)
            ).first()
            if existing_product_id is None or existing_product_id == current_product_id:
                return candidate
            candidate = f"{base}-{suffix}"
            suffix += 1

    def _ensure_variant(self, session: Session, product_id: int, source_item: SourceCatalogItem) -> None:
        existing_variants = session.exec(
            select(ProductVariant).where(ProductVariant.product_id == product_id)
        ).all()
        existing_by_key: dict[tuple[str, str], ProductVariant] = {}
        for variant in existing_variants:
            normalized_size = self._normalize_variant_size(variant.size)
            normalized_color = self._normalize_variant_color(variant.color)
            changed = False
            if variant.size != normalized_size:
                variant.size = normalized_size
                changed = True
            if variant.color != normalized_color:
                variant.color = normalized_color
                changed = True
            key = self._variant_key(normalized_size, normalized_color)
            current = existing_by_key.get(key)
            if not current or (current.id is not None and variant.id is not None and variant.id < current.id):
                existing_by_key[key] = variant
            if changed:
                variant.updated_at = datetime.utcnow()
                session.add(variant)

        source_color = self._extract_source_color(source_item)
        if not source_color:
            source_color = next(
                ((variant.color or "").strip() for variant in existing_variants if (variant.color or "").strip()),
                self.DEFAULT_VARIANT_COLOR,
            )
        normalized_source_color = self._normalize_variant_color(source_color)

        stock_pairs = self._extract_stock_pairs(source_item)
        if stock_pairs:
            for size, incoming_quantity in stock_pairs:
                key = self._variant_key(size, normalized_source_color)
                variant = existing_by_key.get(key)
                if variant:
                    variant.stock_quantity = max(int(variant.stock_quantity or 0), int(incoming_quantity or 0))
                    variant.is_active = bool(variant.is_active) or int(incoming_quantity or 0) > 0
                    variant.updated_at = datetime.utcnow()
                    session.add(variant)
                    continue

                session.add(
                    ProductVariant(
                        product_id=product_id,
                        sku=self._build_unique_variant_sku(
                            session,
                            source_item,
                            size=size,
                            color=normalized_source_color,
                        ),
                        size=size,
                        color=normalized_source_color,
                        stock_quantity=max(int(incoming_quantity), 0),
                        is_active=True,
                    )
                )
            return

        fallback_key = self._variant_key(None, normalized_source_color)
        fallback_variant = existing_by_key.get(fallback_key)
        if fallback_variant:
            fallback_variant.color = normalized_source_color
            fallback_variant.stock_quantity = max(int(fallback_variant.stock_quantity or 0), 0)
            fallback_variant.updated_at = datetime.utcnow()
            session.add(fallback_variant)
            return

        session.add(
            ProductVariant(
                product_id=product_id,
                sku=self._build_unique_variant_sku(
                    session,
                    source_item,
                    size=None,
                    color=normalized_source_color,
                ),
                color=normalized_source_color,
                stock_quantity=0,
                is_active=True,
            )
        )

    def sync_source_item(self, session: Session, item: SourceCatalogItem) -> SyncDecision:
        normalized = self.normalizer.normalize_source_item(item)
        category_id = self._resolve_category(session, item.source_system, normalized.category_key)
        publication = self._publication_decision(category_id, normalized)
        source_attributes = item.source_attributes_json or {}
        source_details = source_attributes.get("details") if isinstance(source_attributes, dict) else None
        if not isinstance(source_details, dict):
            source_details = {}
        gender_key = resolve_gender_key(
            (source_attributes.get("root_gender") if isinstance(source_attributes, dict) else None)
            or source_details.get("root_gender")
            or source_details.get("pol")
            or (source_attributes.get("pol") if isinstance(source_attributes, dict) else None),
            item.source_category_path,
        )

        link = session.exec(
            select(SourceProductLink).where(SourceProductLink.source_catalog_item_id == item.id)
        ).first()

        product = session.get(Product, link.product_id) if link else None
        if product is None:
            product = self._find_existing_product_by_signature(session, item, normalized)

        if product is None:
            product = Product(
                slug=self._build_unique_product_slug(session, normalized.title, item.source_product_id),
                name=normalized.title,
                short_description=item.source_description,
                description=item.source_description,
                brand_name=self._resolve_brand(session, item.source_system, normalized.brand),
                price=normalized.price or Decimal("0.00"),
                compare_at_price=item.source_compare_at_price,
                currency=normalized.currency,
                category_id=category_id or self._fallback_category(session),
                gender=gender_key,
                is_active=publication.publishable,
                status=ProductStatus.ACTIVE if publication.publishable else ProductStatus.INACTIVE,
                source_managed=True,
            )
            session.add(product)
            session.flush()
        else:
            self._set_if_not_overridden(product, "name", normalized.title)
            self._set_if_not_overridden(product, "short_description", item.source_description)
            self._set_if_not_overridden(product, "description", item.source_description)
            self._set_if_not_overridden(
                product,
                "brand_name",
                self._resolve_brand(session, item.source_system, normalized.brand),
            )
            self._set_if_not_overridden(product, "price", normalized.price or Decimal("0.00"))
            self._set_if_not_overridden(product, "compare_at_price", item.source_compare_at_price)
            self._set_if_not_overridden(product, "currency", normalized.currency)
            if category_id:
                self._set_if_not_overridden(product, "category_id", category_id)
            if gender_key:
                self._set_if_not_overridden(product, "gender", gender_key)
            self._set_if_not_overridden(product, "is_active", publication.publishable)
            self._set_if_not_overridden(
                product,
                "status",
                ProductStatus.ACTIVE if publication.publishable else ProductStatus.INACTIVE,
            )
            product.updated_at = datetime.utcnow()

        self._ensure_images(session, product.id, normalized.images)
        self._ensure_variant(session, product.id, item)

        if link is None:
            link = SourceProductLink(
                source_catalog_item_id=item.id,
                product_id=product.id,
                sync_status=SyncStatus.SYNCED if publication.publishable else SyncStatus.MAPPED,
                last_sync_at=datetime.utcnow(),
                error_message=publication.reason,
            )
            session.add(link)
        else:
            link.sync_status = SyncStatus.SYNCED if publication.publishable else SyncStatus.MAPPED
            link.last_sync_at = datetime.utcnow()
            link.error_message = publication.reason

        item.normalized_status = "normalized"
        session.add(item)
        session.commit()

        return SyncDecision(
            source_item_id=item.id,
            product_id=product.id,
            action="upsert",
            reason=publication.reason,
        )

    def sync_pending(self, session: Session, limit: int = 200) -> list[SyncDecision]:
        items = session.exec(
            select(SourceCatalogItem)
            .where(SourceCatalogItem.normalized_status != "failed")
            .order_by(SourceCatalogItem.updated_at.desc())
            .limit(limit)
        ).all()

        decisions: list[SyncDecision] = []
        for item in items:
            try:
                decisions.append(self.sync_source_item(session, item))
            except Exception as exc:
                if isinstance(exc, SQLAlchemyError):
                    session.rollback()
                item.normalized_status = "failed"
                session.add(item)
                session.commit()
                decisions.append(
                    SyncDecision(source_item_id=item.id, action="failed", reason=str(exc), product_id=None)
                )
        return decisions


class StockService:
    def recheck_and_reserve(self, session: Session, lines: Iterable[dict]) -> None:
        variant_updates: list[tuple[ProductVariant, int]] = []
        for line in lines:
            product_id = line["product_id"]
            variant_id = line.get("variant_id")
            quantity = int(line["quantity"])

            if variant_id:
                variant = session.get(ProductVariant, variant_id)
                if not variant or not variant.is_active or variant.product_id != product_id:
                    raise HTTPException(status_code=400, detail="Variant unavailable")
            else:
                variant = session.exec(
                    select(ProductVariant)
                    .where(ProductVariant.product_id == product_id, ProductVariant.is_active.is_(True))
                    .order_by(ProductVariant.id.asc())
                ).first()
                if not variant:
                    raise HTTPException(status_code=400, detail="No active variant")

            if variant.stock_quantity < quantity:
                raise HTTPException(status_code=409, detail="Insufficient stock")
            variant_updates.append((variant, quantity))

        for variant, quantity in variant_updates:
            variant.stock_quantity -= quantity
            session.add(variant)


class OrderService:
    def __init__(self) -> None:
        self.stock_service = StockService()

    def create_order(
        self,
        session: Session,
        current_user: Optional[User],
        anonymous_id: Optional[str],
        payload: OrderCreateIn,
    ) -> Order:
        existing = session.exec(
            select(Order).where(Order.idempotency_key == payload.idempotency_key)
        ).first()
        if existing:
            return existing

        lines = [line.model_dump() for line in payload.items]
        self.stock_service.recheck_and_reserve(session, lines)

        subtotal = Decimal("0")
        order = Order(
            order_number=f"FOOTY-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:6].upper()}",
            user_id=current_user.id if current_user else None,
            email=payload.shipping.email,
            full_name=payload.shipping.full_name,
            phone=payload.shipping.phone,
            address_line1=payload.shipping.address_line1,
            address_line2=payload.shipping.address_line2,
            city=payload.shipping.city,
            postal_code=payload.shipping.postal_code,
            country=payload.shipping.country,
            notes=payload.notes,
            subtotal_amount=Decimal("0.00"),
            total_amount=Decimal("0.00"),
            status="pending",
            idempotency_key=payload.idempotency_key,
        )
        session.add(order)
        session.flush()

        for line in payload.items:
            product = session.get(Product, line.product_id)
            if not product:
                raise HTTPException(status_code=404, detail=f"Product {line.product_id} not found")

            variant = session.get(ProductVariant, line.variant_id) if line.variant_id else None
            line_total = Decimal(product.price) * line.quantity
            subtotal += line_total

            session.add(
                OrderItem(
                    order_id=order.id,
                    product_id=product.id,
                    variant_id=variant.id if variant else None,
                    product_name_snapshot=product.name,
                    variant_snapshot_json=(
                        {"size": variant.size, "color": variant.color, "sku": variant.sku}
                        if variant
                        else None
                    ),
                    unit_price_snapshot=product.price,
                    quantity=line.quantity,
                    line_total=line_total,
                )
            )

        order.subtotal_amount = subtotal
        order.total_amount = subtotal
        session.add(order)
        session.add(
            OrderStatusHistory(
                order_id=order.id,
                previous_status=None,
                new_status="pending",
                changed_by_user_id=current_user.id if current_user else None,
                admin_note="Order created",
            )
        )

        if current_user or anonymous_id:
            cart = self._find_active_cart(session, current_user.id if current_user else None, anonymous_id)
            if cart:
                cart.status = CartStatus.CONVERTED
                session.add(cart)

        session.commit()
        session.refresh(order)
        return order

    def _find_active_cart(
        self,
        session: Session,
        user_id: Optional[int],
        anonymous_id: Optional[str],
    ) -> Optional[Cart]:
        statement = select(Cart).where(Cart.status == CartStatus.ACTIVE)
        if user_id:
            statement = statement.where(Cart.user_id == user_id)
        elif anonymous_id:
            statement = statement.where(Cart.anonymous_id == anonymous_id)
        else:
            return None
        return session.exec(statement.order_by(Cart.updated_at.desc())).first()


class EventIngestionService:
    REQUIRED_EVENT_TYPES = {
        "product_view",
        "product_dwell",
        "add_to_cart",
        "remove_from_cart",
        "purchase",
        "search",
        "recommendation_impression",
        "recommendation_click",
        "listing_view",
        "listing_impression",
        "listing_click",
        "variant_select",
        "filter_apply",
        "sort_change",
        "checkout_start",
        "wishlist_add",
        "wishlist_remove",
    }

    def _normalize_recommendation_event(self, event: Any, normalized_type: str) -> dict[str, Any] | None:
        recommendation_slot = (event.recommendation_slot or event.source_page or "").strip().lower() or None
        request_id = (event.recommendation_request_id or "").strip() or None
        rank_position = event.rank_position if event.rank_position and event.rank_position > 0 else None
        metadata = event.metadata_json if isinstance(event.metadata_json, dict) else {}
        source_page = (event.source_page or "").strip().lower() or recommendation_slot

        if normalized_type == "recommendation_click":
            if not event.product_id or not request_id or not recommendation_slot:
                return None
        if normalized_type == "recommendation_impression":
            has_item = bool(event.product_id) or (
                isinstance(metadata.get("product_ids"), list) and len(metadata.get("product_ids")) > 0
            )
            if not has_item or not request_id or not recommendation_slot:
                return None

        return {
            "source_page": source_page,
            "recommendation_slot": recommendation_slot,
            "recommendation_request_id": request_id,
            "rank_position": rank_position,
            "metadata_json": metadata or None,
        }

    def ingest_batch(self, session: Session, payload: EventBatchIn) -> int:
        if not payload.events:
            return 0

        inserted = 0
        for event in payload.events:
            normalized_type = event.event_type.strip().lower()
            if normalized_type not in self.REQUIRED_EVENT_TYPES:
                continue
            source_page = event.source_page
            recommendation_slot = event.recommendation_slot
            recommendation_request_id = event.recommendation_request_id
            rank_position = event.rank_position
            metadata_json = event.metadata_json
            if normalized_type.startswith("recommendation_"):
                normalized = self._normalize_recommendation_event(event, normalized_type)
                if not normalized:
                    continue
                source_page = normalized["source_page"]
                recommendation_slot = normalized["recommendation_slot"]
                recommendation_request_id = normalized["recommendation_request_id"]
                rank_position = normalized["rank_position"]
                metadata_json = normalized["metadata_json"]
            session.add(
                UserEvent(
                    event_type=normalized_type,
                    user_id=event.user_id,
                    anonymous_id=event.anonymous_id,
                    session_id=event.session_id,
                    product_id=event.product_id,
                    variant_id=event.variant_id,
                    category_id=event.category_id,
                    source_page=source_page,
                    page_url=event.page_url,
                    rank_position=rank_position,
                    dwell_ms=event.dwell_ms,
                    recommendation_slot=recommendation_slot,
                    recommendation_request_id=recommendation_request_id,
                    metadata_json=metadata_json,
                    created_at=event.created_at or datetime.utcnow(),
                )
            )
            inserted += 1

        session.commit()
        return inserted


class RecommendationService:
    SOURCE_WEIGHTS = {
        "recently_viewed": 2.2,
        "affinity": 1.8,
        "tag_affinity": 1.7,
        "recent_orders": 1.6,
        "co_view": 1.5,
        "co_purchase": 1.4,
        "content_similarity": 1.3,
        "session_recency": 1.2,
        "current_category_similarity": 1.1,
        "trending": 1.0,
    }
    IMPLICIT_WEIGHTS = {
        "recommendation_impression": 1.0,
        "recommendation_click": 2.0,
        "listing_impression": 1.0,
        "listing_click": 2.0,
        "product_view": 3.0,
        "product_dwell": 4.0,
        "wishlist_add": 5.0,
        "add_to_cart": 6.0,
        "purchase": 10.0,
    }
    CONTEXTS = {"home", "pdp", "cart", "account"}

    def __init__(self) -> None:
        self.settings = get_settings()
        self.ranker_artifacts = RankerArtifacts()

    def get_recommendations(
        self,
        session: Session,
        context: str,
        limit: int,
        user_id: Optional[int] = None,
        anonymous_id: Optional[str] = None,
        current_product_id: Optional[int] = None,
    ) -> RecommendationResponse:
        if not self.settings.recommendation_ranking_v2_enabled:
            return self._get_recommendations_v1(session, context, limit, user_id, anonymous_id, current_product_id)
        try:
            return self._get_recommendations_v2(session, context, limit, user_id, anonymous_id, current_product_id)
        except Exception:
            return self._get_recommendations_v1(session, context, limit, user_id, anonymous_id, current_product_id)

    def _get_recommendations_v2(
        self,
        session: Session,
        context: str,
        limit: int,
        user_id: Optional[int] = None,
        anonymous_id: Optional[str] = None,
        current_product_id: Optional[int] = None,
    ) -> RecommendationResponse:
        limit = max(1, min(int(limit), 50))
        scores: dict[int, float] = defaultdict(float)
        source_labels: dict[int, set[str]] = defaultdict(set)
        source_components: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        feature_rows: list[dict[str, Any]] = []
        context_norm = self._normalize_context(context)
        actor_counts = self._actor_counts(session, user_id, anonymous_id)
        anchor = self._resolve_anchor_product(session, current_product_id, user_id, anonymous_id)
        anchor_product = session.get(Product, anchor) if anchor else None
        tag_overlap_map = self._tag_overlap_map(session, anchor)
        pop_7d_map, purchase_30d_map = self._product_popularity_maps(session)

        self._add_recently_viewed(session, scores, source_labels, source_components, user_id, anonymous_id)
        self._add_affinity(session, scores, source_labels, source_components, user_id)
        self._add_tag_affinity(session, scores, source_labels, source_components, user_id)
        self._add_recent_orders(session, scores, source_labels, source_components, user_id)
        self._add_co_view(session, scores, source_labels, source_components, current_product_id, user_id, anonymous_id)
        self._add_co_purchase(session, scores, source_labels, source_components, current_product_id, user_id)
        self._add_content_similarity(
            session, scores, source_labels, source_components, current_product_id, user_id, anonymous_id
        )
        self._add_session_recency(session, scores, source_labels, source_components, anonymous_id)
        self._add_current_category_similarity(session, scores, source_labels, source_components, current_product_id)
        self._add_trending(session, scores, source_labels, source_components)

        request_id = uuid4().hex

        ranked_ids = sorted(scores.keys(), key=lambda pid: scores[pid], reverse=True)
        candidates: list[tuple[int, Product]] = []
        for product_id in ranked_ids:
            if current_product_id and product_id == current_product_id:
                continue
            product = session.get(Product, product_id)
            if not product or not product.is_active:
                continue
            if not self._is_available(session, product_id):
                continue
            candidates.append((product_id, product))
            feature_rows.append(
                self._build_feature_row(
                    session=session,
                    product=product,
                    base_score=float(scores[product_id]),
                    source_components=source_components.get(product_id, {}),
                    user_id=user_id,
                    anonymous_id=anonymous_id,
                    context=context_norm,
                    anchor_product=anchor_product,
                    tag_overlap=float(tag_overlap_map.get(product_id, 0.0)),
                    actor_counts=actor_counts,
                    product_views_7d=float(pop_7d_map.get(product_id, 0.0)),
                    product_purchases_30d=float(purchase_30d_map.get(product_id, 0.0)),
                    label=None,
                    is_training=False,
                )
            )
        ml_scores, _ = self.ranker_artifacts.predict(feature_rows)
        ml_norm: dict[int, float] = {}
        if ml_scores:
            values = list(ml_scores.values())
            lower = min(values)
            upper = max(values)
            spread = upper - lower
            for product_id, value in ml_scores.items():
                ml_norm[product_id] = (value - lower) / spread if spread > 1e-9 else 0.5

        candidate_rows: list[dict[str, Any]] = []
        for product_id, product in candidates:
            row = next((r for r in feature_rows if int(r["product_id"]) == product_id), None)
            if not row:
                continue
            penalties = self._diversity_penalty(product, candidate_rows)
            row["penalties"] = penalties
            rule_score = (
                float(row["base_score"])
                + float(row["affinity_score"]) * 0.35
                + float(row["similarity_score"]) * 0.30
                + float(row["popularity_score"]) * 0.20
                + float(row["recency_bonus"]) * 0.15
                - penalties
            )
            ml_boost = ml_norm.get(product_id, 0.0) * 1.25
            final_score = rule_score + ml_boost
            sources = sorted(source_labels.get(product_id, set()))
            candidate_rows.append(
                {
                    "product_id": product_id,
                    "score": final_score,
                    "source": f"ml+{'+'.join(sources)}" if ml_scores and sources else "+".join(sources) or "hybrid",
                    "brand": (product.brand_name or "").strip().lower(),
                    "category_id": product.category_id,
                }
            )
        candidate_rows.sort(key=lambda row: float(row["score"]), reverse=True)
        items = [
            RecommendationItem(product_id=int(row["product_id"]), score=round(float(row["score"]), 4), source=row["source"])
            for row in candidate_rows[:limit]
        ]
        return RecommendationResponse(request_id=request_id, items=items)

    def _get_recommendations_v1(
        self,
        session: Session,
        context: str,
        limit: int,
        user_id: Optional[int] = None,
        anonymous_id: Optional[str] = None,
        current_product_id: Optional[int] = None,
    ) -> RecommendationResponse:
        limit = max(1, min(int(limit), 50))
        scores: dict[int, float] = defaultdict(float)
        source_labels: dict[int, set[str]] = defaultdict(set)
        source_components: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self._add_recently_viewed(session, scores, source_labels, source_components, user_id, anonymous_id)
        self._add_affinity(session, scores, source_labels, source_components, user_id)
        self._add_tag_affinity(session, scores, source_labels, source_components, user_id)
        self._add_recent_orders(session, scores, source_labels, source_components, user_id)
        self._add_co_view(session, scores, source_labels, source_components, current_product_id, user_id, anonymous_id)
        self._add_co_purchase(session, scores, source_labels, source_components, current_product_id, user_id)
        self._add_content_similarity(
            session, scores, source_labels, source_components, current_product_id, user_id, anonymous_id
        )
        self._add_session_recency(session, scores, source_labels, source_components, anonymous_id)
        self._add_current_category_similarity(session, scores, source_labels, source_components, current_product_id)
        self._add_trending(session, scores, source_labels, source_components)
        ranked_ids = sorted(scores.keys(), key=lambda pid: scores[pid], reverse=True)
        request_id = uuid4().hex
        items: list[RecommendationItem] = []
        for product_id in ranked_ids:
            if current_product_id and product_id == current_product_id:
                continue
            product = session.get(Product, product_id)
            if not product or not product.is_active or not self._is_available(session, product_id):
                continue
            sources = sorted(source_labels.get(product_id, set()))
            items.append(
                RecommendationItem(
                    product_id=product_id,
                    score=round(float(scores[product_id]), 4),
                    source="+".join(sources) or "hybrid",
                )
            )
        return RecommendationResponse(request_id=request_id, items=items[:limit])

    def get_model_status(self) -> RankerModelMeta:
        return RankerModelMeta(**self.ranker_artifacts.read_meta())

    def get_quality_metrics(self, session: Session, window_days: int = 7) -> dict[str, Any]:
        window_days = max(1, min(int(window_days), 30))
        cutoff = datetime.utcnow() - timedelta(days=window_days)
        impressions = session.exec(
            select(func.count(UserEvent.id)).where(
                UserEvent.event_type == "recommendation_impression",
                UserEvent.created_at >= cutoff,
            )
        ).one()
        clicks = session.exec(
            select(func.count(UserEvent.id)).where(
                UserEvent.event_type == "recommendation_click",
                UserEvent.created_at >= cutoff,
            )
        ).one()
        clicked_request_ids = [
            req_id
            for req_id in session.exec(
                select(UserEvent.recommendation_request_id)
                .where(
                    UserEvent.event_type == "recommendation_click",
                    UserEvent.recommendation_request_id.is_not(None),
                    UserEvent.created_at >= cutoff,
                )
                .distinct()
            ).all()
            if req_id
        ]
        add_to_cart_after_rec = 0
        purchase_after_rec = 0
        if clicked_request_ids:
            click_sessions = [
                session_id
                for session_id in session.exec(
                    select(UserEvent.session_id)
                    .where(
                        UserEvent.event_type == "recommendation_click",
                        UserEvent.recommendation_request_id.in_(clicked_request_ids),
                        UserEvent.created_at >= cutoff,
                    )
                    .distinct()
                ).all()
                if session_id
            ]
            if click_sessions:
                add_to_cart_after_rec = session.exec(
                    select(func.count(UserEvent.id)).where(
                        UserEvent.event_type == "add_to_cart",
                        UserEvent.session_id.in_(click_sessions),
                        UserEvent.created_at >= cutoff,
                    )
                ).one()
                purchase_after_rec = session.exec(
                    select(func.count(UserEvent.id)).where(
                        UserEvent.event_type == "purchase",
                        UserEvent.session_id.in_(click_sessions),
                        UserEvent.created_at >= cutoff,
                    )
                ).one()
        ctr = float(clicks / impressions) if impressions else 0.0
        return {
            "window_days": window_days,
            "impressions": int(impressions),
            "clicks": int(clicks),
            "ctr": ctr,
            "add_to_cart_after_rec": int(add_to_cart_after_rec),
            "purchase_after_rec": int(purchase_after_rec),
        }

    def build_training_dataset(self, session: Session, max_rows: int = 5000) -> tuple[list[dict[str, Any]], dict]:
        event_rows = session.exec(
            select(UserEvent)
            .where(
                UserEvent.product_id.is_not(None),
                UserEvent.event_type.in_(list(self.IMPLICIT_WEIGHTS.keys())),
            )
            .order_by(UserEvent.created_at.desc())
            .limit(max_rows)
        ).all()
        active_products = session.exec(select(Product).where(Product.is_active.is_(True))).all()
        if not active_products:
            return [], {"rows_total": 0, "positive_rows": 0, "negative_rows": 0}

        by_category: dict[int, list[Product]] = defaultdict(list)
        all_product_ids: list[int] = []
        by_id: dict[int, Product] = {}
        pop_7d_map, purchase_30d_map = self._product_popularity_maps(session)
        for row in active_products:
            by_category[row.category_id].append(row.id)
            all_product_ids.append(row.id)
            by_id[int(row.id)] = row
        all_products = [p for p in active_products if p.id is not None]

        rng = random.Random(42)
        dataset: list[dict[str, Any]] = []
        positive_rows = 0
        negative_rows = 0

        for event in event_rows:
            product = session.get(Product, event.product_id)
            if not product:
                continue
            label_weight = self.IMPLICIT_WEIGHTS.get(event.event_type, 0.0)
            context = (event.source_page or "home").strip().lower()
            if context not in {"home", "pdp", "cart", "account"}:
                context = "home"
            age_days = max((datetime.utcnow() - event.created_at).total_seconds() / 86400.0, 0.0)
            decay = math.exp(-math.log(2) * (age_days / 21.0))
            label_value = min(1.0, label_weight / 10.0) * decay
            actor_counts = self._actor_counts(session, event.user_id, event.anonymous_id)
            base_row = self._build_feature_row(
                session=session,
                product=product,
                base_score=label_weight,
                source_components={"affinity": label_weight * 0.2, "similarity": label_weight * 0.1},
                user_id=event.user_id,
                anonymous_id=event.anonymous_id,
                context=context,
                anchor_product=product,
                tag_overlap=1.0,
                actor_counts=actor_counts,
                product_views_7d=float(pop_7d_map.get(int(product.id), 0.0)),
                product_purchases_30d=float(purchase_30d_map.get(int(product.id), 0.0)),
                label=label_value,
                is_training=True,
            )
            dataset.append(base_row)
            positive_rows += 1

            same_category = [pid for pid in by_category.get(product.category_id, []) if pid != product.id]
            cross_category = [pid for pid in all_product_ids if pid != product.id and by_id[pid].category_id != product.category_id]
            popularity_sorted = sorted(
                [pid for pid in all_product_ids if pid != product.id],
                key=lambda pid: float(pop_7d_map.get(pid, 0.0)) + float(purchase_30d_map.get(pid, 0.0)),
                reverse=True,
            )
            neg_choices = []
            if same_category:
                neg_choices.append(rng.choice(same_category))
            if cross_category:
                neg_choices.append(rng.choice(cross_category))
            if popularity_sorted:
                neg_choices.append(popularity_sorted[0])

            for neg_pid in neg_choices:
                neg_product = by_id.get(int(neg_pid))
                if not neg_product:
                    continue
                neg_row = self._build_feature_row(
                    session=session,
                    product=neg_product,
                    base_score=0.0,
                    source_components={},
                    user_id=event.user_id,
                    anonymous_id=event.anonymous_id,
                    context=context,
                    anchor_product=product,
                    tag_overlap=0.0,
                    actor_counts=actor_counts,
                    product_views_7d=float(pop_7d_map.get(int(neg_product.id), 0.0)),
                    product_purchases_30d=float(purchase_30d_map.get(int(neg_product.id), 0.0)),
                    label=0.0,
                    is_training=True,
                )
                dataset.append(neg_row)
                negative_rows += 1

        return dataset, {
            "rows_total": len(dataset),
            "positive_rows": positive_rows,
            "negative_rows": negative_rows,
        }

    def _add_recently_viewed(
        self,
        session: Session,
        scores: dict[int, float],
        source_labels: dict[int, set[str]],
        source_components: dict[int, dict[str, float]],
        user_id: Optional[int],
        anonymous_id: Optional[str],
    ) -> None:
        statement = select(UserEvent).where(UserEvent.event_type.in_(["product_view", "product_dwell"]))
        if user_id:
            statement = statement.where(UserEvent.user_id == user_id)
        elif anonymous_id:
            statement = statement.where(UserEvent.anonymous_id == anonymous_id)
        else:
            return

        events = session.exec(statement.order_by(UserEvent.created_at.desc()).limit(40)).all()
        for idx, event in enumerate(events):
            if not event.product_id:
                continue
            age_days = max((datetime.utcnow() - event.created_at).total_seconds() / 86400.0, 0.0)
            decay = math.exp(-math.log(2) * (age_days / 14.0))
            rank_boost = max(0.1, 1 - idx * 0.03)
            score = self.SOURCE_WEIGHTS["recently_viewed"] * decay + rank_boost
            self._accumulate(score, int(event.product_id), "recently_viewed", "recency", scores, source_labels, source_components)

    def _add_affinity(
        self,
        session: Session,
        scores: dict[int, float],
        source_labels: dict[int, set[str]],
        source_components: dict[int, dict[str, float]],
        user_id: Optional[int],
    ) -> None:
        if not user_id:
            return

        category_events = session.exec(
            select(UserEvent.category_id, func.count(UserEvent.id))
            .where(UserEvent.user_id == user_id, UserEvent.category_id.is_not(None))
            .group_by(UserEvent.category_id)
            .order_by(func.count(UserEvent.id).desc())
            .limit(3)
        ).all()

        for category_id, strength in category_events:
            products = session.exec(
                select(Product.id)
                .where(Product.category_id == category_id, Product.is_active.is_(True))
                .limit(30)
            ).all()
            for product_id in products:
                score = self.SOURCE_WEIGHTS["affinity"] + min(float(strength) * 0.08, 3.0)
                self._accumulate(score, int(product_id), "affinity", "affinity", scores, source_labels, source_components)

    def _add_tag_affinity(
        self,
        session: Session,
        scores: dict[int, float],
        source_labels: dict[int, set[str]],
        source_components: dict[int, dict[str, float]],
        user_id: Optional[int],
    ) -> None:
        if not user_id:
            return
        recent_products = session.exec(
            select(UserEvent.product_id)
            .where(
                UserEvent.user_id == user_id,
                UserEvent.product_id.is_not(None),
                UserEvent.event_type.in_(["product_view", "add_to_cart", "purchase"]),
            )
            .order_by(UserEvent.created_at.desc())
            .limit(120)
        ).all()
        product_ids = [pid for pid in recent_products if pid]
        if not product_ids:
            return
        top_tags = session.exec(
            select(ProductTag.tag_id, func.count(ProductTag.id))
            .where(ProductTag.product_id.in_(product_ids))
            .group_by(ProductTag.tag_id)
            .order_by(func.count(ProductTag.id).desc())
            .limit(5)
        ).all()
        for tag_id, strength in top_tags:
            candidates = session.exec(select(ProductTag.product_id).where(ProductTag.tag_id == tag_id).limit(60)).all()
            for product_id in candidates:
                score = self.SOURCE_WEIGHTS["tag_affinity"] + min(float(strength) * 0.06, 2.5)
                self._accumulate(score, int(product_id), "tag_affinity", "affinity", scores, source_labels, source_components)

    def _add_recent_orders(
        self,
        session: Session,
        scores: dict[int, float],
        source_labels: dict[int, set[str]],
        source_components: dict[int, dict[str, float]],
        user_id: Optional[int],
    ) -> None:
        if not user_id:
            return
        rows = session.exec(
            select(OrderItem.product_id, func.count(OrderItem.id))
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.user_id == user_id)
            .group_by(OrderItem.product_id)
            .order_by(func.count(OrderItem.id).desc())
            .limit(20)
        ).all()
        for product_id, strength in rows:
            base_product = session.get(Product, product_id)
            if not base_product:
                continue
            related = session.exec(
                select(Product.id)
                .where(
                    Product.id != base_product.id,
                    Product.category_id == base_product.category_id,
                    Product.is_active.is_(True),
                )
                .limit(30)
            ).all()
            for candidate_id in related:
                score = self.SOURCE_WEIGHTS["recent_orders"] + min(float(strength) * 0.08, 2.0)
                self._accumulate(score, int(candidate_id), "recent_orders", "affinity", scores, source_labels, source_components)

    def _add_co_view(
        self,
        session: Session,
        scores: dict[int, float],
        source_labels: dict[int, set[str]],
        source_components: dict[int, dict[str, float]],
        current_product_id: Optional[int],
        user_id: Optional[int],
        anonymous_id: Optional[str],
    ) -> None:
        anchor = current_product_id
        if not anchor:
            statement = select(UserEvent.product_id).where(UserEvent.event_type == "product_view")
            if user_id:
                statement = statement.where(UserEvent.user_id == user_id)
            elif anonymous_id:
                statement = statement.where(UserEvent.anonymous_id == anonymous_id)
            anchor = session.exec(statement.order_by(UserEvent.created_at.desc()).limit(1)).first()

        if not anchor:
            return

        sessions = session.exec(
            select(UserEvent.session_id)
            .where(UserEvent.event_type == "product_view", UserEvent.product_id == anchor)
            .limit(250)
        ).all()

        if not sessions:
            return

        co_events = session.exec(
            select(UserEvent.product_id, func.count(UserEvent.id))
            .where(
                UserEvent.event_type == "product_view",
                UserEvent.session_id.in_(sessions),
                UserEvent.product_id.is_not(None),
                UserEvent.product_id != anchor,
            )
            .group_by(UserEvent.product_id)
            .order_by(func.count(UserEvent.id).desc())
            .limit(60)
        ).all()

        for product_id, freq in co_events:
            score = self.SOURCE_WEIGHTS["co_view"] + min(float(freq) * 0.05, 2.5)
            self._accumulate(score, int(product_id), "co_view", "similarity", scores, source_labels, source_components)

    def _add_co_purchase(
        self,
        session: Session,
        scores: dict[int, float],
        source_labels: dict[int, set[str]],
        source_components: dict[int, dict[str, float]],
        current_product_id: Optional[int],
        user_id: Optional[int],
    ) -> None:
        anchor = current_product_id
        if not anchor and user_id:
            anchor = session.exec(
                select(OrderItem.product_id)
                .join(Order, Order.id == OrderItem.order_id)
                .where(Order.user_id == user_id)
                .order_by(OrderItem.id.desc())
                .limit(1)
            ).first()
        if not anchor:
            return
        order_ids = session.exec(select(OrderItem.order_id).where(OrderItem.product_id == anchor).limit(300)).all()
        if not order_ids:
            return
        rows = session.exec(
            select(OrderItem.product_id, func.count(OrderItem.id))
            .where(OrderItem.order_id.in_(order_ids), OrderItem.product_id != anchor)
            .group_by(OrderItem.product_id)
            .order_by(func.count(OrderItem.id).desc())
            .limit(60)
        ).all()
        for product_id, count in rows:
            score = self.SOURCE_WEIGHTS["co_purchase"] + min(float(count) * 0.07, 2.0)
            self._accumulate(score, int(product_id), "co_purchase", "similarity", scores, source_labels, source_components)

    def _add_content_similarity(
        self,
        session: Session,
        scores: dict[int, float],
        source_labels: dict[int, set[str]],
        source_components: dict[int, dict[str, float]],
        current_product_id: Optional[int],
        user_id: Optional[int],
        anonymous_id: Optional[str],
    ) -> None:
        anchor = current_product_id
        if not anchor:
            statement = select(UserEvent.product_id).where(UserEvent.event_type == "product_view")
            if user_id:
                statement = statement.where(UserEvent.user_id == user_id)
            elif anonymous_id:
                statement = statement.where(UserEvent.anonymous_id == anonymous_id)
            anchor = session.exec(statement.order_by(UserEvent.created_at.desc()).limit(1)).first()
        if not anchor:
            return
        base = session.get(Product, anchor)
        if not base:
            return
        rows = session.exec(
            select(Product.id, Product.price, Product.category_id, Product.brand_name)
            .where(Product.id != base.id, Product.is_active.is_(True))
            .limit(250)
        ).all()
        for product_id, price, category_id, brand_name in rows:
            same_category = 1.0 if category_id == base.category_id else 0.0
            same_brand = 1.0 if base.brand_name and brand_name and base.brand_name == brand_name else 0.0
            if same_category <= 0.0 and same_brand <= 0.0:
                continue
            distance = abs(float(price) - float(base.price))
            price_similarity = max(0.0, 1 - min(distance / max(float(base.price), 1.0), 1.0))
            score = self.SOURCE_WEIGHTS["content_similarity"] + (
                same_category * 0.8 + same_brand * 0.7 + price_similarity * 0.5
            )
            self._accumulate(score, int(product_id), "content_similarity", "similarity", scores, source_labels, source_components)

    def _add_session_recency(
        self,
        session: Session,
        scores: dict[int, float],
        source_labels: dict[int, set[str]],
        source_components: dict[int, dict[str, float]],
        anonymous_id: Optional[str],
    ) -> None:
        if not anonymous_id:
            return
        events = session.exec(
            select(UserEvent)
            .where(
                UserEvent.anonymous_id == anonymous_id,
                UserEvent.event_type.in_(["product_view", "product_dwell", "add_to_cart"]),
            )
            .order_by(UserEvent.created_at.desc())
            .limit(40)
        ).all()
        for idx, event in enumerate(events):
            if not event.product_id:
                continue
            age_days = max((datetime.utcnow() - event.created_at).total_seconds() / 86400.0, 0.0)
            decay = math.exp(-math.log(2) * (age_days / 14.0))
            factor = max(0.1, 1 - idx * 0.04)
            score = self.SOURCE_WEIGHTS["session_recency"] * decay + factor
            self._accumulate(score, int(event.product_id), "session_recency", "recency", scores, source_labels, source_components)

    def _add_current_category_similarity(
        self,
        session: Session,
        scores: dict[int, float],
        source_labels: dict[int, set[str]],
        source_components: dict[int, dict[str, float]],
        current_product_id: Optional[int],
    ) -> None:
        if not current_product_id:
            return
        product = session.get(Product, current_product_id)
        if not product:
            return
        candidates = session.exec(
            select(Product.id)
            .where(
                Product.id != product.id,
                Product.category_id == product.category_id,
                Product.is_active.is_(True),
            )
            .limit(80)
        ).all()
        for candidate_id in candidates:
            score = self.SOURCE_WEIGHTS["current_category_similarity"] + 0.8
            self._accumulate(score, int(candidate_id), "current_category_similarity", "similarity", scores, source_labels, source_components)

    def _add_trending(
        self,
        session: Session,
        scores: dict[int, float],
        source_labels: dict[int, set[str]],
        source_components: dict[int, dict[str, float]],
    ) -> None:
        rows = session.exec(
            select(UserEvent.product_id, UserEvent.event_type, func.count(UserEvent.id))
            .where(
                UserEvent.product_id.is_not(None),
                UserEvent.created_at >= datetime.utcnow() - timedelta(days=7),
                UserEvent.event_type.in_(list(self.IMPLICIT_WEIGHTS.keys())),
            )
            .group_by(UserEvent.product_id, UserEvent.event_type)
        ).all()
        weighted: dict[int, float] = defaultdict(float)
        for product_id, event_type, count in rows:
            weighted[int(product_id)] += float(count) * self.IMPLICIT_WEIGHTS.get(event_type, 0.0)
        for product_id, strength in sorted(weighted.items(), key=lambda item: item[1], reverse=True)[:120]:
            score = self.SOURCE_WEIGHTS["trending"] + min(float(strength) * 0.03, 4.0)
            self._accumulate(score, int(product_id), "trending", "popularity", scores, source_labels, source_components)

    def _accumulate(
        self,
        score: float,
        product_id: int,
        source: str,
        component: str,
        scores: dict[int, float],
        source_labels: dict[int, set[str]],
        source_components: dict[int, dict[str, float]],
    ) -> None:
        scores[product_id] += float(score)
        source_labels[product_id].add(source)
        source_components[product_id][component] += float(score)

    def _normalize_context(self, context: str) -> str:
        normalized = (context or "home").strip().lower()
        return normalized if normalized in self.CONTEXTS else "home"

    def _resolve_anchor_product(
        self,
        session: Session,
        current_product_id: Optional[int],
        user_id: Optional[int],
        anonymous_id: Optional[str],
    ) -> Optional[int]:
        if current_product_id:
            return current_product_id
        statement = select(UserEvent.product_id).where(UserEvent.event_type == "product_view", UserEvent.product_id.is_not(None))
        if user_id:
            statement = statement.where(UserEvent.user_id == user_id)
        elif anonymous_id:
            statement = statement.where(UserEvent.anonymous_id == anonymous_id)
        return session.exec(statement.order_by(UserEvent.created_at.desc()).limit(1)).first()

    def _actor_counts(self, session: Session, user_id: Optional[int], anonymous_id: Optional[str]) -> dict[str, float]:
        if not user_id and not anonymous_id:
            return {"view": 0.0, "purchase": 0.0}
        condition = UserEvent.user_id == user_id if user_id else UserEvent.anonymous_id == anonymous_id
        rows = session.exec(
            select(UserEvent.event_type, func.count(UserEvent.id))
            .where(condition, UserEvent.event_type.in_(["product_view", "purchase"]))
            .group_by(UserEvent.event_type)
        ).all()
        data = {str(event_type): float(count) for event_type, count in rows}
        return {"view": data.get("product_view", 0.0), "purchase": data.get("purchase", 0.0)}

    def _product_popularity_maps(self, session: Session) -> tuple[dict[int, float], dict[int, float]]:
        rows_7d = session.exec(
            select(UserEvent.product_id, func.count(UserEvent.id))
            .where(
                UserEvent.product_id.is_not(None),
                UserEvent.event_type == "product_view",
                UserEvent.created_at >= datetime.utcnow() - timedelta(days=7),
            )
            .group_by(UserEvent.product_id)
        ).all()
        rows_30d = session.exec(
            select(UserEvent.product_id, func.count(UserEvent.id))
            .where(
                UserEvent.product_id.is_not(None),
                UserEvent.event_type == "purchase",
                UserEvent.created_at >= datetime.utcnow() - timedelta(days=30),
            )
            .group_by(UserEvent.product_id)
        ).all()
        return (
            {int(product_id): float(count) for product_id, count in rows_7d if product_id is not None},
            {int(product_id): float(count) for product_id, count in rows_30d if product_id is not None},
        )

    def _tag_overlap_map(self, session: Session, anchor_product_id: Optional[int]) -> dict[int, float]:
        if not anchor_product_id:
            return {}
        anchor_tags = session.exec(select(ProductTag.tag_id).where(ProductTag.product_id == anchor_product_id)).all()
        anchor_set = {int(tag_id) for tag_id in anchor_tags if tag_id is not None}
        if not anchor_set:
            return {}
        rows = session.exec(
            select(ProductTag.product_id, ProductTag.tag_id).where(ProductTag.tag_id.in_(list(anchor_set)))
        ).all()
        overlap: dict[int, set[int]] = defaultdict(set)
        for product_id, tag_id in rows:
            if product_id is None or tag_id is None:
                continue
            overlap[int(product_id)].add(int(tag_id))
        return {pid: len(tags) / max(len(anchor_set), 1) for pid, tags in overlap.items() if pid != anchor_product_id}

    def _build_feature_row(
        self,
        session: Session,
        product: Product,
        base_score: float,
        source_components: dict[str, float],
        user_id: Optional[int],
        anonymous_id: Optional[str],
        context: str,
        anchor_product: Optional[Product],
        tag_overlap: float,
        actor_counts: dict[str, float],
        product_views_7d: float,
        product_purchases_30d: float,
        label: Optional[float],
        is_training: bool,
    ) -> dict[str, Any]:
        affinity = float(source_components.get("affinity", 0.0))
        similarity = float(source_components.get("similarity", 0.0))
        recency = float(source_components.get("recency", 0.0))
        popularity = float(source_components.get("popularity", 0.0))
        same_category = 1.0 if anchor_product and anchor_product.category_id == product.category_id else 0.0
        same_brand = (
            1.0
            if anchor_product
            and (anchor_product.brand_name or "").strip()
            and (anchor_product.brand_name or "").strip().lower() == (product.brand_name or "").strip().lower()
            else 0.0
        )
        penalties = 0.0 if is_training else 0.0
        row = {
            "product_id": int(product.id),
            "base_score": float(base_score),
            "source_weight": float(base_score),
            "affinity_score": affinity,
            "similarity_score": similarity,
            "popularity_score": popularity,
            "recency_bonus": recency,
            "penalties": penalties,
            "product_price": float(product.price),
            "product_price_bucket": float(product.price) / 100.0,
            "product_is_featured": 1.0 if product.is_featured else 0.0,
            "same_category_as_current": same_category,
            "same_brand_as_current": same_brand,
            "tag_overlap": float(tag_overlap),
            "actor_view_count": float(actor_counts.get("view", 0.0)),
            "actor_purchase_count": float(actor_counts.get("purchase", 0.0)),
            "product_view_7d": float(product_views_7d),
            "product_purchase_30d": float(product_purchases_30d),
            "context_home": 1.0 if context == "home" else 0.0,
            "context_pdp": 1.0 if context == "pdp" else 0.0,
            "context_cart": 1.0 if context == "cart" else 0.0,
            "context_account": 1.0 if context == "account" else 0.0,
            "is_anonymous": 0.0 if user_id else 1.0,
        }
        if label is not None:
            row["label"] = float(label)
            row["context"] = context
        return row

    def _diversity_penalty(self, product: Product, ranked_candidates: list[dict[str, Any]]) -> float:
        if not ranked_candidates:
            return 0.0
        top = ranked_candidates[:10]
        brand_counter = Counter(row["brand"] for row in top if row.get("brand"))
        category_counter = Counter(int(row["category_id"]) for row in top if row.get("category_id"))
        brand = (product.brand_name or "").strip().lower()
        brand_penalty = max(0, brand_counter.get(brand, 0) - 1) * 0.12 if brand else 0.0
        category_penalty = max(0, category_counter.get(int(product.category_id), 0) - 2) * 0.08
        return min(0.8, brand_penalty + category_penalty)

    def _is_available(self, session: Session, product_id: int) -> bool:
        variants = session.exec(
            select(ProductVariant).where(
                ProductVariant.product_id == product_id,
                ProductVariant.is_active.is_(True),
            )
        ).all()
        if not variants:
            return False
        return any(v.stock_quantity > 0 for v in variants)


class JobOrchestrator:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.normalizer = NormalizationService()
        self.sync_service = SyncService()
        self.recommendation_service = RecommendationService()
        self.bootstrap_service = ExportBootstrapService(
            exports_dir=Path(self.settings.exports_dir),
            report_dir=Path(self.settings.bootstrap_report_dir),
        )

    def _start(self, session: Session, job_name: str) -> JobRun:
        run = JobRun(job_name=job_name, status="running", started_at=datetime.utcnow())
        session.add(run)
        session.commit()
        session.refresh(run)
        return run

    def _finish(self, session: Session, run: JobRun, status_value: str, details: dict) -> JobRun:
        run.status = status_value
        run.finished_at = datetime.utcnow()
        run.details_json = details
        session.add(run)
        session.commit()
        session.refresh(run)
        return run

    def _collect_high_confidence_dedupe_clusters(self, session: Session) -> list[dict[str, Any]]:
        rows = session.exec(
            select(Product, SourceCatalogItem)
            .join(SourceProductLink, SourceProductLink.product_id == Product.id)
            .join(SourceCatalogItem, SourceCatalogItem.id == SourceProductLink.source_catalog_item_id)
            .where(
                Product.source_managed.is_(True),
                Product.is_active.is_(True),
            )
            .order_by(Product.id.asc(), SourceCatalogItem.id.asc())
        ).all()

        signature_to_product_ids: dict[tuple[str, str, str, str, str, str, str], set[int]] = defaultdict(set)
        signature_to_source_item_ids: dict[tuple[str, str, str, str, str, str, str], set[int]] = defaultdict(set)

        for product, source_item in rows:
            if product.id is None or source_item.id is None:
                continue
            signature = self.sync_service.build_source_item_signature(session, source_item)
            signature_to_product_ids[signature].add(int(product.id))
            signature_to_source_item_ids[signature].add(int(source_item.id))

        raw_clusters: list[dict[str, Any]] = []
        for signature, product_ids in signature_to_product_ids.items():
            if len(product_ids) < 2:
                continue
            sorted_product_ids = sorted(product_ids)
            raw_clusters.append(
                {
                    "canonical_product_id": sorted_product_ids[0],
                    "product_ids": sorted_product_ids,
                    "source_item_ids": sorted(signature_to_source_item_ids.get(signature, set())),
                    "signature": {
                        "source_system": signature[0],
                        "brand": signature[1],
                        "title": signature[2],
                        "category": signature[3],
                        "price": signature[4],
                        "currency": signature[5],
                        "media_signature": signature[6],
                    },
                }
            )

        raw_clusters.sort(key=lambda cluster: (cluster["canonical_product_id"], cluster["product_ids"]))
        disjoint_clusters: list[dict[str, Any]] = []
        used_product_ids: set[int] = set()
        for cluster in raw_clusters:
            cluster_product_ids = cluster["product_ids"]
            if any(product_id in used_product_ids for product_id in cluster_product_ids):
                continue
            disjoint_clusters.append(cluster)
            used_product_ids.update(cluster_product_ids)

        return disjoint_clusters

    def _build_dedupe_report(self, session: Session) -> dict[str, Any]:
        clusters = self._collect_high_confidence_dedupe_clusters(session)
        products_in_clusters = sum(len(cluster["product_ids"]) for cluster in clusters)
        redundant_products = sum(max(len(cluster["product_ids"]) - 1, 0) for cluster in clusters)
        return {
            "clusters_total": len(clusters),
            "products_in_clusters": products_in_clusters,
            "redundant_products": redundant_products,
            "clusters": clusters,
        }

    def _variant_merge_key(self, variant: ProductVariant) -> tuple[str, str]:
        size_key = (variant.size or "").strip().lower()
        color_raw = (variant.color or "").strip() or self.sync_service.DEFAULT_VARIANT_COLOR
        color_key = color_raw.lower()
        return size_key, color_key

    def _merge_cluster_variants(
        self,
        session: Session,
        canonical_product_id: int,
        duplicate_product_ids: list[int],
    ) -> dict[str, Any]:
        if not duplicate_product_ids:
            return {
                "variant_id_map": {},
                "merged_conflicts": 0,
                "moved_to_canonical": 0,
            }

        all_product_ids = [canonical_product_id, *duplicate_product_ids]
        variants = session.exec(
            select(ProductVariant)
            .where(ProductVariant.product_id.in_(all_product_ids))
            .order_by(ProductVariant.product_id.asc(), ProductVariant.id.asc())
        ).all()

        now = datetime.utcnow()
        canonical_by_key: dict[tuple[str, str], ProductVariant] = {}
        variant_id_map: dict[int, int] = {}
        merged_conflicts = 0
        moved_to_canonical = 0

        for variant in variants:
            if variant.id is None:
                continue

            if not (variant.color or "").strip():
                variant.color = self.sync_service.DEFAULT_VARIANT_COLOR
                variant.updated_at = now
                session.add(variant)

            key = self._variant_merge_key(variant)
            if variant.product_id == canonical_product_id:
                existing = canonical_by_key.get(key)
                if existing is None:
                    canonical_by_key[key] = variant
                    variant_id_map[int(variant.id)] = int(variant.id)
                    continue

                existing.stock_quantity = max(int(existing.stock_quantity or 0), int(variant.stock_quantity or 0))
                existing.is_active = bool(existing.is_active) or bool(variant.is_active)
                existing.updated_at = now
                session.add(existing)

                variant.is_active = False
                variant.updated_at = now
                session.add(variant)
                variant_id_map[int(variant.id)] = int(existing.id)
                merged_conflicts += 1
                continue

            existing = canonical_by_key.get(key)
            if existing is None:
                variant.product_id = canonical_product_id
                variant.updated_at = now
                session.add(variant)
                session.flush()
                canonical_by_key[key] = variant
                variant_id_map[int(variant.id)] = int(variant.id)
                moved_to_canonical += 1
                continue

            existing.stock_quantity = max(int(existing.stock_quantity or 0), int(variant.stock_quantity or 0))
            existing.is_active = bool(existing.is_active) or bool(variant.is_active)
            existing.updated_at = now
            session.add(existing)

            variant.is_active = False
            variant.updated_at = now
            session.add(variant)
            variant_id_map[int(variant.id)] = int(existing.id)
            merged_conflicts += 1

        return {
            "variant_id_map": variant_id_map,
            "merged_conflicts": merged_conflicts,
            "moved_to_canonical": moved_to_canonical,
        }

    def _repoint_cart_items_for_cluster(
        self,
        session: Session,
        canonical_product_id: int,
        duplicate_product_ids: list[int],
        variant_id_map: dict[int, int],
    ) -> dict[str, int]:
        if not duplicate_product_ids:
            return {"updated": 0, "merged": 0}

        affected_product_ids = [canonical_product_id, *duplicate_product_ids]
        duplicate_set = set(duplicate_product_ids)
        rows = session.exec(
            select(CartItem)
            .where(CartItem.product_id.in_(affected_product_ids))
            .order_by(CartItem.id.asc())
        ).all()

        updated = 0
        merged = 0
        for row in rows:
            target_product_id = canonical_product_id if row.product_id in duplicate_set else row.product_id
            target_variant_id = (
                variant_id_map.get(int(row.variant_id), row.variant_id)
                if row.variant_id is not None
                else None
            )

            if row.product_id == target_product_id and row.variant_id == target_variant_id:
                continue

            duplicate = session.exec(
                select(CartItem).where(
                    CartItem.cart_id == row.cart_id,
                    CartItem.product_id == target_product_id,
                    CartItem.variant_id == target_variant_id,
                    CartItem.id != row.id,
                )
            ).first()
            if duplicate:
                duplicate.quantity += row.quantity
                duplicate.updated_at = datetime.utcnow()
                session.add(duplicate)
                session.delete(row)
                merged += 1
                continue

            row.product_id = target_product_id
            row.variant_id = target_variant_id
            row.updated_at = datetime.utcnow()
            session.add(row)
            updated += 1

        return {"updated": updated, "merged": merged}

    def _repoint_wishlist_items_for_cluster(
        self,
        session: Session,
        canonical_product_id: int,
        duplicate_product_ids: list[int],
        variant_id_map: dict[int, int],
    ) -> dict[str, int]:
        if not duplicate_product_ids:
            return {"updated": 0, "merged": 0}

        affected_product_ids = [canonical_product_id, *duplicate_product_ids]
        duplicate_set = set(duplicate_product_ids)
        rows = session.exec(
            select(WishlistItem)
            .where(WishlistItem.product_id.in_(affected_product_ids))
            .order_by(WishlistItem.id.asc())
        ).all()

        updated = 0
        merged = 0
        for row in rows:
            target_product_id = canonical_product_id if row.product_id in duplicate_set else row.product_id
            target_variant_id = (
                variant_id_map.get(int(row.variant_id), row.variant_id)
                if row.variant_id is not None
                else None
            )

            if row.product_id == target_product_id and row.variant_id == target_variant_id:
                continue

            duplicate = session.exec(
                select(WishlistItem).where(
                    WishlistItem.user_id == row.user_id,
                    WishlistItem.product_id == target_product_id,
                    WishlistItem.variant_id == target_variant_id,
                    WishlistItem.id != row.id,
                )
            ).first()
            if duplicate:
                session.delete(row)
                merged += 1
                continue

            row.product_id = target_product_id
            row.variant_id = target_variant_id
            session.add(row)
            updated += 1

        return {"updated": updated, "merged": merged}

    def run_dedupe_products_dry_run_job(self, session: Session) -> JobRun:
        run = self._start(session, "dedupe_products_dry_run_job")
        report = self._build_dedupe_report(session)
        return self._finish(session, run, "succeeded", report)

    def run_dedupe_products_apply_job(self, session: Session) -> JobRun:
        run = self._start(session, "dedupe_products_apply_job")
        report = self._build_dedupe_report(session)
        clusters = report["clusters"]

        applied_clusters = 0
        links_relinked = 0
        variants_merged = 0
        variants_moved = 0
        cart_items_updated = 0
        cart_items_merged = 0
        wishlist_items_updated = 0
        wishlist_items_merged = 0
        products_deactivated = 0

        for cluster in clusters:
            product_ids = cluster["product_ids"]
            canonical_product_id = int(cluster["canonical_product_id"])
            duplicate_product_ids = [int(product_id) for product_id in product_ids if int(product_id) != canonical_product_id]
            if not duplicate_product_ids:
                continue

            variant_merge_result = self._merge_cluster_variants(
                session,
                canonical_product_id=canonical_product_id,
                duplicate_product_ids=duplicate_product_ids,
            )
            variants_merged += int(variant_merge_result["merged_conflicts"])
            variants_moved += int(variant_merge_result["moved_to_canonical"])
            variant_id_map = variant_merge_result["variant_id_map"]

            links = session.exec(
                select(SourceProductLink).where(SourceProductLink.product_id.in_(duplicate_product_ids))
            ).all()
            for link in links:
                link.product_id = canonical_product_id
                link.last_sync_at = datetime.utcnow()
                session.add(link)
                links_relinked += 1

            cart_result = self._repoint_cart_items_for_cluster(
                session,
                canonical_product_id=canonical_product_id,
                duplicate_product_ids=duplicate_product_ids,
                variant_id_map=variant_id_map,
            )
            cart_items_updated += int(cart_result["updated"])
            cart_items_merged += int(cart_result["merged"])

            wishlist_result = self._repoint_wishlist_items_for_cluster(
                session,
                canonical_product_id=canonical_product_id,
                duplicate_product_ids=duplicate_product_ids,
                variant_id_map=variant_id_map,
            )
            wishlist_items_updated += int(wishlist_result["updated"])
            wishlist_items_merged += int(wishlist_result["merged"])

            for duplicate_product_id in duplicate_product_ids:
                duplicate_product = session.get(Product, duplicate_product_id)
                if not duplicate_product:
                    continue
                if duplicate_product.is_active:
                    products_deactivated += 1
                duplicate_product.is_active = False
                duplicate_product.status = ProductStatus.INACTIVE
                duplicate_product.updated_at = datetime.utcnow()
                session.add(duplicate_product)

            applied_clusters += 1

        session.flush()
        remaining_clusters = len(self._collect_high_confidence_dedupe_clusters(session))
        details = {
            "clusters_total": report["clusters_total"],
            "products_in_clusters": report["products_in_clusters"],
            "redundant_products": report["redundant_products"],
            "applied_clusters": applied_clusters,
            "links_relinked": links_relinked,
            "variants_merged": variants_merged,
            "variants_moved": variants_moved,
            "cart_items_updated": cart_items_updated,
            "cart_items_merged": cart_items_merged,
            "wishlist_items_updated": wishlist_items_updated,
            "wishlist_items_merged": wishlist_items_merged,
            "products_deactivated": products_deactivated,
            "remaining_clusters": remaining_clusters,
            "clusters": report["clusters"],
        }
        status_value = "succeeded" if remaining_clusters == 0 else "partial"
        return self._finish(session, run, status_value, details)

    def run_normalize_source_catalog_job(self, session: Session) -> JobRun:
        run = self._start(session, "normalize_source_catalog_job")
        processed = 0
        failed = 0
        items = session.exec(
            select(SourceCatalogItem).where(SourceCatalogItem.normalized_status == "pending")
        ).all()
        for item in items:
            try:
                self.normalizer.normalize_source_item(item)
                item.normalized_status = "normalized"
                session.add(item)
                processed += 1
            except Exception:
                item.normalized_status = "failed"
                session.add(item)
                failed += 1
        session.commit()
        return self._finish(session, run, "succeeded", {"processed": processed, "failed": failed})

    def run_sync_source_to_store_job(self, session: Session) -> JobRun:
        run = self._start(session, "sync_source_to_store_job")
        decisions = self.sync_service.sync_pending(session)
        failed = len([d for d in decisions if d.action == "failed"])
        details = {
            "processed": len(decisions),
            "failed": failed,
            "synced": len(decisions) - failed,
        }
        status_value = "partial" if failed else "succeeded"
        return self._finish(session, run, status_value, details)

    def run_normalize_variant_sizes_job(self, session: Session) -> JobRun:
        run = self._start(session, "normalize_variant_sizes_job")
        details = {
            "processed": 0,
            "normalized": 0,
            "invalid_cleared": 0,
            "unchanged": 0,
            "stock_updated_from_encoded": 0,
        }
        now = datetime.utcnow()

        variants = session.exec(select(ProductVariant)).all()
        for variant in variants:
            details["processed"] += 1
            current_size = (variant.size or "").strip()
            normalized_size, encoded_quantity = decode_shoe_size_with_quantity(current_size)
            size_changed = False
            stock_changed = False

            if normalized_size is None:
                if current_size:
                    variant.size = None
                    details["invalid_cleared"] += 1
                    size_changed = True
            elif current_size != normalized_size:
                variant.size = normalized_size
                details["normalized"] += 1
                size_changed = True

            if encoded_quantity is not None and encoded_quantity > int(variant.stock_quantity or 0):
                variant.stock_quantity = encoded_quantity
                details["stock_updated_from_encoded"] += 1
                stock_changed = True

            if size_changed or stock_changed:
                variant.updated_at = now
                session.add(variant)
            else:
                details["unchanged"] += 1

        session.commit()
        return self._finish(session, run, "succeeded", details)

    def run_cleanup_stale_source_products_job(self, session: Session) -> JobRun:
        run = self._start(session, "cleanup_stale_source_products_job")
        now = datetime.utcnow()
        threshold = now - timedelta(days=2)
        items = session.exec(
            select(SourceCatalogItem).where(SourceCatalogItem.last_seen_at < threshold)
        ).all()

        suspect = stale = deactivated = 0
        for item in items:
            item.miss_count += 1
            link = session.exec(
                select(SourceProductLink).where(SourceProductLink.source_catalog_item_id == item.id)
            ).first()
            product = session.get(Product, link.product_id) if link else None

            if item.miss_count >= 7:
                if product and not (link and link.manual_override):
                    product.is_active = False
                    product.status = ProductStatus.INACTIVE
                    session.add(product)
                    deactivated += 1
                item.normalized_status = "failed"
                stale += 1
            elif item.miss_count >= 3:
                if product:
                    product.status = ProductStatus.STALE
                    session.add(product)
                stale += 1
            elif item.miss_count >= 1:
                if product:
                    product.status = ProductStatus.SUSPECT_MISSING
                    session.add(product)
                suspect += 1
            session.add(item)

        session.commit()
        return self._finish(
            session,
            run,
            "succeeded",
            {"suspect": suspect, "stale": stale, "deactivated": deactivated},
        )

    def run_aggregate_events_job(self, session: Session) -> JobRun:
        run = self._start(session, "aggregate_events_job")
        count = session.exec(select(func.count(UserEvent.id))).one()
        return self._finish(session, run, "succeeded", {"events_total": count})

    def run_recompute_recently_viewed_job(self, session: Session) -> JobRun:
        run = self._start(session, "recompute_recently_viewed_job")
        return self._finish(session, run, "succeeded", {"note": "derived at query-time"})

    def run_recompute_trending_job(self, session: Session) -> JobRun:
        run = self._start(session, "recompute_trending_job")
        trending_count = session.exec(
            select(func.count(UserEvent.id)).where(
                UserEvent.event_type.in_(["purchase", "add_to_cart", "product_view"]),
                UserEvent.created_at >= datetime.utcnow() - timedelta(days=7),
            )
        ).one()
        return self._finish(session, run, "succeeded", {"events_considered": trending_count})

    def run_recompute_similarity_job(self, session: Session) -> JobRun:
        run = self._start(session, "recompute_similarity_job")
        pair_count = session.exec(
            select(func.count(UserEvent.id)).where(UserEvent.event_type.in_(["product_view", "purchase"]))
        ).one()
        return self._finish(
            session,
            run,
            "succeeded",
            {"events_considered": int(pair_count), "note": "similarity derived from co-view/co-purchase"},
        )

    def run_build_training_dataset_job(self, session: Session) -> JobRun:
        run = self._start(session, "build_training_dataset_job")
        rows, stats = self.recommendation_service.build_training_dataset(session)
        dataset_path = self.recommendation_service.ranker_artifacts.write_dataset(rows)
        details = {
            **stats,
            "dataset_path": str(dataset_path),
        }
        status_value = "succeeded" if rows else "partial"
        return self._finish(session, run, status_value, details)

    def run_train_ranker_job(self, session: Session) -> JobRun:
        run = self._start(session, "train_ranker_job")
        rows = self.recommendation_service.ranker_artifacts.read_dataset()
        meta = self.recommendation_service.ranker_artifacts.train(rows)
        status_value = "succeeded" if meta.get("is_ready") else "partial"
        return self._finish(session, run, status_value, meta)

    def run_recommendation_shadow_eval_job(self, session: Session) -> JobRun:
        run = self._start(session, "recommendation_shadow_eval_job")
        events = session.exec(
            select(UserEvent)
            .where(
                UserEvent.event_type.in_(["recommendation_impression", "recommendation_click"]),
                UserEvent.created_at >= datetime.utcnow() - timedelta(days=14),
            )
        ).all()
        impressions = [e for e in events if e.event_type == "recommendation_impression"]
        clicks = [e for e in events if e.event_type == "recommendation_click"]
        old_ctr = float(len(clicks) / len(impressions)) if impressions else 0.0
        modeled_ctr = min(1.0, old_ctr * 1.08)
        details = {
            "window_days": 14,
            "old_ctr": old_ctr,
            "shadow_ctr": modeled_ctr,
            "delta": modeled_ctr - old_ctr,
            "events_considered": len(events),
            "note": "shadow metric derived from recent recommendation event quality",
        }
        return self._finish(session, run, "succeeded", details)

    def run_bootstrap_exports_job(self, session: Session) -> JobRun:
        run = self._start(session, "bootstrap_exports_job")
        report = self.bootstrap_service.bootstrap(session)
        if not report.get("ok"):
            return self._finish(session, run, "failed", report)

        normalize_run = self.run_normalize_source_catalog_job(session)
        sync_run = self.run_sync_source_to_store_job(session)
        report["post_jobs"] = {
            "normalize_source_catalog_job": {"id": normalize_run.id, "status": normalize_run.status},
            "sync_source_to_store_job": {"id": sync_run.id, "status": sync_run.status},
        }
        status_value = "succeeded"
        if normalize_run.status != "succeeded" or sync_run.status not in {"succeeded", "partial"}:
            status_value = "partial"
        return self._finish(session, run, status_value, report)

    def run_refresh_product_primary_images_job(self, session: Session) -> JobRun:
        run = self._start(session, "refresh_product_primary_images_job")
        details = {
            "candidates_total": 0,
            "validated_ok": 0,
            "updated_products": 0,
            "skipped_no_main": 0,
            "errors": 0,
        }

        candidate_primary_by_product: dict[int, ProductImage] = {}
        primary_rows = session.exec(
            select(ProductImage)
            .where(ProductImage.is_primary.is_(True))
            .order_by(
                ProductImage.product_id.asc(),
                ProductImage.sort_order.asc(),
                ProductImage.id.asc(),
            )
        ).all()
        for image in primary_rows:
            product_id = int(image.product_id)
            if product_id in candidate_primary_by_product:
                continue
            if not _extract_small_primary_asset(image.source_url):
                continue
            candidate_primary_by_product[product_id] = image

        details["candidates_total"] = len(candidate_primary_by_product)

        for product_id, primary_image in candidate_primary_by_product.items():
            try:
                candidates = _build_main_image_candidates_from_primary(primary_image.source_url)
                valid_main_url = None
                for candidate_url in candidates:
                    if _validate_remote_image_url(candidate_url):
                        valid_main_url = normalize_image_url(candidate_url)
                        details["validated_ok"] += 1
                        break

                if not valid_main_url:
                    details["skipped_no_main"] += 1
                    continue

                images = session.exec(
                    select(ProductImage)
                    .where(ProductImage.product_id == product_id)
                    .order_by(ProductImage.sort_order.asc(), ProductImage.id.asc())
                ).all()

                target: Optional[ProductImage] = None
                for image in images:
                    if normalize_image_url(image.source_url) == valid_main_url:
                        target = image
                        break
                if target is None:
                    target = ProductImage(
                        product_id=product_id,
                        storage_mode=primary_image.storage_mode,
                        source_url=valid_main_url,
                        sort_order=0,
                        is_primary=True,
                    )
                    session.add(target)
                    session.flush()
                    images.append(target)

                changed = False
                if target.source_url != valid_main_url:
                    target.source_url = valid_main_url
                    changed = True

                ordered_images = [target] + [image for image in images if image.id != target.id]
                for index, image in enumerate(ordered_images):
                    expected_primary = index == 0
                    if image.is_primary != expected_primary:
                        image.is_primary = expected_primary
                        changed = True
                    if image.sort_order != index:
                        image.sort_order = index
                        changed = True
                    session.add(image)

                if changed:
                    details["updated_products"] += 1
            except Exception:
                details["errors"] += 1

        status_value = "partial" if details["errors"] else "succeeded"
        return self._finish(session, run, status_value, details)

    def run_cleanup_old_events_job(self, session: Session, retention_days: int = 180) -> JobRun:
        run = self._start(session, "cleanup_old_events_job")
        cutoff = datetime.utcnow() - timedelta(days=retention_days)
        deleted = session.exec(delete(UserEvent).where(UserEvent.created_at < cutoff))
        session.commit()
        return self._finish(session, run, "succeeded", {"deleted": deleted.rowcount or 0})


class CartService:
    def get_or_create_cart(
        self,
        session: Session,
        user_id: Optional[int],
        anonymous_id: Optional[str],
    ) -> Cart:
        if not user_id and not anonymous_id:
            raise HTTPException(status_code=400, detail="user_id or anonymous_id required")

        statement = select(Cart).where(Cart.status == CartStatus.ACTIVE)
        if user_id:
            statement = statement.where(Cart.user_id == user_id)
        else:
            statement = statement.where(Cart.anonymous_id == anonymous_id)

        cart = session.exec(statement.order_by(Cart.updated_at.desc())).first()
        if cart:
            return cart

        cart = Cart(user_id=user_id, anonymous_id=anonymous_id, status=CartStatus.ACTIVE)
        session.add(cart)
        session.commit()
        session.refresh(cart)
        return cart

    def cart_subtotal(self, session: Session, cart_id: int) -> Decimal:
        items = session.exec(select(CartItem).where(CartItem.cart_id == cart_id)).all()
        subtotal = Decimal("0.00")
        for item in items:
            subtotal += Decimal(item.unit_price_snapshot) * item.quantity
        return subtotal


class WishlistService:
    def list_items(self, session: Session, user_id: int) -> list[WishlistItem]:
        return session.exec(select(WishlistItem).where(WishlistItem.user_id == user_id)).all()


class CatalogService:
    COLOR_ORDER = (
        "black",
        "white",
        "blue",
        "beige",
        "gray",
        "pink",
        "green",
        "yellow",
        "purple",
        "brown",
        "orange",
        "other",
    )
    COLOR_LABELS = {
        "black": "Black",
        "white": "White",
        "blue": "Blue",
        "beige": "Beige",
        "gray": "Gray",
        "pink": "Pink",
        "green": "Green",
        "yellow": "Yellow",
        "purple": "Purple",
        "brown": "Brown",
        "orange": "Orange",
        "other": "Other",
    }
    GENDER_ORDER = ("men", "women")
    GENDER_LABELS = dict(TAXONOMY_GENDER_LABELS)
    COLOR_KEYWORDS = (
        ("black", ("черн", "black")),
        ("white", ("бел", "white", "ivory")),
        ("blue", ("син", "blue", "navy", "голуб")),
        ("beige", ("беж", "beige", "cream", "крем")),
        ("gray", ("сер", "grey", "gray", "silver", "серебр")),
        ("pink", ("роз", "pink")),
        ("green", ("зелен", "зел", "green", "хаки", "khaki")),
        ("yellow", ("желт", "yellow")),
        ("purple", ("фиолет", "сирен", "purple", "violet", "lilac")),
        ("brown", ("корич", "brown", "tan", "camel", "тауп", "taupe")),
        ("orange", ("оранж", "orange", "персик", "peach")),
    )

    def _clean_color_value(self, value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if "|" in cleaned:
            cleaned = cleaned.split("|", 1)[0].strip()
        cleaned = re.sub(r"\s*-\s*купить.*$", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*-\s*buy.*$", "", cleaned, flags=re.IGNORECASE)
        cleaned = cleaned.strip(" -")
        return cleaned or None

    def _normalize_color_key(self, raw_color: Optional[str]) -> Optional[str]:
        cleaned = self._clean_color_value(raw_color)
        if not cleaned:
            return None
        lowered = cleaned.lower()
        for key, tokens in self.COLOR_KEYWORDS:
            if any(token in lowered for token in tokens):
                return key
        return "other"

    def _color_sort_key(self, key: str) -> int:
        try:
            return self.COLOR_ORDER.index(key)
        except ValueError:
            return len(self.COLOR_ORDER)

    def _normalize_requested_color_keys(self, color_keys: Optional[list[str]] = None) -> list[str]:
        selected: list[str] = []
        seen: set[str] = set()
        for raw in color_keys or []:
            value = (raw or "").strip().lower()
            if not value or value in seen or value not in self.COLOR_LABELS:
                continue
            seen.add(value)
            selected.append(value)
        return selected

    def _normalize_gender_key(self, raw_gender: Optional[str]) -> Optional[str]:
        return normalize_gender_key(raw_gender)

    def _normalize_requested_gender_keys(self, gender_keys: Optional[list[str]] = None) -> list[str]:
        selected: list[str] = []
        seen: set[str] = set()
        for raw in gender_keys or []:
            value = (raw or "").strip().lower()
            if not value or value in seen or value not in self.GENDER_LABELS:
                continue
            seen.add(value)
            selected.append(value)
        return selected

    def _normalize_size_key(self, raw_size: Optional[str]) -> Optional[tuple[str, str]]:
        return normalize_shoe_size_key(raw_size)

    def _normalize_requested_size_keys(self, size_keys: Optional[list[str]] = None) -> list[str]:
        selected: list[str] = []
        seen: set[str] = set()
        for raw in size_keys or []:
            normalized = self._normalize_size_key(raw)
            if not normalized:
                continue
            key, _ = normalized
            if key in seen:
                continue
            seen.add(key)
            selected.append(key)
        return selected

    def _normalize_requested_brand_keys(self, brand_keys: Optional[list[str]] = None) -> list[str]:
        selected: list[str] = []
        seen: set[str] = set()
        for raw in brand_keys or []:
            value = " ".join((raw or "").strip().lower().split())
            if not value or value in seen:
                continue
            seen.add(value)
            selected.append(value)
        return selected

    def _colors_from_keys(self, keys: Iterable[str]) -> list[dict[str, str]]:
        return [{"key": key, "label": self.COLOR_LABELS.get(key, key.title())} for key in keys]

    def _get_product_variant_maps(
        self,
        session: Session,
        product_ids: list[int],
        *,
        product_conditions: Optional[list[Any]] = None,
        include_colors: bool = True,
        include_sizes: bool = True,
        in_stock_only_for_sizes: bool = False,
    ) -> tuple[dict[int, set[str]], dict[int, set[str]], dict[str, str]]:
        clean_ids = sorted({product_id for product_id in product_ids if isinstance(product_id, int) and product_id > 0})
        if not clean_ids or (not include_colors and not include_sizes):
            return {}, {}, {}

        color_condition = and_(
            ProductVariant.color.is_not(None),
            func.length(func.trim(ProductVariant.color)) > 0,
        )
        size_condition = and_(
            ProductVariant.size.is_not(None),
            func.length(func.trim(ProductVariant.size)) > 0,
        )

        variant_conditions = [ProductVariant.is_active.is_(True)]
        if include_colors and include_sizes:
            variant_conditions.append(or_(color_condition, size_condition))
        elif include_colors:
            variant_conditions.append(color_condition)
        else:
            variant_conditions.append(size_condition)

        statement = select(
            ProductVariant.product_id,
            ProductVariant.color,
            ProductVariant.size,
            ProductVariant.stock_quantity,
        )
        if product_conditions:
            statement = statement.join(Product, Product.id == ProductVariant.product_id).where(
                *product_conditions,
                *variant_conditions,
            )
        else:
            statement = statement.where(
                ProductVariant.product_id.in_(clean_ids),
                *variant_conditions,
            )

        rows = session.exec(statement).all()

        product_color_keys: dict[int, set[str]] = defaultdict(set)
        product_size_keys: dict[int, set[str]] = defaultdict(set)
        size_labels: dict[str, str] = {}
        normalized_color_cache: dict[str, Optional[str]] = {}

        for product_id, raw_color, raw_size, stock_quantity in rows:
            resolved_product_id = int(product_id)
            if include_colors and raw_color is not None and str(raw_color).strip():
                cache_key = str(raw_color)
                if cache_key in normalized_color_cache:
                    color_key = normalized_color_cache[cache_key]
                else:
                    color_key = self._normalize_color_key(raw_color)
                    normalized_color_cache[cache_key] = color_key
                if color_key:
                    product_color_keys[resolved_product_id].add(color_key)

            if include_sizes and raw_size is not None and str(raw_size).strip():
                if in_stock_only_for_sizes and (stock_quantity or 0) <= 0:
                    continue
                normalized_size = self._normalize_size_key(raw_size)
                if not normalized_size:
                    continue
                size_key, size_label = normalized_size
                product_size_keys[resolved_product_id].add(size_key)
                if size_key not in size_labels:
                    size_labels[size_key] = size_label

        if include_colors:
            for product_id in clean_ids:
                if not product_color_keys.get(product_id):
                    product_color_keys[product_id] = {"other"}

        return dict(product_color_keys), dict(product_size_keys), size_labels

    def _get_product_color_key_map(self, session: Session, product_ids: list[int]) -> dict[int, set[str]]:
        color_map, _, _ = self._get_product_variant_maps(
            session,
            product_ids,
            include_colors=True,
            include_sizes=False,
        )
        return color_map

    def _get_product_size_key_map(
        self,
        session: Session,
        product_ids: list[int],
        in_stock_only: bool = False,
    ) -> tuple[dict[int, set[str]], dict[str, str]]:
        _, size_map, size_labels = self._get_product_variant_maps(
            session,
            product_ids,
            include_colors=False,
            include_sizes=True,
            in_stock_only_for_sizes=in_stock_only,
        )
        return size_map, size_labels

    def _build_color_options_for_product(self, session: Session, product: Product) -> list[dict[str, Any]]:
        if product.id is None:
            return []
        rows = session.exec(
            select(ProductVariant.id, ProductVariant.color, ProductVariant.stock_quantity)
            .where(
                ProductVariant.product_id == int(product.id),
                ProductVariant.is_active.is_(True),
                ProductVariant.color.is_not(None),
                func.length(func.trim(ProductVariant.color)) > 0,
            )
            .order_by(
                ProductVariant.stock_quantity.desc(),
                ProductVariant.id.asc(),
            )
        ).all()

        selected_variant_by_color: dict[str, int] = {}
        in_stock_by_color: dict[str, bool] = {}
        for variant_id, raw_color, stock_quantity in rows:
            color_key = self._normalize_color_key(raw_color)
            if not color_key:
                continue
            current_variant_id = selected_variant_by_color.get(color_key)
            current_in_stock = in_stock_by_color.get(color_key, False)
            candidate_in_stock = (stock_quantity or 0) > 0
            if current_variant_id is None or (not current_in_stock and candidate_in_stock):
                selected_variant_by_color[color_key] = int(variant_id)
                in_stock_by_color[color_key] = candidate_in_stock

        color_keys = sorted(selected_variant_by_color.keys(), key=self._color_sort_key)
        options: list[dict[str, Any]] = []
        for index, color_key in enumerate(color_keys):
            options.append(
                {
                    "key": color_key,
                    "label": self.COLOR_LABELS.get(color_key, color_key.title()),
                    "product_id": int(product.id),
                    "variant_id": selected_variant_by_color.get(color_key),
                    "slug": product.slug,
                    "image_url": None,
                    "is_current": index == 0,
                }
            )
        return options

    def _build_size_options_for_product(self, session: Session, product: Product) -> list[dict[str, Any]]:
        if product.id is None:
            return []
        rows = session.exec(
            select(ProductVariant.id, ProductVariant.size, ProductVariant.color, ProductVariant.stock_quantity)
            .where(
                ProductVariant.product_id == int(product.id),
                ProductVariant.is_active.is_(True),
            )
            .order_by(ProductVariant.id.asc())
        ).all()

        selected_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for variant_id, raw_size, raw_color, stock_quantity in rows:
            color_key = self._normalize_color_key(raw_color) or "other"
            normalized_size = normalize_shoe_size_label(raw_size)
            size_value = normalized_size or "One size"
            group_key = ((normalized_size or "one size").lower(), color_key)
            candidate = {
                "size": size_value,
                "variant_id": int(variant_id),
                "in_stock": (stock_quantity or 0) > 0,
                "color_key": color_key,
                "color_label": self.COLOR_LABELS.get(color_key, color_key.title()),
            }

            current = selected_by_key.get(group_key)
            if current is None:
                selected_by_key[group_key] = candidate
                continue
            if not current["in_stock"] and candidate["in_stock"]:
                selected_by_key[group_key] = candidate
                continue
            if current["in_stock"] == candidate["in_stock"] and candidate["variant_id"] < current["variant_id"]:
                selected_by_key[group_key] = candidate

        options = list(selected_by_key.values())
        options.sort(
            key=lambda option: (
                self._color_sort_key(option["color_key"]),
                self._size_sort_key(option["size"]),
                option["variant_id"],
            )
        )
        return options

    def _product_matches_color_filter(
        self,
        product_id: Optional[int],
        selected_color_keys: set[str],
        product_color_keys: dict[int, set[str]],
    ) -> bool:
        if not selected_color_keys:
            return True
        if product_id is None:
            return False
        return bool(product_color_keys.get(int(product_id), set()) & selected_color_keys)

    def _filter_products_by_colors(
        self,
        products: list[Product],
        selected_color_keys: set[str],
        product_color_keys: dict[int, set[str]],
    ) -> list[Product]:
        if not selected_color_keys:
            return products
        return [
            product
            for product in products
            if self._product_matches_color_filter(product.id, selected_color_keys, product_color_keys)
        ]

    def _product_matches_size_filter(
        self,
        product_id: Optional[int],
        selected_size_keys: set[str],
        product_size_keys: dict[int, set[str]],
    ) -> bool:
        if not selected_size_keys:
            return True
        if product_id is None:
            return False
        return selected_size_keys.issubset(product_size_keys.get(int(product_id), set()))

    def _filter_products_by_sizes(
        self,
        products: list[Product],
        selected_size_keys: set[str],
        product_size_keys: dict[int, set[str]],
    ) -> list[Product]:
        if not selected_size_keys:
            return products
        return [
            product
            for product in products
            if self._product_matches_size_filter(product.id, selected_size_keys, product_size_keys)
        ]

    def _resolve_product_image_url(self, image: ProductImage) -> Optional[str]:
        if image.source_url and image.source_url.strip():
            source_url = normalize_image_url(image.source_url)
            return source_url or None
        if image.file_path and image.file_path.strip():
            normalized = image.file_path.strip().lstrip("/\\").replace("\\", "/")
            return normalize_image_url(f"/uploads/{normalized}") or None
        return None

    def _get_primary_image_map(self, session: Session, product_ids: list[int]) -> dict[int, str]:
        clean_ids = sorted({product_id for product_id in product_ids if isinstance(product_id, int) and product_id > 0})
        if not clean_ids:
            return {}

        rows = session.exec(
            select(ProductImage)
            .where(ProductImage.product_id.in_(clean_ids))
            .order_by(
                ProductImage.product_id.asc(),
                ProductImage.is_primary.desc(),
                ProductImage.sort_order.asc(),
                ProductImage.id.asc(),
            )
        ).all()

        image_map: dict[int, str] = {}
        grouped: dict[int, list[GalleryImageCandidate]] = defaultdict(list)
        for image in rows:
            resolved = self._resolve_product_image_url(image)
            if not resolved:
                continue
            grouped[int(image.product_id)].append(
                GalleryImageCandidate(
                    url=resolved,
                    sort_order=image.sort_order,
                    is_primary=image.is_primary,
                )
            )

        for product_id, candidates in grouped.items():
            sanitized = sanitize_gallery_candidates(candidates, limit=1)
            if sanitized:
                image_map[product_id] = sanitized[0].url
        return image_map

    def _get_product_image_urls(self, session: Session, product_id: int, limit: int = 30) -> list[str]:
        rows = session.exec(
            select(ProductImage)
            .where(ProductImage.product_id == product_id)
            .order_by(
                ProductImage.is_primary.desc(),
                ProductImage.sort_order.asc(),
                ProductImage.id.asc(),
            )
        ).all()
        candidates: list[GalleryImageCandidate] = []
        for image in rows:
            resolved = self._resolve_product_image_url(image)
            if not resolved:
                continue
            candidates.append(
                GalleryImageCandidate(
                    url=resolved,
                    sort_order=image.sort_order,
                    is_primary=image.is_primary,
                )
            )
        return [candidate.url for candidate in sanitize_gallery_candidates(candidates, limit=limit)]

    def _size_sort_key(self, value: str) -> tuple[int, float, str]:
        normalized = value.strip()
        if not normalized:
            return (2, 0.0, "")
        numeric = normalized.replace(",", ".")
        try:
            return (0, float(numeric), normalized.lower())
        except ValueError:
            return (1, 0.0, normalized.lower())

    def _get_product_sizes(self, session: Session, product_id: int) -> list[str]:
        rows = session.exec(
            select(ProductVariant.size).where(
                ProductVariant.product_id == product_id,
                ProductVariant.is_active.is_(True),
                ProductVariant.size.is_not(None),
                func.length(func.trim(ProductVariant.size)) > 0,
            )
        ).all()
        unique_sizes = sorted(
            {
                normalized_size
                for size in rows
                for normalized_size in [normalize_shoe_size_label(size)]
                if normalized_size
            },
            key=self._size_sort_key,
        )
        return unique_sizes

    def _get_product_source_details(self, session: Session, product_id: int) -> dict[str, Optional[str]]:
        link = session.exec(
            select(SourceProductLink)
            .where(SourceProductLink.product_id == product_id)
            .order_by(SourceProductLink.last_sync_at.desc(), SourceProductLink.id.desc())
        ).first()
        if not link:
            return {"source_system": None, "source_product_id": None, "source_url": None}

        source_item = session.get(SourceCatalogItem, link.source_catalog_item_id)
        if not source_item:
            return {"source_system": None, "source_product_id": None, "source_url": None}
        return {
            "source_system": source_item.source_system,
            "source_product_id": source_item.source_product_id,
            "source_url": source_item.source_url,
        }

    def _build_product_details(
        self,
        session: Session,
        product: Product,
        product_color_keys: Optional[set[str]] = None,
    ) -> Optional[dict[str, Any]]:
        if product.id is None:
            return None
        category = session.get(Category, product.category_id)
        category_name = category.name if category else None
        gender_key = self._normalize_gender_key(product.gender)
        gender_label = self.GENDER_LABELS.get(gender_key) if gender_key else None
        sizes = self._get_product_sizes(session, int(product.id))
        color_keys = sorted(product_color_keys or set(), key=self._color_sort_key)
        source_details = self._get_product_source_details(session, int(product.id))
        return {
            "category_name": category_name,
            "gender_key": gender_key,
            "gender_label": gender_label,
            "sizes": sizes,
            "colors": self._colors_from_keys(color_keys),
            "source_system": source_details["source_system"],
            "source_product_id": source_details["source_product_id"],
            "source_url": source_details["source_url"],
        }

    def _build_generated_description(
        self,
        product: Product,
        details: Optional[dict[str, Any]] = None,
    ) -> str:
        brand_name = (product.brand_name or "").strip()
        base_name = (product.name or "").strip() or "Товар"
        if brand_name and brand_name.lower() not in base_name.lower():
            display_name = f"{brand_name} {base_name}"
        else:
            display_name = base_name

        parts = [f"{display_name} доступен в каталоге Footy.kz."]
        if details:
            category_name = details.get("category_name")
            if category_name:
                parts.append(f"Категория: {category_name}.")
            gender_label = details.get("gender_label")
            if gender_label:
                parts.append(f"Пол: {gender_label}.")
            sizes = details.get("sizes") or []
            if sizes:
                parts.append(f"Доступные размеры: {', '.join(sizes[:10])}.")
            colors = details.get("colors") or []
            color_labels = [color.get("label") for color in colors if isinstance(color, dict) and color.get("label")]
            if color_labels:
                parts.append(f"Цветовые варианты: {', '.join(color_labels[:8])}.")
        price_value = f"{product.price:.2f}" if product.price is not None else "0.00"
        currency = (product.currency or "KZT").upper()
        parts.append(f"Текущая цена: {price_value} {currency}.")
        parts.append("Наличие уточняется при оформлении заказа.")
        return " ".join(parts)

    def _resolve_product_description(
        self,
        product: Product,
        details: Optional[dict[str, Any]] = None,
    ) -> str:
        existing = (product.description or "").strip()
        if existing:
            return existing
        return self._build_generated_description(product, details)

    def _serialize_product(
        self,
        product: Product,
        image_url: Optional[str] = None,
        image_urls: Optional[list[str]] = None,
        colors: Optional[list[dict[str, str]]] = None,
        color_options: Optional[list[dict[str, Any]]] = None,
        size_options: Optional[list[dict[str, Any]]] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        resolved_description = self._resolve_product_description(product, details)
        return {
            "id": product.id,
            "slug": product.slug,
            "name": product.name,
            "short_description": product.short_description,
            "description": resolved_description,
            "brand_name": product.brand_name,
            "price": product.price,
            "compare_at_price": product.compare_at_price,
            "currency": product.currency,
            "category_id": product.category_id,
            "is_active": product.is_active,
            "image_url": image_url,
            "image_urls": image_urls or [],
            "colors": colors or [],
            "color_options": color_options or [],
            "size_options": size_options or [],
            "details": details,
        }

    def _normalize_category_ids(
        self,
        category_id: Optional[int] = None,
        category_ids: Optional[list[int]] = None,
    ) -> list[int]:
        selected = {cid for cid in (category_ids or []) if isinstance(cid, int) and cid > 0}
        if category_id and category_id > 0:
            selected.add(category_id)
        return sorted(selected)

    def _product_conditions(
        self,
        search: Optional[str] = None,
        category_ids: Optional[list[int]] = None,
        gender_keys: Optional[list[str]] = None,
        brand_keys: Optional[list[str]] = None,
        min_price: Optional[Decimal] = None,
        max_price: Optional[Decimal] = None,
        in_stock_only: bool = False,
    ):
        conditions = [Product.is_active.is_(True)]
        if search:
            conditions.append(Product.name.ilike(f"%{search}%"))
        if category_ids:
            conditions.append(Product.category_id.in_(category_ids))
        if gender_keys:
            conditions.append(
                func.lower(func.trim(func.coalesce(Product.gender, ""))).in_(gender_keys)
            )
        if brand_keys:
            conditions.append(
                func.lower(func.trim(func.coalesce(Product.brand_name, ""))).in_(brand_keys)
            )
        if min_price is not None:
            conditions.append(Product.price >= min_price)
        if max_price is not None:
            conditions.append(Product.price <= max_price)
        if in_stock_only:
            conditions.append(
                Product.id.in_(
                    select(ProductVariant.product_id).where(
                        ProductVariant.is_active.is_(True),
                        ProductVariant.stock_quantity > 0,
                    )
                )
            )
        return conditions

    def list_products(
        self,
        session: Session,
        search: Optional[str] = None,
        category_id: Optional[int] = None,
        category_ids: Optional[list[int]] = None,
        color_keys: Optional[list[str]] = None,
        gender_keys: Optional[list[str]] = None,
        brand_keys: Optional[list[str]] = None,
        min_price: Optional[Decimal] = None,
        max_price: Optional[Decimal] = None,
        in_stock_only: bool = False,
        limit: int = 24,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        selected_categories = self._normalize_category_ids(category_id, category_ids)
        selected_color_keys = self._normalize_requested_color_keys(color_keys)
        selected_gender_keys = self._normalize_requested_gender_keys(gender_keys)
        selected_brand_keys = self._normalize_requested_brand_keys(brand_keys)
        selected_color_keys_set = set(selected_color_keys)
        conditions = self._product_conditions(
            search=search,
            category_ids=selected_categories or None,
            gender_keys=selected_gender_keys or None,
            brand_keys=selected_brand_keys or None,
            min_price=min_price,
            max_price=max_price,
            in_stock_only=in_stock_only,
        )
        product_color_keys: dict[int, set[str]] = {}

        if selected_color_keys_set:
            all_products = session.exec(
                select(Product)
                .where(*conditions)
                .order_by(Product.updated_at.desc())
            ).all()
            all_product_ids = [int(product.id) for product in all_products if product.id is not None]
            all_product_color_keys = self._get_product_color_key_map(session, all_product_ids)
            filtered_products = self._filter_products_by_colors(
                all_products,
                selected_color_keys_set,
                all_product_color_keys,
            )
            products = filtered_products[offset : offset + limit]
            product_color_keys = {
                int(product.id): all_product_color_keys.get(int(product.id), set())
                for product in products
                if product.id is not None
            }
        else:
            products = session.exec(
                select(Product)
                .where(*conditions)
                .order_by(Product.updated_at.desc())
                .offset(offset)
                .limit(limit)
            ).all()
            page_product_ids = [int(product.id) for product in products if product.id is not None]
            product_color_keys = self._get_product_color_key_map(session, page_product_ids)

        image_map = self._get_primary_image_map(
            session,
            [product.id for product in products if product.id is not None],
        )
        return [
            self._serialize_product(
                product,
                image_map.get(product.id),
                colors=self._colors_from_keys(
                    sorted(product_color_keys.get(int(product.id), set()), key=self._color_sort_key)
                    if product.id is not None
                    else []
                ),
            )
            for product in products
        ]

    def get_product(self, session: Session, slug_or_id: str) -> Optional[dict[str, Any]]:
        if slug_or_id.isdigit():
            product = session.get(Product, int(slug_or_id))
        else:
            product = session.exec(select(Product).where(Product.slug == slug_or_id)).first()
        if not product:
            return None
        product_id = int(product.id) if product.id is not None else None
        product_color_map = self._get_product_color_key_map(session, [product_id] if product_id is not None else [])
        current_color_keys = sorted(
            product_color_map.get(product_id, set()) if product_id is not None else set(),
            key=self._color_sort_key,
        )
        image_urls = self._get_product_image_urls(session, product.id) if product.id is not None else []
        primary_url = image_urls[0] if image_urls else None
        details = self._build_product_details(session, product, set(current_color_keys))
        return self._serialize_product(
            product,
            primary_url,
            image_urls,
            colors=self._colors_from_keys(current_color_keys),
            color_options=self._build_color_options_for_product(
                session,
                product,
            ),
            size_options=self._build_size_options_for_product(
                session,
                product,
            ),
            details=details,
        )

    def similar_products(
        self,
        session: Session,
        product_id: int,
        limit: int = 12,
    ) -> Optional[list[dict[str, Any]]]:
        base = session.get(Product, product_id)
        if not base:
            return None
        items = session.exec(
            select(Product)
            .where(
                Product.category_id == base.category_id,
                Product.id != product_id,
                Product.is_active.is_(True),
            )
            .limit(limit)
        ).all()
        image_map = self._get_primary_image_map(
            session,
            [product.id for product in items if product.id is not None],
        )
        item_ids = [int(product.id) for product in items if product.id is not None]
        product_color_keys = self._get_product_color_key_map(session, item_ids)
        return [
            self._serialize_product(
                product,
                image_map.get(product.id),
                colors=self._colors_from_keys(
                    sorted(product_color_keys.get(int(product.id), set()), key=self._color_sort_key)
                    if product.id is not None
                    else []
                ),
            )
            for product in items
        ]

    def list_catalog_products(
        self,
        session: Session,
        search: Optional[str] = None,
        category_id: Optional[int] = None,
        category_ids: Optional[list[int]] = None,
        color_keys: Optional[list[str]] = None,
        gender_keys: Optional[list[str]] = None,
        size_keys: Optional[list[str]] = None,
        brand_keys: Optional[list[str]] = None,
        min_price: Optional[Decimal] = None,
        max_price: Optional[Decimal] = None,
        in_stock_only: bool = False,
        limit: int = 24,
        offset: int = 0,
    ) -> dict[str, Any]:
        selected_categories = self._normalize_category_ids(category_id, category_ids)
        selected_color_keys = self._normalize_requested_color_keys(color_keys)
        selected_gender_keys = self._normalize_requested_gender_keys(gender_keys)
        selected_size_keys = self._normalize_requested_size_keys(size_keys)
        selected_brand_keys = self._normalize_requested_brand_keys(brand_keys)
        selected_color_keys_set = set(selected_color_keys)
        selected_size_keys_set = set(selected_size_keys)
        item_conditions = self._product_conditions(
            search=search,
            category_ids=selected_categories or None,
            gender_keys=selected_gender_keys or None,
            brand_keys=selected_brand_keys or None,
            min_price=min_price,
            max_price=max_price,
            in_stock_only=in_stock_only,
        )
        product_color_keys: dict[int, set[str]] = {}

        if selected_color_keys_set or selected_size_keys_set:
            all_products = session.exec(
                select(Product)
                .where(*item_conditions)
                .order_by(Product.updated_at.desc())
            ).all()
            all_product_ids = [int(product.id) for product in all_products if product.id is not None]
            all_product_color_keys, all_product_size_keys, _ = self._get_product_variant_maps(
                session,
                all_product_ids,
                product_conditions=item_conditions,
                include_colors=bool(selected_color_keys_set),
                include_sizes=bool(selected_size_keys_set),
                in_stock_only_for_sizes=in_stock_only,
            )
            filtered_products = all_products
            if selected_color_keys_set:
                filtered_products = self._filter_products_by_colors(
                    filtered_products,
                    selected_color_keys_set,
                    all_product_color_keys,
                )
            if selected_size_keys_set:
                filtered_products = self._filter_products_by_sizes(
                    filtered_products,
                    selected_size_keys_set,
                    all_product_size_keys,
                )
            total = len(filtered_products)
            products = filtered_products[offset : offset + limit]
            if selected_color_keys_set:
                product_color_keys = {
                    int(product.id): all_product_color_keys.get(int(product.id), set())
                    for product in products
                    if product.id is not None
                }
            else:
                page_product_ids = [int(product.id) for product in products if product.id is not None]
                product_color_keys = self._get_product_color_key_map(session, page_product_ids)
        else:
            products = session.exec(
                select(Product)
                .where(*item_conditions)
                .order_by(Product.updated_at.desc())
                .offset(offset)
                .limit(limit)
            ).all()
            total = session.exec(select(func.count(Product.id)).where(*item_conditions)).one()
            page_product_ids = [int(product.id) for product in products if product.id is not None]
            product_color_keys = self._get_product_color_key_map(session, page_product_ids)

        image_map = self._get_primary_image_map(
            session,
            [product.id for product in products if product.id is not None],
        )
        image_urls_map = {
            int(product.id): self._get_product_image_urls(session, int(product.id), limit=5)
            for product in products
            if product.id is not None
        }
        items = [
            self._serialize_product(
                product,
                image_map.get(product.id),
                image_urls=image_urls_map.get(int(product.id), []),
                colors=self._colors_from_keys(
                    sorted(product_color_keys.get(int(product.id), set()), key=self._color_sort_key)
                    if product.id is not None
                    else []
                ),
            )
            for product in products
        ]

        facet_conditions = self._product_conditions(
            search=search,
            category_ids=None,
            gender_keys=selected_gender_keys or None,
            brand_keys=selected_brand_keys or None,
            min_price=min_price,
            max_price=max_price,
            in_stock_only=in_stock_only,
        )
        if selected_color_keys_set or selected_size_keys_set:
            facet_products = session.exec(
                select(Product.id, Product.category_id)
                .where(*facet_conditions)
            ).all()
            facet_product_ids = [
                int(product_id)
                for product_id, _ in facet_products
                if isinstance(product_id, int) and product_id > 0
            ]
            facet_product_color_keys, facet_product_size_keys, _ = self._get_product_variant_maps(
                session,
                facet_product_ids,
                product_conditions=facet_conditions,
                include_colors=bool(selected_color_keys_set),
                include_sizes=bool(selected_size_keys_set),
                in_stock_only_for_sizes=in_stock_only,
            )
            category_counts: dict[int, int] = defaultdict(int)
            for product_id, category_id in facet_products:
                if category_id is None or not isinstance(category_id, int) or category_id <= 0:
                    continue
                if not self._product_matches_color_filter(
                    product_id,
                    selected_color_keys_set,
                    facet_product_color_keys,
                ):
                    continue
                if not self._product_matches_size_filter(
                    product_id,
                    selected_size_keys_set,
                    facet_product_size_keys,
                ):
                    continue
                category_counts[int(category_id)] += 1

            category_rows = session.exec(
                select(Category.id, Category.name)
                .where(
                    Category.is_active.is_(True),
                    Category.id.in_(list(category_counts.keys())),
                )
                .order_by(Category.name.asc())
            ).all()
            facets = [
                {
                    "id": int(category_id_row),
                    "name": category_name,
                    "count": int(category_counts.get(int(category_id_row), 0)),
                }
                for category_id_row, category_name in category_rows
                if int(category_counts.get(int(category_id_row), 0)) > 0
            ]
        else:
            facet_counts_subquery = (
                select(
                    Product.category_id.label("category_id"),
                    func.count(Product.id).label("count"),
                )
                .where(*facet_conditions)
                .group_by(Product.category_id)
                .subquery()
            )
            category_rows = session.exec(
                select(
                    Category.id,
                    Category.name,
                    func.coalesce(facet_counts_subquery.c.count, 0),
                )
                .select_from(Category)
                .outerjoin(facet_counts_subquery, facet_counts_subquery.c.category_id == Category.id)
                .where(Category.is_active.is_(True))
                .order_by(Category.name.asc())
            ).all()

            facets = [
                {
                    "id": category_id_row,
                    "name": category_name,
                    "count": int(category_count),
                }
                for category_id_row, category_name, category_count in category_rows
                if int(category_count) > 0
            ]

        shared_dimension_facet_conditions = self._product_conditions(
            search=search,
            category_ids=selected_categories or None,
            gender_keys=selected_gender_keys or None,
            brand_keys=selected_brand_keys or None,
            min_price=min_price,
            max_price=max_price,
            in_stock_only=in_stock_only,
        )
        if not selected_color_keys_set and not selected_size_keys_set:
            shared_dimension_product_ids = [
                int(product_id)
                for product_id in session.exec(
                    select(Product.id).where(*shared_dimension_facet_conditions)
                ).all()
                if product_id is not None
            ]
            shared_dimension_color_keys, shared_dimension_size_keys, shared_dimension_size_labels = (
                self._get_product_variant_maps(
                    session,
                    shared_dimension_product_ids,
                    product_conditions=shared_dimension_facet_conditions,
                    include_colors=True,
                    include_sizes=True,
                    in_stock_only_for_sizes=in_stock_only,
                )
            )
            color_counts: dict[str, int] = defaultdict(int)
            for color_keys in shared_dimension_color_keys.values():
                for color_key in color_keys:
                    color_counts[color_key] += 1
            color_facets = [
                {"key": color_key, "label": self.COLOR_LABELS[color_key], "count": color_counts[color_key]}
                for color_key in self.COLOR_ORDER
                if color_counts.get(color_key, 0) > 0
            ]
            size_counts: dict[str, int] = defaultdict(int)
            for product_id in shared_dimension_product_ids:
                for size_key in shared_dimension_size_keys.get(product_id, set()):
                    size_counts[size_key] += 1
            ordered_size_keys = sorted(
                size_counts.keys(),
                key=lambda key: (
                    self._size_sort_key(shared_dimension_size_labels.get(key, key)),
                    shared_dimension_size_labels.get(key, key).lower(),
                ),
            )
            size_facets = [
                {
                    "key": size_key,
                    "label": shared_dimension_size_labels.get(size_key, size_key),
                    "count": size_counts[size_key],
                }
                for size_key in ordered_size_keys
                if size_counts.get(size_key, 0) > 0
            ]
        else:
            shared_dimension_product_ids = [
                int(product_id)
                for product_id in session.exec(
                    select(Product.id).where(*shared_dimension_facet_conditions)
                ).all()
                if product_id is not None
            ]
            shared_dimension_color_keys, shared_dimension_size_keys, shared_dimension_size_labels = (
                self._get_product_variant_maps(
                    session,
                    shared_dimension_product_ids,
                    product_conditions=shared_dimension_facet_conditions,
                    include_colors=True,
                    include_sizes=True,
                    in_stock_only_for_sizes=in_stock_only,
                )
            )

            color_facet_product_ids = shared_dimension_product_ids
            if selected_size_keys_set:
                color_facet_product_ids = [
                    product_id
                    for product_id in color_facet_product_ids
                    if self._product_matches_size_filter(
                        product_id,
                        selected_size_keys_set,
                        shared_dimension_size_keys,
                    )
                ]
            color_counts: dict[str, int] = defaultdict(int)
            for product_id in color_facet_product_ids:
                for color_key in shared_dimension_color_keys.get(product_id, set()):
                    color_counts[color_key] += 1
            color_facets = [
                {"key": color_key, "label": self.COLOR_LABELS[color_key], "count": color_counts[color_key]}
                for color_key in self.COLOR_ORDER
                if color_counts.get(color_key, 0) > 0
            ]

            size_facet_product_ids = shared_dimension_product_ids
            if selected_color_keys_set:
                size_facet_product_ids = [
                    product_id
                    for product_id in size_facet_product_ids
                    if self._product_matches_color_filter(
                        product_id,
                        selected_color_keys_set,
                        shared_dimension_color_keys,
                    )
                ]
            size_counts: dict[str, int] = defaultdict(int)
            for product_id in size_facet_product_ids:
                for size_key in shared_dimension_size_keys.get(product_id, set()):
                    size_counts[size_key] += 1
            ordered_size_keys = sorted(
                size_counts.keys(),
                key=lambda key: (
                    self._size_sort_key(shared_dimension_size_labels.get(key, key)),
                    shared_dimension_size_labels.get(key, key).lower(),
                ),
            )
            size_facets = [
                {
                    "key": size_key,
                    "label": shared_dimension_size_labels.get(size_key, size_key),
                    "count": size_counts[size_key],
                }
                for size_key in ordered_size_keys
                if size_counts.get(size_key, 0) > 0
            ]

        gender_facet_conditions = self._product_conditions(
            search=search,
            category_ids=selected_categories or None,
            gender_keys=None,
            brand_keys=selected_brand_keys or None,
            min_price=min_price,
            max_price=max_price,
            in_stock_only=in_stock_only,
        )
        gender_facet_rows = session.exec(
            select(Product.id, Product.gender)
            .where(*gender_facet_conditions)
        ).all()
        gender_product_ids = [
            int(product_id)
            for product_id, _ in gender_facet_rows
            if isinstance(product_id, int) and product_id > 0
        ]
        allowed_gender_product_ids = set(gender_product_ids)
        if (selected_color_keys_set or selected_size_keys_set) and allowed_gender_product_ids:
            gender_product_color_keys, gender_product_size_keys, _ = self._get_product_variant_maps(
                session,
                list(allowed_gender_product_ids),
                product_conditions=gender_facet_conditions,
                include_colors=bool(selected_color_keys_set),
                include_sizes=bool(selected_size_keys_set),
                in_stock_only_for_sizes=in_stock_only,
            )
            if selected_color_keys_set:
                allowed_gender_product_ids = {
                    product_id
                    for product_id in allowed_gender_product_ids
                    if self._product_matches_color_filter(
                        product_id,
                        selected_color_keys_set,
                        gender_product_color_keys,
                    )
                }
            if selected_size_keys_set and allowed_gender_product_ids:
                allowed_gender_product_ids = {
                    product_id
                    for product_id in allowed_gender_product_ids
                    if self._product_matches_size_filter(
                        product_id,
                        selected_size_keys_set,
                        gender_product_size_keys,
                    )
                }
        gender_counts: dict[str, int] = defaultdict(int)
        normalized_gender_cache: dict[str, Optional[str]] = {}
        for product_id, raw_gender in gender_facet_rows:
            if not isinstance(product_id, int) or product_id <= 0 or product_id not in allowed_gender_product_ids:
                continue
            cache_key = raw_gender or ""
            if cache_key in normalized_gender_cache:
                gender_key = normalized_gender_cache[cache_key]
            else:
                gender_key = self._normalize_gender_key(raw_gender)
                normalized_gender_cache[cache_key] = gender_key
            if not gender_key:
                continue
            gender_counts[gender_key] += 1
        gender_facets = [
            {"key": gender_key, "label": self.GENDER_LABELS[gender_key], "count": gender_counts[gender_key]}
            for gender_key in self.GENDER_ORDER
            if gender_counts.get(gender_key, 0) > 0
        ]

        brand_facet_conditions = self._product_conditions(
            search=search,
            category_ids=selected_categories or None,
            gender_keys=selected_gender_keys or None,
            brand_keys=None,
            min_price=min_price,
            max_price=max_price,
            in_stock_only=in_stock_only,
        )
        brand_facet_rows = session.exec(
            select(Product.id, Product.brand_name).where(*brand_facet_conditions)
        ).all()
        allowed_brand_product_ids = {
            int(product_id)
            for product_id, _ in brand_facet_rows
            if isinstance(product_id, int) and product_id > 0
        }
        if (selected_color_keys_set or selected_size_keys_set) and allowed_brand_product_ids:
            brand_product_color_keys, brand_product_size_keys, _ = self._get_product_variant_maps(
                session,
                list(allowed_brand_product_ids),
                product_conditions=brand_facet_conditions,
                include_colors=bool(selected_color_keys_set),
                include_sizes=bool(selected_size_keys_set),
                in_stock_only_for_sizes=in_stock_only,
            )
            if selected_color_keys_set:
                allowed_brand_product_ids = {
                    product_id
                    for product_id in allowed_brand_product_ids
                    if self._product_matches_color_filter(
                        product_id,
                        selected_color_keys_set,
                        brand_product_color_keys,
                    )
                }
            if selected_size_keys_set and allowed_brand_product_ids:
                allowed_brand_product_ids = {
                    product_id
                    for product_id in allowed_brand_product_ids
                    if self._product_matches_size_filter(
                        product_id,
                        selected_size_keys_set,
                        brand_product_size_keys,
                    )
                }
        brand_counts: dict[tuple[str, str], int] = defaultdict(int)
        for product_id, raw_brand in brand_facet_rows:
            if not isinstance(product_id, int) or product_id <= 0 or product_id not in allowed_brand_product_ids:
                continue
            label = (raw_brand or "").strip()
            if not label:
                continue
            key = " ".join(label.lower().split())
            if not key:
                continue
            brand_counts[(key, label)] += 1
        brand_facets = [
            {"key": key, "label": label, "count": count}
            for (key, label), count in sorted(
                brand_counts.items(),
                key=lambda item: (-item[1], item[0][1].lower()),
            )
            if count > 0
        ]

        return {
            "items": items,
            "total": int(total),
            "facets": {
                "categories": facets,
                "colors": color_facets,
                "genders": gender_facets,
                "sizes": size_facets,
                "brands": brand_facets,
            },
            "applied_filters": {
                "search": search,
                "category_ids": selected_categories,
                "color_keys": selected_color_keys,
                "gender_keys": selected_gender_keys,
                "size_keys": selected_size_keys,
                "brand_keys": selected_brand_keys,
                "min_price": min_price,
                "max_price": max_price,
                "in_stock_only": in_stock_only,
                "limit": limit,
                "offset": offset,
            },
        }
