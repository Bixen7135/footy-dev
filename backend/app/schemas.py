from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshTokenResponse(TokenResponse):
    pass


class UserRegisterIn(BaseModel):
    email: str
    password: str
    full_name: str
    phone: Optional[str] = None


class UserLoginIn(BaseModel):
    email: str
    password: str


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    full_name: str
    phone: Optional[str]
    role: str


class SourceItemIn(BaseModel):
    source_system: str
    source_product_id: str
    source_url: str
    title: str
    price: Optional[Decimal] = None
    unavailable: bool = False
    currency: Optional[str] = None
    category_path: str
    images: list[str]
    last_seen_at: datetime
    description: Optional[str] = None
    brand: Optional[str] = None
    attributes: Optional[dict[str, Any]] = None
    sizes: Optional[list[str]] = None
    colors: Optional[list[str]] = None
    stock: Optional[dict[str, int]] = None
    breadcrumbs: Optional[list[str]] = None
    compare_at_price: Optional[Decimal] = None
    gender: Optional[str] = None
    product_type: Optional[str] = None
    material: Optional[str] = None
    season: Optional[str] = None
    tags: Optional[list[str]] = None


class ImportRunIn(BaseModel):
    source_system: str
    items: list[SourceItemIn]


class ImportRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_system: str
    status: str
    run_type: str
    items_seen_count: int
    items_created_count: int
    items_updated_count: int
    items_failed_count: int
    started_at: datetime
    finished_at: Optional[datetime]


class MappingRequest(BaseModel):
    source_system: str
    source_category_key: Optional[str] = None
    internal_category_id: Optional[int] = None
    source_brand_key: Optional[str] = None
    internal_brand_name: Optional[str] = None
    attributes_map: Optional[dict[str, str]] = None


class ProductVariantIn(BaseModel):
    sku: str
    size: Optional[str] = None
    color: Optional[str] = None
    stock_quantity: int = Field(default=0, ge=0)
    is_active: bool = True


class ProductImageIn(BaseModel):
    source_url: Optional[str] = None
    file_path: Optional[str] = None
    alt_text: str = ""
    sort_order: int = 0
    is_primary: bool = False
    storage_mode: str = "external_cdn"


class MediaUploadOut(BaseModel):
    ok: bool = True
    storage_mode: str
    source_url: Optional[str] = None
    file_path: Optional[str] = None
    content_type: Optional[str] = None
    size_bytes: Optional[int] = None


class ProductCreateIn(BaseModel):
    slug: str
    name: str
    short_description: Optional[str] = None
    description: Optional[str] = None
    brand_name: Optional[str] = None
    price: Decimal
    compare_at_price: Optional[Decimal] = None
    currency: str = "KZT"
    category_id: int
    is_active: bool = False


class ProductPatchIn(BaseModel):
    name: Optional[str] = None
    short_description: Optional[str] = None
    description: Optional[str] = None
    brand_name: Optional[str] = None
    price: Optional[Decimal] = None
    compare_at_price: Optional[Decimal] = None
    currency: Optional[str] = None
    category_id: Optional[int] = None
    is_active: Optional[bool] = None


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    short_description: Optional[str]
    description: Optional[str]
    brand_name: Optional[str]
    price: Decimal
    compare_at_price: Optional[Decimal]
    currency: str
    category_id: int
    is_active: bool
    image_url: Optional[str] = None
    image_urls: list[str] = Field(default_factory=list)
    colors: list["ProductColorOut"] = Field(default_factory=list)
    color_options: list["ProductColorOptionOut"] = Field(default_factory=list)
    size_options: list["ProductSizeOptionOut"] = Field(default_factory=list)
    details: Optional["ProductDetailsOut"] = None


class ProductColorOut(BaseModel):
    key: str
    label: str


class ProductColorOptionOut(BaseModel):
    key: str
    label: str
    product_id: int
    variant_id: Optional[int] = None
    slug: str
    image_url: Optional[str] = None
    is_current: bool = False


class ProductSizeOptionOut(BaseModel):
    size: str
    variant_id: int
    in_stock: bool
    color_key: str
    color_label: str


class ProductDetailsOut(BaseModel):
    category_name: Optional[str] = None
    gender_key: Optional[str] = None
    gender_label: Optional[str] = None
    sizes: list[str] = Field(default_factory=list)
    colors: list["ProductColorOut"] = Field(default_factory=list)
    source_system: Optional[str] = None
    source_product_id: Optional[str] = None
    source_url: Optional[str] = None


class CatalogCategoryFacetOut(BaseModel):
    id: int
    name: str
    count: int


class CatalogColorFacetOut(BaseModel):
    key: str
    label: str
    count: int


class CatalogGenderFacetOut(BaseModel):
    key: str
    label: str
    count: int


class CatalogSizeFacetOut(BaseModel):
    key: str
    label: str
    count: int


class CatalogBrandFacetOut(BaseModel):
    key: str
    label: str
    count: int


class CatalogFacetsOut(BaseModel):
    categories: list[CatalogCategoryFacetOut] = Field(default_factory=list)
    colors: list[CatalogColorFacetOut] = Field(default_factory=list)
    genders: list[CatalogGenderFacetOut] = Field(default_factory=list)
    sizes: list[CatalogSizeFacetOut] = Field(default_factory=list)
    brands: list[CatalogBrandFacetOut] = Field(default_factory=list)


