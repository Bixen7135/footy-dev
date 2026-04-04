from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

from sqlalchemy import JSON, Column, Index, Numeric, UniqueConstraint
from sqlmodel import Field, SQLModel


class UserRole(str, Enum):
    CUSTOMER = "customer"
    ADMIN = "admin"


class ProductStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPECT_MISSING = "suspect_missing"
    STALE = "stale"


class SyncStatus(str, Enum):
    NEW = "new"
    MAPPED = "mapped"
    SYNCED = "synced"
    STALE = "stale"
    FAILED = "failed"


class RunType(str, Enum):
    IMPORT = "import"
    NORMALIZE = "normalize"
    SYNC = "sync"
    MEDIA = "media"


class RunStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"


class CartStatus(str, Enum):
    ACTIVE = "active"
    CONVERTED = "converted"
    ABANDONED = "abandoned"


class StorageMode(str, Enum):
    EXTERNAL_CDN = "external_cdn"
    LOCAL_COPY = "local_copy"
    UPLOADED = "uploaded"


class TimestampedModel(SQLModel):
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class SourceSystemConfig(TimestampedModel, table=True):
    __tablename__ = "source_system_configs"

    id: Optional[int] = Field(default=None, primary_key=True)
    source_system: str = Field(index=True, unique=True)
    base_url: str
    allowed_roots_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    rate_limit_rps: Optional[float] = None
    concurrency: Optional[int] = None
    headers_json: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    storage_mode_default: StorageMode = Field(default=StorageMode.EXTERNAL_CDN)
    is_active: bool = True


