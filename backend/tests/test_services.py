import csv
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlmodel import select

import app.services as services_module
from app.bootstrap import ExportBootstrapService
from app.config import get_settings
from app.models import (
    Cart,
    CartItem,
    CartStatus,
    Category,
    Product,
    ProductImage,
    ProductVariant,
    SourceCatalogItem,
    SourceCategoryMap,
    SourceProductLink,
    SyncStatus,
    User,
    UserEvent,
    WishlistItem,
)
from app.schemas import EventBatchIn, EventIn, ImportRunIn, OrderAddressIn, OrderCreateIn, OrderLineIn, SourceItemIn
from app.security import hash_password
from app.services import (
    CatalogService,
    EventIngestionService,
    IngestionService,
    JobOrchestrator,
    OrderService,
    RecommendationService,
    SyncService,
    _build_main_image_candidates_from_primary,
)
from app.size_utils import decode_shoe_size_with_quantity, normalize_shoe_size_label
from app.taxonomy import CANONICAL_CATEGORIES
from scripts.remap_catalog_taxonomy import remap_catalog_taxonomy


def _write_csv(path: Path, headers: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def test_import_deduplication(session):
    service = IngestionService()
    payload = ImportRunIn(
        source_system="supplier_a",
        items=[
            SourceItemIn(
                source_system="supplier_a",
                source_product_id="p-1",
                source_url="https://s/item-1",
                title="Item 1",
                price=Decimal("10.00"),
                currency="KZT",
                category_path="men/shoes",
                images=["https://img/1.jpg"],
                last_seen_at=datetime.utcnow(),
            )
        ],
    )

    service.run_import(session, payload.source_system, payload.items)
    service.run_import(session, payload.source_system, payload.items)

    items = session.exec(select(SourceCatalogItem)).all()
    assert len(items) == 1


def test_publication_requires_mapping(session):
    ingestion = IngestionService()
    sync = SyncService()

    item = SourceItemIn(
        source_system="supplier_a",
        source_product_id="x-1",
        source_url="https://s/x1",
        title="Mapped Later",
        price=Decimal("55.00"),
        currency="KZT",
        category_path="unknown/category",
        images=["https://img/x1.jpg"],
        last_seen_at=datetime.utcnow(),
    )
    ingestion.run_import(session, "supplier_a", [item])
    source_row = session.exec(select(SourceCatalogItem)).first()

    decision = sync.sync_source_item(session, source_row)
    product = session.get(Product, decision.product_id)

    assert decision.reason == "missing_category_mapping"
    assert product.is_active is False


def test_sync_assigns_default_other_color_to_variants(session):
    category = Category(name="Shoes", slug="svc-shoes")
    session.add(category)
    session.commit()
    session.refresh(category)

    session.add(
        SourceCategoryMap(
            source_system="supplier_a",
            source_category_key="men/shoes",
            internal_category_id=category.id,
        )
    )
    session.commit()

    ingestion = IngestionService()
    sync = SyncService()

    item = SourceItemIn(
        source_system="supplier_a",
        source_product_id="colorless-1",
        source_url="https://s/colorless-1",
        title="Colorless Model",
        price=Decimal("80.00"),
        currency="KZT",
        category_path="men/shoes",
        images=["https://img/colorless-1.jpg"],
        stock={"42": 2},
        last_seen_at=datetime.utcnow(),
    )
    ingestion.run_import(session, "supplier_a", [item])
    source_row = session.exec(
        select(SourceCatalogItem).where(SourceCatalogItem.source_product_id == "colorless-1")
    ).first()
    sync.sync_source_item(session, source_row)

    variants = session.exec(select(ProductVariant).order_by(ProductVariant.id.asc())).all()
    assert variants
    assert all(variant.color == "Other" for variant in variants)


def test_normalize_shoe_size_label_accepts_only_eu_half_steps():
    assert normalize_shoe_size_label("34") == "34"
    assert normalize_shoe_size_label("34.5") == "34.5"
    assert normalize_shoe_size_label("36") == "36"
    assert normalize_shoe_size_label("36.5") == "36.5"
    assert normalize_shoe_size_label("44,5") == "44.5"
    assert normalize_shoe_size_label(50) == "50"
    assert normalize_shoe_size_label("341487") == "34"
    assert normalize_shoe_size_label("34.513") == "34.5"

    for invalid in ("00341", "741334", "99", "abc", "33.5", "50.3", "50.5", "51", "", None):
        assert normalize_shoe_size_label(invalid) is None


def test_decode_shoe_size_with_quantity_extracts_encoded_quantity():
    assert decode_shoe_size_with_quantity("34") == ("34", None)
    assert decode_shoe_size_with_quantity("34.5") == ("34.5", None)
    assert decode_shoe_size_with_quantity("341487") == ("34", 1487)
    assert decode_shoe_size_with_quantity("34.513") == ("34.5", 13)
    assert decode_shoe_size_with_quantity("44,51044") == ("44.5", 1044)
    assert decode_shoe_size_with_quantity("741334") == (None, None)
    assert decode_shoe_size_with_quantity("00341") == (None, None)


def test_sync_reuses_product_by_signature_and_upserts_sizes(session):
    category = Category(name="Sig Shoes", slug="sig-shoes")
    session.add(category)
    session.commit()
    session.refresh(category)

    session.add(
        SourceCategoryMap(
            source_system="supplier_sig",
            source_category_key="men/sig-shoes",
            internal_category_id=category.id,
        )
    )
    session.commit()

    ingestion = IngestionService()
    sync = SyncService()

    first_item = SourceItemIn(
        source_system="supplier_sig",
        source_product_id="sig-1",
        source_url="https://supplier.sig/p/sig-1",
        title="Signature Model",
        price=Decimal("190.00"),
        currency="KZT",
        category_path="men/sig-shoes",
        brand="SigBrand",
        images=["https://img.sig/model-main.jpg"],
        stock={"42": 2},
        last_seen_at=datetime.utcnow(),
    )
    second_item = SourceItemIn(
        source_system="supplier_sig",
        source_product_id="sig-2",
        source_url="https://supplier.sig/p/sig-2",
        title="Signature Model",
        price=Decimal("190.00"),
        currency="KZT",
        category_path="men/sig-shoes",
        brand="SigBrand",
        images=["https://img.sig/model-main.jpg"],
        stock={"43": 3},
        last_seen_at=datetime.utcnow(),
    )

    ingestion.run_import(session, "supplier_sig", [first_item, second_item])

    source_rows = session.exec(
        select(SourceCatalogItem)
        .where(SourceCatalogItem.source_system == "supplier_sig")
        .order_by(SourceCatalogItem.source_product_id.asc())
    ).all()
    assert len(source_rows) == 2

    first_decision = sync.sync_source_item(session, source_rows[0])
    second_decision = sync.sync_source_item(session, source_rows[1])
    assert first_decision.product_id == second_decision.product_id

    products = session.exec(select(Product).where(Product.name == "Signature Model")).all()
    assert len(products) == 1
    product_id = int(products[0].id)

    links = session.exec(
        select(SourceProductLink).where(SourceProductLink.product_id == product_id)
    ).all()
    assert len(links) == 2

    variants = session.exec(
        select(ProductVariant)
        .where(ProductVariant.product_id == product_id)
        .order_by(ProductVariant.size.asc(), ProductVariant.id.asc())
    ).all()
    assert {(variant.size, variant.color) for variant in variants} == {("42", "Other"), ("43", "Other")}
    assert {variant.size for variant in variants} == {"42", "43"}


def test_sync_decodes_encoded_sizes_and_ignores_invalid_sizes(session):
    category = Category(name="Mixed Sizes", slug="mixed-sizes")
    session.add(category)
    session.commit()
    session.refresh(category)

    session.add(
        SourceCategoryMap(
            source_system="supplier_sizes",
            source_category_key="men/shoes",
            internal_category_id=category.id,
        )
    )
    session.commit()

    sync = SyncService()

    source_item = SourceCatalogItem(
        source_system="supplier_sizes",
        source_product_id="mixed-1",
        source_url="https://supplier.sizes/p/mixed-1",
        source_title="Mixed Size Model",
        source_price=Decimal("180.00"),
        source_currency="KZT",
        source_category_path="men/shoes",
        source_media_json=["https://img.sizes/model.jpg"],
        source_stock_json={
            "sizes": ["341487", "34.513", "74", "41", "44,5", "99", "00", "741334"],
            "stock_by_size": {"41": 2, "44,5": 1},
        },
        last_seen_at=datetime.utcnow(),
    )
    session.add(source_item)
    session.commit()
    session.refresh(source_item)

    decision = sync.sync_source_item(session, source_item)
    variants = session.exec(
        select(ProductVariant)
        .where(ProductVariant.product_id == decision.product_id)
        .order_by(ProductVariant.size.asc(), ProductVariant.id.asc())
    ).all()

    size_to_quantity = {variant.size: variant.stock_quantity for variant in variants}
    assert size_to_quantity == {"34": 1487, "34.5": 13, "41": 2, "44.5": 1}
    assert all(variant.color == "Other" for variant in variants)


def test_normalize_variant_sizes_job_updates_sizes_and_returns_counters(session):
    category = Category(name="Normalize Sizes", slug="normalize-sizes")
    session.add(category)
    session.commit()
    session.refresh(category)

    product = Product(
        slug="normalize-size-product",
        name="Normalize Size Product",
        brand_name="SizeBrand",
        price=Decimal("199.00"),
        currency="KZT",
        category_id=category.id,
        is_active=True,
    )
    session.add(product)
    session.commit()
    session.refresh(product)

    session.add(
        ProductVariant(
            product_id=product.id,
            sku="normalize-v-44-5",
            size="44,5",
            color="Black",
            stock_quantity=2,
            is_active=True,
        )
    )
    session.add(
        ProductVariant(
            product_id=product.id,
            sku="normalize-v-341487",
            size="341487",
            color="Black",
            stock_quantity=2,
            is_active=True,
        )
    )
    session.add(
        ProductVariant(
            product_id=product.id,
            sku="normalize-v-74",
            size="74",
            color="Black",
            stock_quantity=2,
            is_active=True,
        )
    )
    session.add(
        ProductVariant(
            product_id=product.id,
            sku="normalize-v-99",
            size="99",
            color="Black",
            stock_quantity=2,
            is_active=True,
        )
    )
    session.add(
        ProductVariant(
            product_id=product.id,
            sku="normalize-v-41",
            size="41",
            color="Black",
            stock_quantity=2,
            is_active=True,
        )
    )
    session.add(
        ProductVariant(
            product_id=product.id,
            sku="normalize-v-empty",
            size=None,
            color="Black",
            stock_quantity=2,
            is_active=True,
        )
    )
    session.commit()

    run = JobOrchestrator().run_normalize_variant_sizes_job(session)
    assert run.status == "succeeded"
    assert run.details_json == {
        "processed": 6,
        "normalized": 2,
        "invalid_cleared": 2,
        "unchanged": 2,
        "stock_updated_from_encoded": 1,
    }

    variants = session.exec(
        select(ProductVariant)
        .where(ProductVariant.product_id == product.id)
        .order_by(ProductVariant.sku.asc())
    ).all()
    size_by_sku = {variant.sku: variant.size for variant in variants}
    stock_by_sku = {variant.sku: variant.stock_quantity for variant in variants}
    assert size_by_sku == {
        "normalize-v-41": "41",
        "normalize-v-44-5": "44.5",
        "normalize-v-341487": "34",
        "normalize-v-74": None,
        "normalize-v-99": None,
        "normalize-v-empty": None,
    }
    assert stock_by_sku["normalize-v-341487"] == 1487


def test_dedupe_jobs_dry_run_and_apply_merge_products(session):
    category = Category(name="Dedupe Shoes", slug="dedupe-shoes")
    session.add(category)
    session.commit()
    session.refresh(category)

    canonical_product = Product(
        slug="dedupe-canonical",
        name="Dedupe Model",
        brand_name="DedupeBrand",
        price=Decimal("159.00"),
        currency="KZT",
        category_id=category.id,
        is_active=True,
    )
    duplicate_product = Product(
        slug="dedupe-duplicate",
        name="Dedupe Model",
        brand_name="DedupeBrand",
        price=Decimal("159.00"),
        currency="KZT",
        category_id=category.id,
        is_active=True,
    )
    session.add(canonical_product)
    session.add(duplicate_product)
    session.commit()
    session.refresh(canonical_product)
    session.refresh(duplicate_product)

    canonical_variant = ProductVariant(
        product_id=canonical_product.id,
        sku="dedupe-v-canonical",
        size="42",
        color="Black",
        stock_quantity=1,
        is_active=True,
    )
    duplicate_variant = ProductVariant(
        product_id=duplicate_product.id,
        sku="dedupe-v-duplicate",
        size="42",
        color="Black",
        stock_quantity=5,
        is_active=True,
    )
    session.add(canonical_variant)
    session.add(duplicate_variant)
    session.commit()
    session.refresh(canonical_variant)
    session.refresh(duplicate_variant)

    source_item_1 = SourceCatalogItem(
        source_system="supplier_dedupe",
        source_product_id="dup-1",
        source_url="https://supplier.dedupe/p/dup-1",
        source_title="Dedupe Model",
        source_brand="DedupeBrand",
        source_category_path="men/dedupe",
        source_price=Decimal("159.00"),
        source_currency="KZT",
        source_media_json=["https://img.dedupe/model-main.jpg"],
    )
    source_item_2 = SourceCatalogItem(
        source_system="supplier_dedupe",
        source_product_id="dup-2",
        source_url="https://supplier.dedupe/p/dup-2",
        source_title="Dedupe Model",
        source_brand="DedupeBrand",
        source_category_path="men/dedupe",
        source_price=Decimal("159.00"),
        source_currency="KZT",
        source_media_json=["https://img.dedupe/model-main.jpg"],
    )
    session.add(source_item_1)
    session.add(source_item_2)
    session.commit()
    session.refresh(source_item_1)
    session.refresh(source_item_2)

    session.add(
        SourceProductLink(
            source_catalog_item_id=source_item_1.id,
            product_id=canonical_product.id,
            sync_status=SyncStatus.SYNCED,
        )
    )
    session.add(
        SourceProductLink(
            source_catalog_item_id=source_item_2.id,
            product_id=duplicate_product.id,
            sync_status=SyncStatus.SYNCED,
        )
    )

    cart = Cart(anonymous_id="anon-dedupe", status=CartStatus.ACTIVE)
    user = User(email="dedupe-user@example.com", password_hash=hash_password("secret"), full_name="Dedupe User")
    session.add(cart)
    session.add(user)
    session.commit()
    session.refresh(cart)
    session.refresh(user)

    session.add(
        CartItem(
            cart_id=cart.id,
            product_id=canonical_product.id,
            variant_id=canonical_variant.id,
            quantity=1,
            unit_price_snapshot=canonical_product.price,
        )
    )
    session.add(
        CartItem(
            cart_id=cart.id,
            product_id=duplicate_product.id,
            variant_id=duplicate_variant.id,
            quantity=2,
            unit_price_snapshot=duplicate_product.price,
        )
    )
    session.add(
        WishlistItem(
            user_id=user.id,
            product_id=canonical_product.id,
            variant_id=canonical_variant.id,
        )
    )
    session.add(
        WishlistItem(
            user_id=user.id,
            product_id=duplicate_product.id,
            variant_id=duplicate_variant.id,
        )
    )
    session.commit()

    orchestrator = JobOrchestrator()
    dry_run = orchestrator.run_dedupe_products_dry_run_job(session)
    assert dry_run.status == "succeeded"
    assert dry_run.details_json["clusters_total"] == 1
    assert dry_run.details_json["redundant_products"] == 1

    apply_run = orchestrator.run_dedupe_products_apply_job(session)
    assert apply_run.status == "succeeded"
    assert apply_run.details_json["applied_clusters"] == 1
    assert apply_run.details_json["products_deactivated"] == 1
    assert apply_run.details_json["remaining_clusters"] == 0

    updated_duplicate_product = session.get(Product, duplicate_product.id)
    assert updated_duplicate_product.is_active is False

    links = session.exec(
        select(SourceProductLink)
        .where(SourceProductLink.source_catalog_item_id == source_item_2.id)
    ).all()
    assert len(links) == 1
    assert links[0].product_id == canonical_product.id

    refreshed_canonical_variant = session.get(ProductVariant, canonical_variant.id)
    refreshed_duplicate_variant = session.get(ProductVariant, duplicate_variant.id)
    assert refreshed_canonical_variant.stock_quantity == 5
    assert refreshed_duplicate_variant.is_active is False

    cart_items = session.exec(
        select(CartItem)
        .where(CartItem.cart_id == cart.id)
        .order_by(CartItem.id.asc())
    ).all()
    assert len(cart_items) == 1
    assert cart_items[0].product_id == canonical_product.id
    assert cart_items[0].variant_id == canonical_variant.id
    assert cart_items[0].quantity == 3

    wishlist_items = session.exec(
        select(WishlistItem)
        .where(WishlistItem.user_id == user.id)
        .order_by(WishlistItem.id.asc())
    ).all()
    assert len(wishlist_items) == 1
    assert wishlist_items[0].product_id == canonical_product.id
    assert wishlist_items[0].variant_id == canonical_variant.id


def test_manual_override_not_overwritten(session):
    category = Category(name="Shoes", slug="shoes")
    session.add(category)
    session.commit()
    session.refresh(category)

    mapping = SourceCategoryMap(
        source_system="supplier_a",
        source_category_key="men/shoes",
        internal_category_id=category.id,
    )
    session.add(mapping)
    session.commit()

    ingestion = IngestionService()
    sync = SyncService()

    item = SourceItemIn(
        source_system="supplier_a",
        source_product_id="ovr-1",
        source_url="https://s/o1",
        title="Original Title",
        price=Decimal("70.00"),
        currency="KZT",
        category_path="men/shoes",
        images=["https://img/o1.jpg"],
        last_seen_at=datetime.utcnow(),
    )
    ingestion.run_import(session, "supplier_a", [item])
    source_row = session.exec(select(SourceCatalogItem)).first()
    decision = sync.sync_source_item(session, source_row)
    product = session.get(Product, decision.product_id)

    product.name = "Admin Owned Title"
    product.admin_overrides_json = ["name"]
    session.add(product)
    session.commit()

    item2 = item.model_copy(update={"title": "Source Updated Title"})
    ingestion.run_import(session, "supplier_a", [item2])
    source_row = session.exec(select(SourceCatalogItem).where(SourceCatalogItem.source_product_id == "ovr-1")).first()
    sync.sync_source_item(session, source_row)

    product = session.get(Product, product.id)
    assert product.name == "Admin Owned Title"


def test_order_stock_recheck_and_idempotency(session):
    category = Category(name="Tees", slug="tees")
    session.add(category)
    session.commit()
    session.refresh(category)

    product = Product(
        slug="tee-1",
        name="Tee",
        price=Decimal("20.00"),
        currency="KZT",
        category_id=category.id,
        is_active=True,
    )
    session.add(product)
    session.commit()
    session.refresh(product)

    variant = ProductVariant(product_id=product.id, sku="tee-1-m", stock_quantity=3)
    session.add(variant)

    user = User(
        email="buyer@example.com",
        password_hash=hash_password("secret"),
        full_name="Buyer",
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    session.refresh(variant)

    payload = OrderCreateIn(
        idempotency_key="order-dup-1",
        shipping=OrderAddressIn(
            full_name="Buyer",
            email="buyer@example.com",
            phone="123",
            address_line1="Street",
            city="City",
            postal_code="12345",
            country="US",
        ),
        items=[OrderLineIn(product_id=product.id, variant_id=variant.id, quantity=2)],
    )

    service = OrderService()
    order1 = service.create_order(session, user, None, payload)
    order2 = service.create_order(session, user, None, payload)

    updated_variant = session.get(ProductVariant, variant.id)
    assert order1.id == order2.id
    assert updated_variant.stock_quantity == 1


def test_events_batch_persistence(session):
    service = EventIngestionService()
    inserted = service.ingest_batch(
        session,
        EventBatchIn(
            events=[
                EventIn(event_type="product_view", session_id="s1", anonymous_id="a1"),
                EventIn(event_type="recommendation_click", session_id="s1", anonymous_id="a1"),
                EventIn(event_type="unknown_event", session_id="s1", anonymous_id="a1"),
            ]
        ),
    )

    rows = session.exec(select(UserEvent)).all()
    assert inserted == 2
    assert len(rows) == 2


def test_recommendations_filter_inactive(session):
    category = Category(name="Caps", slug="caps")
    session.add(category)
    session.commit()
    session.refresh(category)

    active = Product(
        slug="cap-active",
        name="Cap Active",
        price=Decimal("15.00"),
        currency="KZT",
        category_id=category.id,
        is_active=True,
    )
    inactive = Product(
        slug="cap-inactive",
        name="Cap Inactive",
        price=Decimal("15.00"),
        currency="KZT",
        category_id=category.id,
        is_active=False,
    )
    session.add(active)
    session.add(inactive)
    session.commit()
    session.refresh(active)
    session.refresh(inactive)

    session.add(ProductVariant(product_id=active.id, sku="cap-active-1", stock_quantity=5, is_active=True))
    session.add(ProductVariant(product_id=inactive.id, sku="cap-inactive-1", stock_quantity=5, is_active=True))
    session.add(
        UserEvent(
            event_type="product_view",
            session_id="sess-1",
            anonymous_id="anon-1",
            product_id=active.id,
        )
    )
    session.add(
        UserEvent(
            event_type="product_view",
            session_id="sess-1",
            anonymous_id="anon-1",
            product_id=inactive.id,
        )
    )
    session.commit()

    recs = RecommendationService().get_recommendations(
        session=session,
        context="home",
        limit=10,
        anonymous_id="anon-1",
    )

    ids = [item.product_id for item in recs.items]
    assert active.id in ids
    assert inactive.id not in ids


def test_catalog_filter_by_multiple_categories_and_legacy_id(session):
    category_boots = Category(name="Boots", slug="svc-boots")
    category_jerseys = Category(name="Jerseys", slug="svc-jerseys")
    session.add(category_boots)
    session.add(category_jerseys)
    session.commit()
    session.refresh(category_boots)
    session.refresh(category_jerseys)

    boot = Product(
        slug="svc-boot",
        name="Service Boot",
        price=Decimal("99.00"),
        currency="KZT",
        category_id=category_boots.id,
        is_active=True,
    )
    jersey = Product(
        slug="svc-jersey",
        name="Service Jersey",
        price=Decimal("79.00"),
        currency="KZT",
        category_id=category_jerseys.id,
        is_active=True,
    )
    session.add(boot)
    session.add(jersey)
    session.commit()
    session.refresh(boot)
    session.refresh(jersey)

    service = CatalogService()

    from_multi = service.list_products(session, category_ids=[category_boots.id, category_jerseys.id])
    assert {item["id"] for item in from_multi} == {boot.id, jersey.id}

    from_union = service.list_products(session, category_id=category_boots.id, category_ids=[category_jerseys.id])
    assert {item["id"] for item in from_union} == {boot.id, jersey.id}


def test_bootstrap_from_exports_idempotent(session, tmp_path):
    exports_dir = tmp_path / "exports"
    report_dir = tmp_path / "reports"

    _write_csv(
        exports_dir / "source_catalog_items.csv",
        [
            "id",
            "source_system",
            "source_product_id",
            "source_url",
            "source_title",
            "source_description",
            "source_brand",
            "source_category_path",
            "source_attributes_json",
            "source_price",
            "source_compare_at_price",
            "source_currency",
            "source_stock_json",
            "source_media_json",
            "normalized_payload_json",
            "normalized_status",
            "last_seen_at",
        ],
        [
            {
                "id": 1,
                "source_system": "supplier_test",
                "source_product_id": "sku-1",
                "source_url": "https://supplier.local/item/sku-1",
                "source_title": "Boot One",
                "source_description": "desc",
                "source_brand": "BrandX",
                "source_category_path": "shoes/boots",
                "source_attributes_json": "{}",
                "source_price": "100.00",
                "source_compare_at_price": "",
                "source_currency": "KZT",
                "source_stock_json": "{\"sizes\": [\"42\"]}",
                "source_media_json": "[\"https://img/boot-1.jpg\"]",
                "normalized_payload_json": "{\"media_urls\": []}",
                "normalized_status": "pending",
                "last_seen_at": datetime.utcnow().isoformat(),
            }
        ],
    )
    _write_csv(
        exports_dir / "source_product_links.csv",
        [
            "id",
            "source_system",
            "source_product_id",
            "source_item_id",
            "product_id",
            "sync_status",
            "missing_runs_count",
            "last_synced_at",
        ],
        [
            {
                "id": 1,
                "source_system": "supplier_test",
                "source_product_id": "sku-1",
                "source_item_id": 1,
                "product_id": 1,
                "sync_status": "synced",
                "missing_runs_count": 0,
                "last_synced_at": datetime.utcnow().isoformat(),
            }
        ],
    )
    _write_csv(
        exports_dir / "products.csv",
        [
            "id",
            "external_source_system",
            "external_source_product_id",
            "canonical_url",
            "title",
            "brand_name",
            "category_slug",
            "description",
            "seller_name",
            "color",
            "current_price",
            "compare_at_price",
            "currency",
            "is_active",
        ],
        [
            {
                "id": 1,
                "external_source_system": "supplier_test",
                "external_source_product_id": "sku-1",
                "canonical_url": "https://footy.local/p/boot-one",
                "title": "Boot One",
                "brand_name": "BrandX",
                "category_slug": "boots",
                "description": "desc",
                "seller_name": "seller",
                "color": "Black",
                "current_price": "100.00",
                "compare_at_price": "",
                "currency": "KZT",
                "is_active": "1",
            }
        ],
    )
    _write_csv(
        exports_dir / "product_images.csv",
        ["id", "product_id", "storage_mode", "source_url", "file_path", "cdn_asset_id", "is_primary", "sort_order", "created_at"],
        [
            {
                "id": 1,
                "product_id": 1,
                "storage_mode": "external_cdn",
                "source_url": "https://img/boot-1.jpg",
                "file_path": "",
                "cdn_asset_id": "",
                "is_primary": "1",
                "sort_order": "0",
                "created_at": datetime.utcnow().isoformat(),
            }
        ],
    )
    _write_csv(
        exports_dir / "product_variants.csv",
        ["id", "product_id", "size", "color", "stock_status", "metadata_json", "created_at", "updated_at"],
        [
            {
                "id": 1,
                "product_id": 1,
                "size": "42",
                "color": "Black",
                "stock_status": "in_stock",
                "metadata_json": "{}",
                "created_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat(),
            }
        ],
    )
    _write_csv(
        exports_dir / "product_tags.csv",
        ["id", "product_id", "tag", "created_at", "updated_at"],
        [
            {
                "id": 1,
                "product_id": 1,
                "tag": "NEW",
                "created_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat(),
            }
        ],
    )
    _write_csv(
        exports_dir / "import_runs.csv",
        [
            "id",
            "source_system",
            "run_type",
            "status",
            "root_url",
            "checkpoint_json",
            "pages_total",
            "pages_processed",
            "items_seen_count",
            "items_created_count",
            "items_updated_count",
            "items_failed_count",
            "error_summary",
            "started_at",
            "finished_at",
        ],
        [
            {
                "id": 1,
                "source_system": "supplier_test",
                "run_type": "import",
                "status": "completed",
                "root_url": "",
                "checkpoint_json": "{}",
                "pages_total": 1,
                "pages_processed": 1,
                "items_seen_count": 1,
                "items_created_count": 1,
                "items_updated_count": 0,
                "items_failed_count": 0,
                "error_summary": "",
                "started_at": datetime.utcnow().isoformat(),
                "finished_at": datetime.utcnow().isoformat(),
            }
        ],
    )

    service = ExportBootstrapService(exports_dir=exports_dir, report_dir=report_dir)
    report_1 = service.bootstrap(session)
    report_2 = service.bootstrap(session)

    assert report_1["ok"] is True
    assert report_2["ok"] is True
    assert session.exec(select(SourceCatalogItem)).all()
    assert len(session.exec(select(Product)).all()) == 1
    assert len(session.exec(select(ProductVariant)).all()) == 1


def test_bootstrap_normalizes_variant_sizes_and_hides_invalid_facets(session, tmp_path):
    exports_dir = tmp_path / "exports"
    report_dir = tmp_path / "reports"
    now = datetime.utcnow().isoformat()

    _write_csv(
        exports_dir / "source_catalog_items.csv",
        [
            "id",
            "source_system",
            "source_product_id",
            "source_url",
            "source_title",
            "source_description",
            "source_brand",
            "source_category_path",
            "source_attributes_json",
            "source_price",
            "source_compare_at_price",
            "source_currency",
            "source_stock_json",
            "source_media_json",
            "normalized_payload_json",
            "normalized_status",
            "last_seen_at",
        ],
        [
            {
                "id": 1,
                "source_system": "bootstrap_sizes",
                "source_product_id": "size-boot-1",
                "source_url": "https://supplier.local/item/size-boot-1",
                "source_title": "Size Boot Normalized",
                "source_description": "desc",
                "source_brand": "BrandS",
                "source_category_path": "shoes/boots",
                "source_attributes_json": "{}",
                "source_price": "130.00",
                "source_compare_at_price": "",
                "source_currency": "KZT",
                "source_stock_json": "{\"sizes\": [\"41\", \"341487\", \"34.513\", \"741334\", \"99\"], \"stock_by_size\": {\"41\": 2}}",
                "source_media_json": "[\"https://img/size-boot-1.jpg\"]",
                "normalized_payload_json": "{\"media_urls\": []}",
                "normalized_status": "pending",
                "last_seen_at": now,
            }
        ],
    )
    _write_csv(
        exports_dir / "source_product_links.csv",
        [
            "id",
            "source_system",
            "source_product_id",
            "source_item_id",
            "product_id",
            "sync_status",
            "missing_runs_count",
            "last_synced_at",
        ],
        [
            {
                "id": 1,
                "source_system": "bootstrap_sizes",
                "source_product_id": "size-boot-1",
                "source_item_id": 1,
                "product_id": 1,
                "sync_status": "synced",
                "missing_runs_count": 0,
                "last_synced_at": now,
            }
        ],
    )
    _write_csv(
        exports_dir / "products.csv",
        [
            "id",
            "external_source_system",
            "external_source_product_id",
            "canonical_url",
            "title",
            "brand_name",
            "category_slug",
            "description",
            "seller_name",
            "color",
            "current_price",
            "compare_at_price",
            "currency",
            "is_active",
        ],
        [
            {
                "id": 1,
                "external_source_system": "bootstrap_sizes",
                "external_source_product_id": "size-boot-1",
                "canonical_url": "https://footy.local/p/size-boot-normalized",
                "title": "Size Boot Normalized",
                "brand_name": "BrandS",
                "category_slug": "boots",
                "description": "desc",
                "seller_name": "seller",
                "color": "Black",
                "current_price": "130.00",
                "compare_at_price": "",
                "currency": "KZT",
                "is_active": "1",
            }
        ],
    )
    _write_csv(
        exports_dir / "product_images.csv",
        [
            "id",
            "product_id",
            "storage_mode",
            "source_url",
            "file_path",
            "cdn_asset_id",
            "is_primary",
            "sort_order",
            "created_at",
        ],
        [
            {
                "id": 1,
                "product_id": 1,
                "storage_mode": "external_cdn",
                "source_url": "https://img/size-boot-1.jpg",
                "file_path": "",
                "cdn_asset_id": "",
                "is_primary": "1",
                "sort_order": "0",
                "created_at": now,
            }
        ],
    )
    _write_csv(
        exports_dir / "product_variants.csv",
        ["id", "product_id", "size", "color", "stock_status", "metadata_json", "created_at", "updated_at"],
        [
            {
                "id": 1,
                "product_id": 1,
                "size": "41",
                "color": "Black",
                "stock_status": "in_stock",
                "metadata_json": "{}",
                "created_at": now,
                "updated_at": now,
            },
            {
                "id": 2,
                "product_id": 1,
                "size": "341487",
                "color": "Black",
                "stock_status": "in_stock",
                "metadata_json": "{}",
                "created_at": now,
                "updated_at": now,
            },
            {
                "id": 3,
                "product_id": 1,
                "size": "34.513",
                "color": "Black",
                "stock_status": "in_stock",
                "metadata_json": "{}",
                "created_at": now,
                "updated_at": now,
            },
            {
                "id": 4,
                "product_id": 1,
                "size": "741334",
                "color": "Black",
                "stock_status": "in_stock",
                "metadata_json": "{}",
                "created_at": now,
                "updated_at": now,
            },
        ],
    )
    _write_csv(
        exports_dir / "product_tags.csv",
        ["id", "product_id", "tag", "created_at", "updated_at"],
        [
            {
                "id": 1,
                "product_id": 1,
                "tag": "NEW",
                "created_at": now,
                "updated_at": now,
            }
        ],
    )
    _write_csv(
        exports_dir / "import_runs.csv",
        [
            "id",
            "source_system",
            "run_type",
            "status",
            "root_url",
            "checkpoint_json",
            "pages_total",
            "pages_processed",
            "items_seen_count",
            "items_created_count",
            "items_updated_count",
            "items_failed_count",
            "error_summary",
            "started_at",
            "finished_at",
        ],
        [
            {
                "id": 1,
                "source_system": "bootstrap_sizes",
                "run_type": "import",
                "status": "completed",
                "root_url": "",
                "checkpoint_json": "{}",
                "pages_total": 1,
                "pages_processed": 1,
                "items_seen_count": 1,
                "items_created_count": 1,
                "items_updated_count": 0,
                "items_failed_count": 0,
                "error_summary": "",
                "started_at": now,
                "finished_at": now,
            }
        ],
    )

    service = ExportBootstrapService(exports_dir=exports_dir, report_dir=report_dir)
    report = service.bootstrap(session)
    assert report["ok"] is True

    source_item = session.exec(
        select(SourceCatalogItem).where(
            SourceCatalogItem.source_system == "bootstrap_sizes",
            SourceCatalogItem.source_product_id == "size-boot-1",
        )
    ).first()
    assert source_item is not None
    assert source_item.source_stock_json == {"34": 1487, "34.5": 13, "41": 2}

    variants = session.exec(select(ProductVariant).order_by(ProductVariant.id.asc())).all()
    size_by_sku = {variant.sku: variant.size for variant in variants}
    stock_by_sku = {variant.sku: variant.stock_quantity for variant in variants}
    assert size_by_sku == {
        "expv-1": "41",
        "expv-2": "34",
        "expv-3": "34.5",
        "expv-4": None,
    }
    assert stock_by_sku["expv-2"] == 1487
    assert stock_by_sku["expv-3"] == 13

    catalog = CatalogService().list_catalog_products(session, search="Size Boot Normalized")
    size_keys = {row["key"] for row in catalog["facets"]["sizes"]}
    assert size_keys == {"34", "34.5", "41"}


def test_bootstrap_backfills_variant_color_for_single_color_model(session, tmp_path):
    exports_dir = tmp_path / "exports"
    report_dir = tmp_path / "reports"

    now = datetime.utcnow().isoformat()
    _write_csv(
        exports_dir / "source_catalog_items.csv",
        [
            "id",
            "source_system",
            "source_product_id",
            "source_url",
            "source_title",
            "source_description",
            "source_brand",
            "source_category_path",
            "source_attributes_json",
            "source_price",
            "source_compare_at_price",
            "source_currency",
            "source_stock_json",
            "source_media_json",
            "normalized_payload_json",
            "normalized_status",
            "last_seen_at",
        ],
        [
            {
                "id": 1,
                "source_system": "supplier_test",
                "source_product_id": "sku-1",
                "source_url": "https://supplier.local/item/sku-1",
                "source_title": "Model One",
                "source_description": "desc",
                "source_brand": "BrandX",
                "source_category_path": "shoes/boots",
                "source_attributes_json": "{}",
                "source_price": "100.00",
                "source_compare_at_price": "",
                "source_currency": "KZT",
                "source_stock_json": "{\"sizes\": [\"42\"]}",
                "source_media_json": "[]",
                "normalized_payload_json": "{}",
                "normalized_status": "pending",
                "last_seen_at": now,
            },
            {
                "id": 2,
                "source_system": "supplier_test",
                "source_product_id": "sku-2",
                "source_url": "https://supplier.local/item/sku-2",
                "source_title": "Model One",
                "source_description": "desc",
                "source_brand": "BrandX",
                "source_category_path": "shoes/boots",
                "source_attributes_json": "{}",
                "source_price": "100.00",
                "source_compare_at_price": "",
                "source_currency": "KZT",
                "source_stock_json": "{\"sizes\": [\"42\"]}",
                "source_media_json": "[]",
                "normalized_payload_json": "{}",
                "normalized_status": "pending",
                "last_seen_at": now,
            },
            {
                "id": 3,
                "source_system": "supplier_test",
                "source_product_id": "sku-3",
                "source_url": "https://supplier.local/item/sku-3",
                "source_title": "Model Two",
                "source_description": "desc",
                "source_brand": "BrandY",
                "source_category_path": "shoes/boots",
                "source_attributes_json": "{}",
                "source_price": "120.00",
                "source_compare_at_price": "",
                "source_currency": "KZT",
                "source_stock_json": "{\"sizes\": [\"43\"]}",
                "source_media_json": "[]",
                "normalized_payload_json": "{}",
                "normalized_status": "pending",
                "last_seen_at": now,
            },
        ],
    )
    _write_csv(
        exports_dir / "source_product_links.csv",
        ["id", "source_system", "source_product_id", "source_item_id", "product_id", "sync_status", "missing_runs_count", "last_synced_at"],
        [
            {
                "id": 1,
                "source_system": "supplier_test",
                "source_product_id": "sku-1",
                "source_item_id": 1,
                "product_id": 1,
                "sync_status": "synced",
                "missing_runs_count": 0,
                "last_synced_at": now,
            },
            {
                "id": 2,
                "source_system": "supplier_test",
                "source_product_id": "sku-2",
                "source_item_id": 2,
                "product_id": 2,
                "sync_status": "synced",
                "missing_runs_count": 0,
                "last_synced_at": now,
            },
            {
                "id": 3,
                "source_system": "supplier_test",
                "source_product_id": "sku-3",
                "source_item_id": 3,
                "product_id": 3,
                "sync_status": "synced",
                "missing_runs_count": 0,
                "last_synced_at": now,
            },
        ],
    )
    _write_csv(
        exports_dir / "products.csv",
        [
            "id",
            "external_source_system",
            "external_source_product_id",
            "canonical_url",
            "title",
            "brand_name",
            "category_slug",
            "description",
            "seller_name",
            "color",
            "current_price",
            "compare_at_price",
            "currency",
            "is_active",
        ],
        [
            {
                "id": 1,
                "external_source_system": "supplier_test",
                "external_source_product_id": "sku-1",
                "canonical_url": "https://footy.local/p/model-one-a",
                "title": "Model One",
                "brand_name": "BrandX",
                "category_slug": "boots",
                "description": "desc",
                "seller_name": "seller",
                "color": "Black",
                "current_price": "100.00",
                "compare_at_price": "",
                "currency": "KZT",
                "is_active": "1",
            },
            {
                "id": 2,
                "external_source_system": "supplier_test",
                "external_source_product_id": "sku-2",
                "canonical_url": "https://footy.local/p/model-one-b",
                "title": "Model One",
                "brand_name": "BrandX",
                "category_slug": "boots",
                "description": "desc",
                "seller_name": "seller",
                "color": "",
                "current_price": "100.00",
                "compare_at_price": "",
                "currency": "KZT",
                "is_active": "1",
            },
            {
                "id": 3,
                "external_source_system": "supplier_test",
                "external_source_product_id": "sku-3",
                "canonical_url": "https://footy.local/p/model-two-a",
                "title": "Model Two",
                "brand_name": "BrandY",
                "category_slug": "boots",
                "description": "desc",
                "seller_name": "seller",
                "color": "",
                "current_price": "120.00",
                "compare_at_price": "",
                "currency": "KZT",
                "is_active": "1",
            },
        ],
    )
    _write_csv(
        exports_dir / "product_images.csv",
        ["id", "product_id", "storage_mode", "source_url", "file_path", "cdn_asset_id", "is_primary", "sort_order", "created_at"],
        [],
    )
    _write_csv(
        exports_dir / "product_variants.csv",
        ["id", "product_id", "size", "color", "stock_status", "metadata_json", "created_at", "updated_at"],
        [
            {
                "id": 1,
                "product_id": 1,
                "size": "42",
                "color": "Black - buy online | INTERTOP",
                "stock_status": "in_stock",
                "metadata_json": "{}",
                "created_at": now,
                "updated_at": now,
            },
            {
                "id": 2,
                "product_id": 2,
                "size": "42",
                "color": "",
                "stock_status": "in_stock",
                "metadata_json": "{}",
                "created_at": now,
                "updated_at": now,
            },
            {
                "id": 3,
                "product_id": 3,
                "size": "43",
                "color": "",
                "stock_status": "in_stock",
                "metadata_json": "{}",
                "created_at": now,
                "updated_at": now,
            },
        ],
    )
    _write_csv(
        exports_dir / "product_tags.csv",
        ["id", "product_id", "tag", "created_at", "updated_at"],
        [],
    )
    _write_csv(
        exports_dir / "import_runs.csv",
        [
            "id",
            "source_system",
            "run_type",
            "status",
            "root_url",
            "checkpoint_json",
            "pages_total",
            "pages_processed",
            "items_seen_count",
            "items_created_count",
            "items_updated_count",
            "items_failed_count",
            "error_summary",
            "started_at",
            "finished_at",
        ],
        [],
    )

    service = ExportBootstrapService(exports_dir=exports_dir, report_dir=report_dir)
    report = service.bootstrap(session)

    assert report["ok"] is True
    assert report["created"]["variant_color_backfilled"] == 2
    variants = session.exec(select(ProductVariant).order_by(ProductVariant.id.asc())).all()
    assert len(variants) == 3
    assert variants[0].color == "Black - buy online | INTERTOP"
    assert variants[1].color == "Black"
    assert variants[2].color == "Other"


def test_bootstrap_sanitizes_product_gallery_images(session, tmp_path):
    exports_dir = tmp_path / "exports"
    report_dir = tmp_path / "reports"
    now = datetime.utcnow().isoformat()

    _write_csv(
        exports_dir / "source_catalog_items.csv",
        [
            "id",
            "source_system",
            "source_product_id",
            "source_url",
            "source_title",
            "source_description",
            "source_brand",
            "source_category_path",
            "source_attributes_json",
            "source_price",
            "source_compare_at_price",
            "source_currency",
            "source_stock_json",
            "source_media_json",
            "normalized_payload_json",
            "normalized_status",
            "last_seen_at",
        ],
        [
            {
                "id": 1,
                "source_system": "supplier_test",
                "source_product_id": "sku-1",
                "source_url": "https://supplier.local/item/sku-1",
                "source_title": "Gallery Product",
                "source_description": "desc",
                "source_brand": "BrandX",
                "source_category_path": "shoes/boots",
                "source_attributes_json": "{}",
                "source_price": "100.00",
                "source_compare_at_price": "",
                "source_currency": "KZT",
                "source_stock_json": "{\"sizes\": [\"42\"]}",
                "source_media_json": "[]",
                "normalized_payload_json": "{}",
                "normalized_status": "pending",
                "last_seen_at": now,
            }
        ],
    )
    _write_csv(
        exports_dir / "source_product_links.csv",
        ["id", "source_system", "source_product_id", "source_item_id", "product_id", "sync_status", "missing_runs_count", "last_synced_at"],
        [
            {
                "id": 1,
                "source_system": "supplier_test",
                "source_product_id": "sku-1",
                "source_item_id": 1,
                "product_id": 1,
                "sync_status": "synced",
                "missing_runs_count": 0,
                "last_synced_at": now,
            }
        ],
    )
    _write_csv(
        exports_dir / "products.csv",
        [
            "id",
            "external_source_system",
            "external_source_product_id",
            "canonical_url",
            "title",
            "brand_name",
            "category_slug",
            "description",
            "seller_name",
            "color",
            "current_price",
            "compare_at_price",
            "currency",
            "is_active",
        ],
        [
            {
                "id": 1,
                "external_source_system": "supplier_test",
                "external_source_product_id": "sku-1",
                "canonical_url": "https://footy.local/p/gallery-product",
                "title": "Gallery Product",
                "brand_name": "BrandX",
                "category_slug": "boots",
                "description": "desc",
                "seller_name": "seller",
                "color": "Black",
                "current_price": "100.00",
                "compare_at_price": "",
                "currency": "KZT",
                "is_active": "1",
            }
        ],
    )
    _write_csv(
        exports_dir / "product_images.csv",
        ["id", "product_id", "storage_mode", "source_url", "file_path", "cdn_asset_id", "is_primary", "sort_order", "created_at"],
        [
            {
                "id": 1,
                "product_id": 1,
                "storage_mode": "external_cdn",
                "source_url": "https://cdn.intertop.com/load/mp111111/small/MAIN.jpg/",
                "file_path": "",
                "cdn_asset_id": "",
                "is_primary": "1",
                "sort_order": "0",
                "created_at": now,
            },
            {
                "id": 2,
                "product_id": 1,
                "storage_mode": "external_cdn",
                "source_url": "https://cdn.intertop.com/load/mp111111/big/MAIN.jpg/",
                "file_path": "",
                "cdn_asset_id": "",
                "is_primary": "0",
                "sort_order": "1",
                "created_at": now,
            },
            {
                "id": 3,
                "product_id": 1,
                "storage_mode": "external_cdn",
                "source_url": "https://cdn.intertop.com/load/mp111111/small/2.jpg/",
                "file_path": "",
                "cdn_asset_id": "",
                "is_primary": "0",
                "sort_order": "2",
                "created_at": now,
            },
            {
                "id": 4,
                "product_id": 1,
                "storage_mode": "external_cdn",
                "source_url": "https://cdn.intertop.com/load/mp111111/big/2.jpg/",
                "file_path": "",
                "cdn_asset_id": "",
                "is_primary": "0",
                "sort_order": "3",
                "created_at": now,
            },
            {
                "id": 5,
                "product_id": 1,
                "storage_mode": "external_cdn",
                "source_url": "https://cdn.intertop.com/load/mp999999/small/MAIN.jpg/",
                "file_path": "",
                "cdn_asset_id": "",
                "is_primary": "0",
                "sort_order": "4",
                "created_at": now,
            },
        ],
    )
    _write_csv(
        exports_dir / "product_variants.csv",
        ["id", "product_id", "size", "color", "stock_status", "metadata_json", "created_at", "updated_at"],
        [],
    )
    _write_csv(
        exports_dir / "product_tags.csv",
        ["id", "product_id", "tag", "created_at", "updated_at"],
        [],
    )
    _write_csv(
        exports_dir / "import_runs.csv",
        [
            "id",
            "source_system",
            "run_type",
            "status",
            "root_url",
            "checkpoint_json",
            "pages_total",
            "pages_processed",
            "items_seen_count",
            "items_created_count",
            "items_updated_count",
            "items_failed_count",
            "error_summary",
            "started_at",
            "finished_at",
        ],
        [],
    )

    service = ExportBootstrapService(exports_dir=exports_dir, report_dir=report_dir)
    report = service.bootstrap(session)

    assert report["ok"] is True
    images = session.exec(select(ProductImage).order_by(ProductImage.sort_order.asc(), ProductImage.id.asc())).all()
    assert len(images) == 2
    assert images[0].source_url == "https://cdn.intertop.com/load/mp111111/big/MAIN.jpg"
    assert images[1].source_url == "https://cdn.intertop.com/load/mp111111/big/2.jpg"
    assert all("mp999999" not in (image.source_url or "") for image in images)
    assert images[0].is_primary is True
    assert images[0].sort_order == 0
    assert images[1].is_primary is False
    assert images[1].sort_order == 1


def test_bootstrap_maps_canonical_categories_and_gender(session, tmp_path):
    exports_dir = tmp_path / "exports"
    report_dir = tmp_path / "reports"
    now = datetime.utcnow().isoformat()

    _write_csv(
        exports_dir / "source_catalog_items.csv",
        [
            "id",
            "source_system",
            "source_product_id",
            "source_url",
            "source_title",
            "source_description",
            "source_brand",
            "source_category_path",
            "source_attributes_json",
            "source_price",
            "source_compare_at_price",
            "source_currency",
            "source_stock_json",
            "source_media_json",
            "normalized_payload_json",
            "normalized_status",
            "root_gender",
            "root_category",
            "last_seen_at",
        ],
        [
            {
                "id": 1,
                "source_system": "supplier_test",
                "source_product_id": "sku-1",
                "source_url": "https://supplier.local/item/sku-1",
                "source_title": "Model One",
                "source_description": "desc",
                "source_brand": "BrandX",
                "source_category_path": json.dumps(["women", "shoes", "Кеды низкие"], ensure_ascii=False),
                "source_attributes_json": "{}",
                "source_price": "100.00",
                "source_compare_at_price": "",
                "source_currency": "KZT",
                "source_stock_json": "{\"sizes\": [\"42\"]}",
                "source_media_json": "[]",
                "normalized_payload_json": "{}",
                "normalized_status": "pending",
                "root_gender": "women",
                "root_category": "shoes",
                "last_seen_at": now,
            }
        ],
    )
    _write_csv(
        exports_dir / "source_product_links.csv",
        ["id", "source_system", "source_product_id", "source_item_id", "product_id", "sync_status", "missing_runs_count", "last_synced_at"],
        [
            {
                "id": 1,
                "source_system": "supplier_test",
                "source_product_id": "sku-1",
                "source_item_id": 1,
                "product_id": 1,
                "sync_status": "synced",
                "missing_runs_count": 0,
                "last_synced_at": now,
            }
        ],
    )
    _write_csv(
        exports_dir / "products.csv",
        [
            "id",
            "external_source_system",
            "external_source_product_id",
            "canonical_url",
            "title",
            "brand_name",
            "category_slug",
            "description",
            "seller_name",
            "color",
            "current_price",
            "compare_at_price",
            "currency",
            "is_active",
        ],
        [
            {
                "id": 1,
                "external_source_system": "supplier_test",
                "external_source_product_id": "sku-1",
                "canonical_url": "https://footy.local/p/model-one",
                "title": "Model One",
                "brand_name": "BrandX",
                "category_slug": "shoes",
                "description": "desc",
                "seller_name": "seller",
                "color": "Black",
                "current_price": "100.00",
                "compare_at_price": "",
                "currency": "KZT",
                "is_active": "1",
            }
        ],
    )
    _write_csv(
        exports_dir / "product_images.csv",
        ["id", "product_id", "storage_mode", "source_url", "file_path", "cdn_asset_id", "is_primary", "sort_order", "created_at"],
        [],
    )
    _write_csv(
        exports_dir / "product_variants.csv",
        ["id", "product_id", "size", "color", "stock_status", "metadata_json", "created_at", "updated_at"],
        [],
    )
    _write_csv(
        exports_dir / "product_tags.csv",
        ["id", "product_id", "tag", "created_at", "updated_at"],
        [],
    )
    _write_csv(
        exports_dir / "import_runs.csv",
        [
            "id",
            "source_system",
            "run_type",
            "status",
            "root_url",
            "checkpoint_json",
            "pages_total",
            "pages_processed",
            "items_seen_count",
            "items_created_count",
            "items_updated_count",
            "items_failed_count",
            "error_summary",
            "started_at",
            "finished_at",
        ],
        [],
    )

    service = ExportBootstrapService(exports_dir=exports_dir, report_dir=report_dir)
    report = service.bootstrap(session)

    assert report["ok"] is True
    product = session.exec(select(Product).where(Product.slug == "model-one-sku-1")).first()
    assert product is not None
    category = session.get(Category, product.category_id)
    assert category is not None
    assert category.slug == "kedy"
    assert category.name == "Кеды"
    assert product.gender == "women"
    canonical_slugs = {category_row.slug for category_row in CANONICAL_CATEGORIES}
    active_canonical_categories = session.exec(
        select(Category).where(
            Category.slug.in_(canonical_slugs),
            Category.is_active.is_(True),
        )
    ).all()
    assert len(active_canonical_categories) == len(CANONICAL_CATEGORIES)


def test_remap_catalog_taxonomy_updates_category_and_gender(session):
    legacy_category = Category(name="Shoes", slug="shoes", is_active=True)
    session.add(legacy_category)
    session.commit()
    session.refresh(legacy_category)

    product = Product(
        slug="remap-shoe",
        name="Remap Shoe",
        price=Decimal("100.00"),
        currency="KZT",
        category_id=legacy_category.id,
        gender=None,
        is_active=True,
    )
    session.add(product)
    session.commit()
    session.refresh(product)

    source_item = SourceCatalogItem(
        source_system="supplier_test",
        source_product_id="sku-remap-1",
        source_url="https://supplier.local/item/sku-remap-1",
        source_title="Remap Shoe",
        source_category_path=json.dumps(["women", "shoes", "Ботинки"], ensure_ascii=False),
        source_currency="KZT",
        source_attributes_json={"root_gender": "women"},
    )
    session.add(source_item)
    session.commit()
    session.refresh(source_item)

    session.add(
        SourceProductLink(
            source_catalog_item_id=source_item.id,
            product_id=product.id,
            sync_status=SyncStatus.SYNCED,
        )
    )
    session.commit()

    report = remap_catalog_taxonomy(session)
    session.refresh(product)
    remapped_category = session.get(Category, product.category_id)
    legacy_after = session.exec(select(Category).where(Category.slug == "shoes")).first()

    assert report["ok"] is True
    assert report["updated_products"] >= 1
    assert report["updated_gender"] >= 1
    assert remapped_category is not None
    assert remapped_category.slug == "botinki-i-sapogi"
    assert product.gender == "women"
    assert legacy_after is not None
    assert legacy_after.is_active is False


def test_build_main_image_candidates_from_primary_url():
    assert _build_main_image_candidates_from_primary(
        "https://cdn.intertop.com/load/mp123456/small/2.jpg/"
    ) == [
        "https://cdn.intertop.com/load/mp123456/big/MAIN.jpg",
        "https://cdn.intertop.com/load/mp123456/big/MAIN.jpeg",
        "https://cdn.intertop.com/load/mp123456/big/MAIN.webp",
    ]
    assert _build_main_image_candidates_from_primary(
        "https://cdn.intertop.com/load/mp123456/small/MAIN.jpg"
    ) == []
    assert _build_main_image_candidates_from_primary(
        "https://cdn.intertop.com/load/mp123456/big/2.jpg"
    ) == []


def test_refresh_product_primary_images_job_updates_and_is_idempotent(session, monkeypatch):
    category = Category(name="Primary Fix", slug="primary-fix")
    session.add(category)
    session.commit()
    session.refresh(category)

    product_ok = Product(
        slug="primary-fix-ok",
        name="Primary Fix Ok",
        price=Decimal("120.00"),
        currency="KZT",
        category_id=category.id,
        is_active=True,
    )
    product_skip = Product(
        slug="primary-fix-skip",
        name="Primary Fix Skip",
        price=Decimal("130.00"),
        currency="KZT",
        category_id=category.id,
        is_active=True,
    )
    session.add(product_ok)
    session.add(product_skip)
    session.commit()
    session.refresh(product_ok)
    session.refresh(product_skip)

    session.add(
        ProductImage(
            product_id=product_ok.id,
            source_url="https://cdn.intertop.com/load/mp555111/small/2.jpg/",
            sort_order=0,
            is_primary=True,
        )
    )
    session.add(
        ProductImage(
            product_id=product_ok.id,
            source_url="https://cdn.intertop.com/load/mp555111/small/3.jpg/",
            sort_order=1,
            is_primary=False,
        )
    )
    session.add(
        ProductImage(
            product_id=product_skip.id,
            source_url="https://cdn.intertop.com/load/mp555222/small/2.jpg/",
            sort_order=0,
            is_primary=True,
        )
    )
    session.commit()

    def fake_validate(url: str, timeout_seconds: float = 4.0) -> bool:
        del timeout_seconds
        return url == "https://cdn.intertop.com/load/mp555111/big/MAIN.jpg"

    monkeypatch.setattr(services_module, "_validate_remote_image_url", fake_validate)

    orchestrator = JobOrchestrator()
    run = orchestrator.run_refresh_product_primary_images_job(session)
    assert run.job_name == "refresh_product_primary_images_job"
    assert run.status == "succeeded"
    assert run.details_json == {
        "candidates_total": 2,
        "validated_ok": 1,
        "updated_products": 1,
        "skipped_no_main": 1,
        "errors": 0,
    }

    ok_images = session.exec(
        select(ProductImage)
        .where(ProductImage.product_id == product_ok.id)
        .order_by(ProductImage.sort_order.asc(), ProductImage.id.asc())
    ).all()
    assert ok_images[0].source_url == "https://cdn.intertop.com/load/mp555111/big/MAIN.jpg"
    assert ok_images[0].is_primary is True
    assert ok_images[0].sort_order == 0
    assert sum(1 for image in ok_images if image.source_url == "https://cdn.intertop.com/load/mp555111/big/MAIN.jpg") == 1

    skip_primary = session.exec(
        select(ProductImage)
        .where(
            ProductImage.product_id == product_skip.id,
            ProductImage.is_primary.is_(True),
        )
        .order_by(ProductImage.sort_order.asc(), ProductImage.id.asc())
    ).first()
    assert skip_primary is not None
    assert skip_primary.source_url == "https://cdn.intertop.com/load/mp555222/small/2.jpg/"

    run_second = orchestrator.run_refresh_product_primary_images_job(session)
    assert run_second.status == "succeeded"
    assert run_second.details_json == {
        "candidates_total": 1,
        "validated_ok": 0,
        "updated_products": 0,
        "skipped_no_main": 1,
        "errors": 0,
    }

    ok_images_after = session.exec(
        select(ProductImage)
        .where(ProductImage.product_id == product_ok.id)
        .order_by(ProductImage.sort_order.asc(), ProductImage.id.asc())
    ).all()
    assert sum(1 for image in ok_images_after if image.source_url == "https://cdn.intertop.com/load/mp555111/big/MAIN.jpg") == 1


def test_recommendation_fallback_when_model_missing(session, tmp_path):
    settings = get_settings()
    original_dir = settings.ranker_artifacts_dir
    settings.ranker_artifacts_dir = str(tmp_path / "ranker")
    try:
        category = Category(name="Rec Category", slug="rec-category")
        session.add(category)
        session.commit()
        session.refresh(category)

        product = Product(
            slug="rec-boot",
            name="Rec Boot",
            price=Decimal("50.00"),
            currency="KZT",
            category_id=category.id,
            is_active=True,
        )
        session.add(product)
        session.commit()
        session.refresh(product)
        session.add(ProductVariant(product_id=product.id, sku="rec-boot-v1", stock_quantity=3, is_active=True))
        session.add(
            UserEvent(
                event_type="product_view",
                session_id="rec-session-1",
                anonymous_id="rec-anon-1",
                product_id=product.id,
            )
        )
        session.commit()

        recs = RecommendationService().get_recommendations(
            session=session,
            context="home",
            limit=5,
            anonymous_id="rec-anon-1",
        )
        assert recs.items
    finally:
        settings.ranker_artifacts_dir = original_dir