class CatalogAppliedFiltersOut(BaseModel):
    search: Optional[str] = None
    category_ids: list[int] = Field(default_factory=list)
    color_keys: list[str] = Field(default_factory=list)
    gender_keys: list[str] = Field(default_factory=list)
    size_keys: list[str] = Field(default_factory=list)
    brand_keys: list[str] = Field(default_factory=list)
    min_price: Optional[Decimal] = None
    max_price: Optional[Decimal] = None
    in_stock_only: bool = False
    limit: int
    offset: int


class CatalogProductsOut(BaseModel):
    items: list[ProductOut]
    total: int
    facets: CatalogFacetsOut
    applied_filters: CatalogAppliedFiltersOut


class CartItemIn(BaseModel):
    product_id: int
    variant_id: Optional[int] = None
    quantity: int = Field(ge=1)


class CartItemPatchIn(BaseModel):
    quantity: int = Field(ge=1)


class CartItemOut(BaseModel):
    id: int
    product_id: int
    variant_id: Optional[int]
    quantity: int
    unit_price_snapshot: Decimal


class CartOut(BaseModel):
    id: int
    status: str
    items: list[CartItemOut]
    subtotal: Decimal


class WishlistIn(BaseModel):
    product_id: int
    variant_id: Optional[int] = None


class OrderAddressIn(BaseModel):
    full_name: str
    email: str
    phone: str
    address_line1: str
    address_line2: Optional[str] = None
    city: str
    postal_code: str
    country: str


class OrderLineIn(BaseModel):
    product_id: int
    variant_id: Optional[int] = None
    quantity: int = Field(ge=1)


class OrderCreateIn(BaseModel):
    idempotency_key: str
    notes: Optional[str] = None
    shipping: OrderAddressIn
    items: list[OrderLineIn]


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_number: str
    status: str
    total_amount: Decimal
    created_at: datetime


class EventIn(BaseModel):
    event_type: str
    user_id: Optional[int] = None
    anonymous_id: Optional[str] = None
    session_id: str
    product_id: Optional[int] = None
    variant_id: Optional[int] = None
    category_id: Optional[int] = None
    source_page: Optional[str] = None
    page_url: Optional[str] = None
    rank_position: Optional[int] = None
    dwell_ms: Optional[int] = None
    recommendation_slot: Optional[str] = None
    recommendation_request_id: Optional[str] = None
    metadata_json: Optional[dict[str, Any]] = None
    created_at: Optional[datetime] = None


class EventBatchIn(BaseModel):
    events: list[EventIn]


class RecommendationRequest(BaseModel):
    user_id: Optional[int] = None
    anonymous_id: Optional[str] = None
    context: str
    current_product_id: Optional[int] = None
    limit: int = Field(default=12, ge=1, le=50)


class RecommendationItem(BaseModel):
    product_id: int
    score: float
    source: str


class RecommendationResponse(BaseModel):
    request_id: str
    items: list[RecommendationItem]


class RecommendationFeatureVector(BaseModel):
    product_id: int
    features: dict[str, float] = Field(default_factory=dict)


class RankerModelMeta(BaseModel):
    is_ready: bool
    model_version: Optional[str] = None
    trained_at: Optional[str] = None
    rows: int = 0
    features: list[str] = Field(default_factory=list)
    train_rmse: Optional[float] = None
    fallback_reason: Optional[str] = None
    model_path: Optional[str] = None
    dataset_path: Optional[str] = None


class BootstrapRowError(BaseModel):
    file: str
    row: Optional[int] = None
    error: str


class ExportBootstrapReport(BaseModel):
    ok: bool
    started_at: str
    finished_at: Optional[str] = None
    exports_dir: str
    rows: dict[str, int] = Field(default_factory=dict)
    created: dict[str, int] = Field(default_factory=dict)
    updated: dict[str, int] = Field(default_factory=dict)
    skipped: dict[str, int] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    reconciliation: dict[str, int] = Field(default_factory=dict)
    report_path: Optional[str] = None


class SyncDecision(BaseModel):
    source_item_id: int
    product_id: Optional[int] = None
    action: str
    reason: Optional[str] = None


class PublicationDecision(BaseModel):
    publishable: bool
    reason: Optional[str] = None


class NormalizedItem(BaseModel):
    source_item_id: int
    title: str
    brand: Optional[str]
    currency: str
    price: Optional[Decimal]
    category_key: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    images: list[str] = Field(default_factory=list)


class OrderSnapshot(BaseModel):
    product_id: int
    variant_id: Optional[int]
    product_name_snapshot: str
    variant_snapshot_json: Optional[dict[str, Any]]
    unit_price_snapshot: Decimal
    quantity: int
    line_total: Decimal


class SourceSystemIn(BaseModel):
    source_system: str
    base_url: str
    allowed_roots_json: list[str] = Field(default_factory=list)
    rate_limit_rps: Optional[float] = None
    concurrency: Optional[int] = None
    headers_json: Optional[dict[str, Any]] = None
    storage_mode_default: str = "external_cdn"
    is_active: bool = True


class SourceSystemPatchIn(BaseModel):
    base_url: Optional[str] = None
    allowed_roots_json: Optional[list[str]] = None
    rate_limit_rps: Optional[float] = None
    concurrency: Optional[int] = None
    headers_json: Optional[dict[str, Any]] = None
    storage_mode_default: Optional[str] = None
    is_active: Optional[bool] = None


class CMSPageIn(BaseModel):
    slug: str
    title: str
    content_markdown: str
    is_published: bool = True


class CMSPagePatchIn(BaseModel):
    title: Optional[str] = None
    content_markdown: Optional[str] = None
    is_published: Optional[bool] = None