class SourceCatalogItem(TimestampedModel, table=True):
    __tablename__ = "source_catalog_items"
    __table_args__ = (
        UniqueConstraint("source_system", "source_product_id", name="uq_source_item_key"),
        Index("ix_source_item_last_seen", "last_seen_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    source_system: str = Field(index=True)
    source_product_id: str = Field(index=True)
    source_url: str
    source_title: str
    source_description: Optional[str] = None
    source_brand: Optional[str] = None
    source_category_path: str
    source_attributes_json: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    source_price: Optional[Decimal] = Field(
        default=None, sa_column=Column(Numeric(12, 2), nullable=True)
    )
    source_compare_at_price: Optional[Decimal] = Field(
        default=None, sa_column=Column(Numeric(12, 2), nullable=True)
    )
    source_currency: Optional[str] = None
    source_stock_json: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    source_media_json: Optional[list] = Field(default=None, sa_column=Column(JSON))
    normalized_status: str = Field(default="pending", index=True)
    miss_count: int = Field(default=0)
    last_seen_at: datetime = Field(default_factory=datetime.utcnow)


class SourceProductLink(TimestampedModel, table=True):
    __tablename__ = "source_product_links"
    __table_args__ = (UniqueConstraint("source_catalog_item_id", name="uq_source_product_link_item"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    source_catalog_item_id: int = Field(foreign_key="source_catalog_items.id", index=True)
    product_id: int = Field(foreign_key="products.id", index=True)
    sync_status: SyncStatus = Field(default=SyncStatus.NEW)
    manual_override: bool = False
    last_sync_at: Optional[datetime] = None
    error_message: Optional[str] = None


class SourceCategoryMap(TimestampedModel, table=True):
    __tablename__ = "source_categories_map"
    __table_args__ = (
        UniqueConstraint("source_system", "source_category_key", name="uq_source_category_map"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    source_system: str = Field(index=True)
    source_category_key: str = Field(index=True)
    internal_category_id: int = Field(foreign_key="categories.id")
    confidence_score: Optional[float] = None


class SourceBrandMap(TimestampedModel, table=True):
    __tablename__ = "source_brands_map"
    __table_args__ = (
        UniqueConstraint("source_system", "source_brand_key", name="uq_source_brand_map"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    source_system: str = Field(index=True)
    source_brand_key: str = Field(index=True)
    internal_brand_name: str
    confidence_score: Optional[float] = None


class SourceAttributeMap(TimestampedModel, table=True):
    __tablename__ = "source_attributes_map"
    __table_args__ = (
        UniqueConstraint(
            "source_system",
            "source_attribute_key",
            "source_attribute_value",
            name="uq_source_attribute_map",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    source_system: str = Field(index=True)
    source_attribute_key: str
    source_attribute_value: str
    internal_attribute_name: str
    internal_attribute_value: str
    confidence_score: Optional[float] = None


class ImportRun(TimestampedModel, table=True):
    __tablename__ = "import_runs"
    __table_args__ = (Index("ix_import_run_status", "status", "started_at"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    source_system: str = Field(index=True)
    run_type: RunType = Field(default=RunType.IMPORT)
    status: RunStatus = Field(default=RunStatus.RUNNING)
    started_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None
    page_cursor: Optional[str] = None
    last_success_url: Optional[str] = None
    resume_token: Optional[str] = None
    retry_count: int = 0
    items_seen_count: int = 0
    items_created_count: int = 0
    items_updated_count: int = 0
    items_failed_count: int = 0
    error_summary: Optional[str] = None


class Category(TimestampedModel, table=True):
    __tablename__ = "categories"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    slug: str = Field(unique=True, index=True)
    parent_id: Optional[int] = Field(default=None, foreign_key="categories.id")
    is_active: bool = True
    sort_order: int = 0


class Tag(TimestampedModel, table=True):
    __tablename__ = "tags"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    slug: str = Field(unique=True, index=True)
    is_active: bool = True


class Product(TimestampedModel, table=True):
    __tablename__ = "products"
    __table_args__ = (Index("ix_product_status", "is_active", "updated_at"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    slug: str = Field(unique=True, index=True)
    name: str
    short_description: Optional[str] = None
    description: Optional[str] = None
    brand_name: Optional[str] = None
    price: Decimal = Field(sa_column=Column(Numeric(12, 2), nullable=False))
    compare_at_price: Optional[Decimal] = Field(
        default=None, sa_column=Column(Numeric(12, 2), nullable=True)
    )
    currency: str = Field(default="KZT")
    category_id: int = Field(foreign_key="categories.id", index=True)
    gender: Optional[str] = None
    product_type: Optional[str] = None
    material: Optional[str] = None
    season: Optional[str] = None
    is_active: bool = False
    is_featured: bool = False
    source_managed: bool = True
    status: ProductStatus = Field(default=ProductStatus.INACTIVE)
    admin_overrides_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))


class ProductImage(SQLModel, table=True):
    __tablename__ = "product_images"

    id: Optional[int] = Field(default=None, primary_key=True)
    product_id: int = Field(foreign_key="products.id", index=True)
    storage_mode: StorageMode = Field(default=StorageMode.EXTERNAL_CDN)
    source_url: Optional[str] = None
    file_path: Optional[str] = None
    alt_text: str = Field(default="")
    sort_order: int = 0
    is_primary: bool = False
    cdn_asset_id: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ProductTag(SQLModel, table=True):
    __tablename__ = "product_tags"
    __table_args__ = (UniqueConstraint("product_id", "tag_id", name="uq_product_tag"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    product_id: int = Field(foreign_key="products.id", index=True)
    tag_id: int = Field(foreign_key="tags.id", index=True)


class ProductVariant(TimestampedModel, table=True):
    __tablename__ = "product_variants"
    __table_args__ = (UniqueConstraint("sku", name="uq_variant_sku"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    product_id: int = Field(foreign_key="products.id", index=True)
    sku: str = Field(index=True)
    size: Optional[str] = None
    color: Optional[str] = None
    stock_quantity: int = 0
    is_active: bool = True


class User(TimestampedModel, table=True):
    __tablename__ = "users"

    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True)
    password_hash: str
    full_name: str
    phone: Optional[str] = None
    role: UserRole = Field(default=UserRole.CUSTOMER)
    is_active: bool = True


class UserAddress(TimestampedModel, table=True):
    __tablename__ = "user_addresses"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    full_name: str
    phone: str
    address_line1: str
    address_line2: Optional[str] = None
    city: str
    postal_code: str
    country: str
    is_default: bool = False


class Cart(TimestampedModel, table=True):
    __tablename__ = "carts"
    __table_args__ = (Index("ix_cart_actor", "user_id", "anonymous_id", "status"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, foreign_key="users.id", index=True)
    anonymous_id: Optional[str] = Field(default=None, index=True)
    status: CartStatus = Field(default=CartStatus.ACTIVE)
    merged_into_cart_id: Optional[int] = None


class CartItem(TimestampedModel, table=True):
    __tablename__ = "cart_items"
    __table_args__ = (
        UniqueConstraint("cart_id", "product_id", "variant_id", name="uq_cart_item_line"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    cart_id: int = Field(foreign_key="carts.id", index=True)
    product_id: int = Field(foreign_key="products.id", index=True)
    variant_id: Optional[int] = Field(default=None, foreign_key="product_variants.id", index=True)
    quantity: int = Field(default=1, ge=1)
    unit_price_snapshot: Decimal = Field(sa_column=Column(Numeric(12, 2), nullable=False))


class WishlistItem(SQLModel, table=True):
    __tablename__ = "wishlist_items"
    __table_args__ = (
        UniqueConstraint("user_id", "product_id", "variant_id", name="uq_wishlist_line"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    product_id: int = Field(foreign_key="products.id", index=True)
    variant_id: Optional[int] = Field(default=None, foreign_key="product_variants.id", index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Order(TimestampedModel, table=True):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("order_number", name="uq_order_number"),
        UniqueConstraint("idempotency_key", name="uq_order_idempotency"),
        Index("ix_order_user", "user_id", "created_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    order_number: str
    user_id: Optional[int] = Field(default=None, foreign_key="users.id", index=True)
    email: str
    full_name: str
    phone: str
    address_line1: str
    address_line2: Optional[str] = None
    city: str
    postal_code: str
    country: str
    notes: Optional[str] = None
    subtotal_amount: Decimal = Field(sa_column=Column(Numeric(12, 2), nullable=False))
    total_amount: Decimal = Field(sa_column=Column(Numeric(12, 2), nullable=False))
    status: str = Field(default="pending", index=True)
    idempotency_key: Optional[str] = Field(default=None, index=True)


class OrderItem(SQLModel, table=True):
    __tablename__ = "order_items"

    id: Optional[int] = Field(default=None, primary_key=True)
    order_id: int = Field(foreign_key="orders.id", index=True)
    product_id: int = Field(foreign_key="products.id")
    variant_id: Optional[int] = Field(default=None, foreign_key="product_variants.id")
    product_name_snapshot: str
    variant_snapshot_json: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    unit_price_snapshot: Decimal = Field(sa_column=Column(Numeric(12, 2), nullable=False))
    quantity: int
    line_total: Decimal = Field(sa_column=Column(Numeric(12, 2), nullable=False))


class OrderStatusHistory(SQLModel, table=True):
    __tablename__ = "order_status_history"

    id: Optional[int] = Field(default=None, primary_key=True)
    order_id: int = Field(foreign_key="orders.id", index=True)
    previous_status: Optional[str] = None
    new_status: str
    changed_by_user_id: Optional[int] = Field(default=None, foreign_key="users.id")
    admin_note: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class UserEvent(SQLModel, table=True):
    __tablename__ = "user_events"
    __table_args__ = (Index("ix_event_type_created", "event_type", "created_at"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    event_type: str = Field(index=True)
    user_id: Optional[int] = Field(default=None, index=True)
    anonymous_id: Optional[str] = Field(default=None, index=True)
    session_id: str = Field(index=True)
    product_id: Optional[int] = Field(default=None, index=True)
    variant_id: Optional[int] = Field(default=None, index=True)
    category_id: Optional[int] = Field(default=None, index=True)
    source_page: Optional[str] = None
    page_url: Optional[str] = None
    rank_position: Optional[int] = None
    dwell_ms: Optional[int] = None
    recommendation_slot: Optional[str] = None
    recommendation_request_id: Optional[str] = None
    metadata_json: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.utcnow)


class CMSPage(TimestampedModel, table=True):
    __tablename__ = "cms_pages"

    id: Optional[int] = Field(default=None, primary_key=True)
    slug: str = Field(unique=True, index=True)
    title: str
    content_markdown: str
    is_published: bool = True


class JobRun(TimestampedModel, table=True):
    __tablename__ = "job_runs"
    __table_args__ = (Index("ix_job_name_started", "job_name", "started_at"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    job_name: str = Field(index=True)
    status: str = Field(default="running", index=True)
    started_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None
    details_json: Optional[dict] = Field(default=None, sa_column=Column(JSON))
