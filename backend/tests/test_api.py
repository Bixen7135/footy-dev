import csv
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlmodel import Session, select

import app.services as services_module
from app.database import create_db_and_tables, engine
from app.main import app
from app.bootstrap import ExportBootstrapService
from app.models import (
    Category,
    Product,
    ProductImage,
    ProductVariant,
    SourceCatalogItem,
    SourceProductLink,
    SyncStatus,
    User,
    UserRole,
)
from app.security import hash_password


client = TestClient(app)


def _write_csv(path: Path, headers: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def _ensure_admin() -> None:
    with Session(engine) as session:
        user = session.exec(select(User).where(User.email == "admin@footy.local")).first()
        if user:
            return
        user = User(
            email="admin@footy.local",
            password_hash=hash_password("adminpass"),
            full_name="Admin",
            role=UserRole.ADMIN,
        )
        session.add(user)
        session.commit()


def _admin_client() -> TestClient:
    _ensure_admin()
    response = client.post(
        "/auth/login",
        json={"email": "admin@footy.local", "password": "adminpass"},
    )
    assert response.status_code == 200
    return client


def _seed_product(slug: str, sku: str, stock: int = 10) -> tuple[int, int]:
    with Session(engine) as session:
        category = session.exec(select(Category).where(Category.slug == "test-cat")).first()
        if not category:
            category = Category(name="Test Category", slug="test-cat")
            session.add(category)
            session.commit()
            session.refresh(category)

        product = session.exec(select(Product).where(Product.slug == slug)).first()
        if not product:
            product = Product(
                slug=slug,
                name=f"Product {slug}",
                price=Decimal("42.00"),
                currency="KZT",
                category_id=category.id,
                is_active=True,
            )
            session.add(product)
            session.commit()
            session.refresh(product)

        variant = session.exec(select(ProductVariant).where(ProductVariant.sku == sku)).first()
        if not variant:
            variant = ProductVariant(product_id=product.id, sku=sku, stock_quantity=stock, is_active=True)
            session.add(variant)
        else:
            variant.stock_quantity = stock
            variant.is_active = True
            session.add(variant)
        session.commit()
        session.refresh(variant)

        return product.id, variant.id


def test_auth_refresh_lifecycle_api():
    create_db_and_tables()
    email = f"user-{uuid4().hex[:8]}@example.com"

    reg = client.post(
        "/auth/register",
        json={
            "email": email,
            "password": "secret123",
            "full_name": "Auth User",
        },
    )
    assert reg.status_code == 200

    login = client.post("/auth/login", json={"email": email, "password": "secret123"})
    assert login.status_code == 200

    me_before = client.get("/auth/me")
    assert me_before.status_code == 200

    refresh = client.post("/auth/refresh")
    assert refresh.status_code == 200

    me_after = client.get("/auth/me")
    assert me_after.status_code == 200
    assert me_after.json()["email"] == email

    logout = client.post("/auth/logout")
    assert logout.status_code == 200

    me_logged_out = client.get("/auth/me")
    assert me_logged_out.status_code == 401


def test_import_map_sync_flow_api():
    create_db_and_tables()
    admin = _admin_client()

    with Session(engine) as session:
        category = session.exec(select(Category).where(Category.slug == "boots")).first()
        if not category:
            category = Category(name="Boots", slug="boots")
            session.add(category)
            session.commit()
            session.refresh(category)

    import_payload = {
        "source_system": "supplier_api",
        "items": [
            {
                "source_system": "supplier_api",
                "source_product_id": "api-1",
                "source_url": "https://supplier.local/item/api-1",
                "title": "API Boot",
                "price": "120.00",
                "currency": "KZT",
                "category_path": "footwear/boots",
                "images": ["https://img.local/api-1.jpg"],
                "last_seen_at": datetime.utcnow().isoformat(),
            }
        ],
    }
    run = admin.post("/admin/imports/run", json=import_payload)
    assert run.status_code == 200

    source_items = admin.get("/admin/source-products")
    assert source_items.status_code == 200
    source_item = next(item for item in source_items.json() if item["source_product_id"] == "api-1")

    with Session(engine) as session:
        category = session.exec(select(Category).where(Category.slug == "boots")).first()

    mapping = admin.post(
        f"/admin/source-products/{source_item['id']}/map",
        json={
            "source_system": "supplier_api",
            "source_category_key": "footwear/boots",
            "internal_category_id": category.id,
        },
    )
    assert mapping.status_code == 200

    sync = admin.post("/admin/imports/sync-products")
    assert sync.status_code == 200

    products = client.get("/products")
    assert products.status_code == 200
    assert any(p["name"] == "API Boot" for p in products.json())


def test_products_support_category_ids_and_legacy_category_id_api():
    create_db_and_tables()

    with Session(engine) as session:
        category_a = Category(name=f"MultiCat A {uuid4().hex[:6]}", slug=f"multi-a-{uuid4().hex[:6]}")
        category_b = Category(name=f"MultiCat B {uuid4().hex[:6]}", slug=f"multi-b-{uuid4().hex[:6]}")
        category_c = Category(name=f"MultiCat C {uuid4().hex[:6]}", slug=f"multi-c-{uuid4().hex[:6]}")
        session.add(category_a)
        session.add(category_b)
        session.add(category_c)
        session.commit()
        session.refresh(category_a)
        session.refresh(category_b)
        session.refresh(category_c)

        product_a = Product(
            slug=f"multi-cat-a-{uuid4().hex[:6]}",
            name="MultiCat Alpha",
            price=Decimal("10.00"),
            currency="KZT",
            category_id=category_a.id,
            is_active=True,
        )
        product_b = Product(
            slug=f"multi-cat-b-{uuid4().hex[:6]}",
            name="MultiCat Beta",
            price=Decimal("20.00"),
            currency="KZT",
            category_id=category_b.id,
            is_active=True,
        )
        product_c = Product(
            slug=f"multi-cat-c-{uuid4().hex[:6]}",
            name="MultiCat Gamma",
            price=Decimal("30.00"),
            currency="KZT",
            category_id=category_c.id,
            is_active=True,
        )
        session.add(product_a)
        session.add(product_b)
        session.add(product_c)
        session.commit()
        session.refresh(product_a)
        session.refresh(product_b)
        session.refresh(product_c)

        category_a_id = category_a.id
        category_b_id = category_b.id
        category_c_id = category_c.id
        product_a_id = product_a.id
        product_b_id = product_b.id
        product_c_id = product_c.id

    multiple = client.get(f"/products?search=MultiCat&category_ids={category_a_id},{category_b_id}")
    assert multiple.status_code == 200
    multiple_ids = {row["id"] for row in multiple.json()}
    assert product_a_id in multiple_ids
    assert product_b_id in multiple_ids
    assert product_c_id not in multiple_ids

    soft_parsing = client.get(f"/products?search=MultiCat&category_ids=bad,,{category_b_id},{category_b_id}")
    assert soft_parsing.status_code == 200
    soft_ids = {row["id"] for row in soft_parsing.json()}
    assert product_b_id in soft_ids
    assert product_a_id not in soft_ids

    union_filtered = client.get(
        f"/products?search=MultiCat&category_id={category_c_id}&category_ids={category_a_id}&max_price=20"
    )
    assert union_filtered.status_code == 200
    union_ids = {row["id"] for row in union_filtered.json()}
    assert product_a_id in union_ids
    assert product_b_id not in union_ids
    assert product_c_id not in union_ids


def test_product_endpoints_include_image_url_api():
    create_db_and_tables()
    suffix = uuid4().hex[:8]
    primary_url = f"https://img.local/{suffix}-primary.jpg"
    similar_url = f"https://img.local/{suffix}-similar.jpg"

    with Session(engine) as session:
        category = Category(name=f"Image Cat {suffix}", slug=f"image-cat-{suffix}")
        session.add(category)
        session.commit()
        session.refresh(category)

        main_product = Product(
            slug=f"image-main-{suffix}",
            name=f"Image Main {suffix}",
            price=Decimal("123.00"),
            currency="KZT",
            category_id=category.id,
            is_active=True,
        )
        similar_product = Product(
            slug=f"image-similar-{suffix}",
            name=f"Image Similar {suffix}",
            price=Decimal("77.00"),
            currency="KZT",
            category_id=category.id,
            is_active=True,
        )
        session.add(main_product)
        session.add(similar_product)
        session.commit()
        session.refresh(main_product)
        session.refresh(similar_product)

        session.add(
            ProductImage(
                product_id=main_product.id,
                source_url=f"https://img.local/{suffix}-secondary.jpg",
                sort_order=2,
                is_primary=False,
            )
        )
        session.add(
            ProductImage(
                product_id=main_product.id,
                source_url=primary_url,
                sort_order=1,
                is_primary=True,
            )
        )
        session.add(
            ProductImage(
                product_id=similar_product.id,
                source_url=similar_url,
                sort_order=0,
                is_primary=True,
            )
        )
        session.commit()

        main_id = main_product.id
        main_slug = main_product.slug
        similar_id = similar_product.id

    products = client.get(f"/products?search={suffix}")
    assert products.status_code == 200
    main_row = next(row for row in products.json() if row["id"] == main_id)
    assert main_row["image_url"] == primary_url

    detail = client.get(f"/products/{main_slug}")
    assert detail.status_code == 200
    assert detail.json()["image_url"] == primary_url
    assert detail.json()["image_urls"][:2] == [primary_url, f"https://img.local/{suffix}-secondary.jpg"]

    catalog = client.get(f"/catalog/products?search={suffix}&limit=10&offset=0")
    assert catalog.status_code == 200
    catalog_main = next(row for row in catalog.json()["items"] if row["id"] == main_id)
    assert catalog_main["image_url"] == primary_url
    assert catalog_main["image_urls"] == []

    similar = client.get(f"/products/{main_id}/similar")
    assert similar.status_code == 200
    similar_row = next(row for row in similar.json() if row["id"] == similar_id)
    assert similar_row["image_url"] == similar_url


def test_refresh_primary_images_job_updates_catalog_and_similar_api(monkeypatch):
    create_db_and_tables()
    suffix = uuid4().hex[:8]
    expected_main = f"https://cdn.intertop.com/load/mp-main-{suffix}/big/MAIN.jpg"
    expected_similar = f"https://cdn.intertop.com/load/mp-sim-{suffix}/big/MAIN.jpg"

    with Session(engine) as session:
        category = Category(name=f"Main Preview Cat {suffix}", slug=f"main-preview-cat-{suffix}")
        session.add(category)
        session.commit()
        session.refresh(category)

        main_product = Product(
            slug=f"main-preview-{suffix}",
            name=f"Main Preview {suffix}",
            price=Decimal("100.00"),
            currency="KZT",
            category_id=category.id,
            is_active=True,
        )
        similar_product = Product(
            slug=f"main-preview-sim-{suffix}",
            name=f"Main Preview Similar {suffix}",
            price=Decimal("90.00"),
            currency="KZT",
            category_id=category.id,
            is_active=True,
        )
        session.add(main_product)
        session.add(similar_product)
        session.commit()
        session.refresh(main_product)
        session.refresh(similar_product)

        session.add(
            ProductImage(
                product_id=main_product.id,
                source_url=f"https://cdn.intertop.com/load/mp-main-{suffix}/small/2.jpg/",
                sort_order=0,
                is_primary=True,
            )
        )
        session.add(
            ProductImage(
                product_id=similar_product.id,
                source_url=f"https://cdn.intertop.com/load/mp-sim-{suffix}/small/2.jpg/",
                sort_order=0,
                is_primary=True,
            )
        )
        session.commit()

        main_id = main_product.id
        similar_id = similar_product.id

    def fake_validate(url: str, timeout_seconds: float = 4.0) -> bool:
        del timeout_seconds
        return url in {expected_main, expected_similar}

    monkeypatch.setattr(services_module, "_validate_remote_image_url", fake_validate)

    admin = _admin_client()
    job_run = admin.post("/admin/jobs/refresh_product_primary_images_job/run")
    assert job_run.status_code == 200
    details = job_run.json()["details_json"]
    assert details == {
        "candidates_total": 2,
        "validated_ok": 2,
        "updated_products": 2,
        "skipped_no_main": 0,
        "errors": 0,
    }

    products = client.get(f"/products?search={suffix}")
    assert products.status_code == 200
    products_rows = {row["id"]: row for row in products.json()}
    assert products_rows[main_id]["image_url"] == expected_main
    assert products_rows[similar_id]["image_url"] == expected_similar

    catalog = client.get(f"/catalog/products?search={suffix}&limit=10&offset=0")
    assert catalog.status_code == 200
    catalog_rows = {row["id"]: row for row in catalog.json()["items"]}
    assert catalog_rows[main_id]["image_url"] == expected_main
    assert catalog_rows[similar_id]["image_url"] == expected_similar

    similar = client.get(f"/products/{main_id}/similar")
    assert similar.status_code == 200
    similar_row = next(row for row in similar.json() if row["id"] == similar_id)
    assert similar_row["image_url"] == expected_similar


def test_product_detail_sanitizes_mixed_gallery_images_api():
    create_db_and_tables()
    suffix = uuid4().hex[:8]

    with Session(engine) as session:
        category = Category(name=f"Gallery Cat {suffix}", slug=f"gallery-cat-{suffix}")
        session.add(category)
        session.commit()
        session.refresh(category)

        product = Product(
            slug=f"gallery-main-{suffix}",
            name=f"Gallery Main {suffix}",
            price=Decimal("149.00"),
            currency="KZT",
            category_id=category.id,
            is_active=True,
        )
        session.add(product)
        session.commit()
        session.refresh(product)

        session.add(
            ProductImage(
                product_id=product.id,
                source_url="https://cdn.intertop.com/load/mp111111/small/MAIN.jpg/",
                sort_order=0,
                is_primary=True,
            )
        )
        session.add(
            ProductImage(
                product_id=product.id,
                source_url="https://cdn.intertop.com/load/mp111111/big/MAIN.jpg/",
                sort_order=1,
                is_primary=False,
            )
        )
        session.add(
            ProductImage(
                product_id=product.id,
                source_url="https://cdn.intertop.com/load/mp111111/small/2.jpg/",
                sort_order=2,
                is_primary=False,
            )
        )
        session.add(
            ProductImage(
                product_id=product.id,
                source_url="https://cdn.intertop.com/load/mp111111/big/2.jpg/",
                sort_order=3,
                is_primary=False,
            )
        )
        session.add(
            ProductImage(
                product_id=product.id,
                source_url="https://cdn.intertop.com/load/mp999999/small/MAIN.jpg/",
                sort_order=4,
                is_primary=False,
            )
        )
        session.commit()

        product_slug = product.slug

    response = client.get(f"/products/{product_slug}")
    assert response.status_code == 200
    body = response.json()

    assert body["image_url"] == "https://cdn.intertop.com/load/mp111111/big/MAIN.jpg"
    assert body["image_urls"] == [
        "https://cdn.intertop.com/load/mp111111/big/MAIN.jpg",
        "https://cdn.intertop.com/load/mp111111/big/2.jpg",
    ]


def test_product_detail_autogenerates_description_and_details_api():
    create_db_and_tables()
    suffix = uuid4().hex[:8]

    with Session(engine) as session:
        category = Category(name=f"Detail Cat {suffix}", slug=f"detail-cat-{suffix}")
        session.add(category)
        session.commit()
        session.refresh(category)

        product = Product(
            slug=f"detail-main-{suffix}",
            name=f"Detail Main {suffix}",
            brand_name="DetailBrand",
            price=Decimal("222.00"),
            currency="KZT",
            category_id=category.id,
            gender="women",
            is_active=True,
            description=None,
        )
        session.add(product)
        session.commit()
        session.refresh(product)

        session.add(
            ProductVariant(
                product_id=product.id,
                sku=f"detail-size-37-{suffix}",
                size="37",
                color="White - buy online | INTERTOP",
                stock_quantity=5,
                is_active=True,
            )
        )
        session.add(
            ProductVariant(
                product_id=product.id,
                sku=f"detail-size-38-{suffix}",
                size="38",
                color="White - buy online | INTERTOP",
                stock_quantity=5,
                is_active=True,
            )
        )
        source_item = SourceCatalogItem(
            source_system="intertop_kz",
            source_product_id=f"src-{suffix}",
            source_url=f"https://intertop.kz/item/{suffix}",
            source_title=f"Detail Main {suffix}",
            source_category_path="shoes",
            source_currency="KZT",
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
        product_slug = product.slug

    response = client.get(f"/products/{product_slug}")
    assert response.status_code == 200
    body = response.json()

    assert body["description"]
    assert "Footy.kz" in body["description"]
    assert body["details"]["category_name"] == f"Detail Cat {suffix}"
    assert body["details"]["gender_key"] == "women"
    assert body["details"]["source_system"] == "intertop_kz"
    assert body["details"]["source_product_id"] == f"src-{suffix}"
    assert body["details"]["source_url"] == f"https://intertop.kz/item/{suffix}"
    assert body["details"]["sizes"] == ["37", "38"]
    size_option_map = {row["size"]: row for row in body["size_options"]}
    assert set(size_option_map.keys()) == {"37", "38"}
    assert size_option_map["37"]["in_stock"] is True
    assert size_option_map["38"]["in_stock"] is True
    assert size_option_map["37"]["color_key"] == "white"
    assert size_option_map["38"]["color_key"] == "white"


def test_catalog_products_endpoint_total_and_facets_api():
    create_db_and_tables()
    suffix = uuid4().hex[:6]

    with Session(engine) as session:
        category_a = Category(name=f"Facet A {suffix}", slug=f"facet-a-{suffix}")
        category_b = Category(name=f"Facet B {suffix}", slug=f"facet-b-{suffix}")
        session.add(category_a)
        session.add(category_b)
        session.commit()
        session.refresh(category_a)
        session.refresh(category_b)

        product_a = Product(
            slug=f"facet-prod-a-{suffix}",
            name=f"Facet Product Alpha {suffix}",
            price=Decimal("11.00"),
            currency="KZT",
            category_id=category_a.id,
            is_active=True,
        )
        product_b = Product(
            slug=f"facet-prod-b-{suffix}",
            name=f"Facet Product Beta {suffix}",
            price=Decimal("12.00"),
            currency="KZT",
            category_id=category_b.id,
            is_active=True,
        )
        session.add(product_a)
        session.add(product_b)
        session.commit()
        session.refresh(product_a)
        session.refresh(product_b)

        variant_a = ProductVariant(
            product_id=product_a.id,
            sku=f"facet-a-{suffix}",
            stock_quantity=5,
            is_active=True,
        )
        variant_b = ProductVariant(
            product_id=product_b.id,
            sku=f"facet-b-{suffix}",
            stock_quantity=0,
            is_active=True,
        )
        session.add(variant_a)
        session.add(variant_b)
        session.commit()

        category_a_id = category_a.id
        category_b_id = category_b.id
        product_a_id = product_a.id
        product_b_id = product_b.id

    response = client.get(f"/catalog/products?search={suffix}&in_stock_only=true")
    assert response.status_code == 200
    body = response.json()

    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["id"] == product_a_id
    assert body["applied_filters"]["in_stock_only"] is True

    facet_map = {row["id"]: row["count"] for row in body["facets"]["categories"]}
    assert facet_map[category_a_id] == 1
    assert category_b_id not in facet_map

    filtered = client.get(f"/catalog/products?search={suffix}&category_ids={category_b_id}")
    assert filtered.status_code == 200
    filtered_body = filtered.json()
    assert filtered_body["total"] == 1
    assert filtered_body["items"][0]["id"] == product_b_id
    filtered_facets = {row["id"]: row["count"] for row in filtered_body["facets"]["categories"]}
    assert filtered_facets[category_a_id] == 1
    assert filtered_facets[category_b_id] == 1


def test_categories_endpoint_hides_empty_categories_api():
    create_db_and_tables()
    suffix = uuid4().hex[:6]

    with Session(engine) as session:
        visible_category = Category(name=f"Visible Category {suffix}", slug=f"visible-category-{suffix}", is_active=True)
        empty_category = Category(name=f"Empty Category {suffix}", slug=f"empty-category-{suffix}", is_active=True)
        inactive_category = Category(name=f"Inactive Category {suffix}", slug=f"inactive-category-{suffix}", is_active=False)
        session.add(visible_category)
        session.add(empty_category)
        session.add(inactive_category)
        session.commit()
        session.refresh(visible_category)
        session.refresh(empty_category)
        session.refresh(inactive_category)

        session.add(
            Product(
                slug=f"visible-product-{suffix}",
                name=f"Visible Product {suffix}",
                price=Decimal("99.00"),
                currency="KZT",
                category_id=visible_category.id,
                is_active=True,
            )
        )
        session.add(
            Product(
                slug=f"inactive-category-product-{suffix}",
                name=f"Inactive Category Product {suffix}",
                price=Decimal("88.00"),
                currency="KZT",
                category_id=inactive_category.id,
                is_active=True,
            )
        )
        session.commit()

        visible_slug = visible_category.slug
        empty_slug = empty_category.slug
        inactive_slug = inactive_category.slug

    response = client.get("/categories")
    assert response.status_code == 200
    slugs = {row["slug"] for row in response.json()}

    assert visible_slug in slugs
    assert empty_slug not in slugs
    assert inactive_slug not in slugs


def test_product_out_includes_colors_api():
    create_db_and_tables()
    suffix = uuid4().hex[:6]

    with Session(engine) as session:
        category = Category(name=f"Colors {suffix}", slug=f"colors-{suffix}")
        session.add(category)
        session.commit()
        session.refresh(category)

        product = Product(
            slug=f"color-main-{suffix}",
            name=f"Color Model {suffix}",
            brand_name="ColorBrand",
            price=Decimal("100.00"),
            currency="KZT",
            category_id=category.id,
            is_active=True,
        )
        session.add(product)
        session.commit()
        session.refresh(product)

        black_out_of_stock = ProductVariant(
            product_id=product.id,
            sku=f"color-black-oos-{suffix}",
            color="Black - buy online | INTERTOP",
            stock_quantity=0,
            is_active=True,
        )
        black_in_stock = ProductVariant(
            product_id=product.id,
            sku=f"color-black-stock-{suffix}",
            color="Black - buy online | INTERTOP",
            stock_quantity=3,
            is_active=True,
        )
        white_in_stock = ProductVariant(
            product_id=product.id,
            sku=f"color-white-sku-{suffix}",
            color="White - buy online | INTERTOP",
            stock_quantity=2,
            is_active=True,
        )
        session.add(black_out_of_stock)
        session.add(black_in_stock)
        session.add(white_in_stock)
        session.commit()
        session.refresh(black_out_of_stock)
        session.refresh(black_in_stock)
        session.refresh(white_in_stock)
        black_in_stock_id = black_in_stock.id
        white_in_stock_id = white_in_stock.id

        session.add(
            ProductVariant(
                product_id=product.id,
                sku=f"color-size-sku-{suffix}",
                color=None,
                stock_quantity=3,
                is_active=True,
            )
        )
        session.commit()

    list_response = client.get(f"/products?search={suffix}")
    assert list_response.status_code == 200
    list_row = next(row for row in list_response.json() if row["slug"] == f"color-main-{suffix}")
    list_keys = {row["key"] for row in list_row["colors"]}
    assert list_keys == {"black", "white"}

    detail_response = client.get(f"/products/color-main-{suffix}")
    assert detail_response.status_code == 200
    detail_body = detail_response.json()
    detail_keys = {row["key"] for row in detail_body["colors"]}
    assert detail_keys == {"black", "white"}
    option_map = {row["key"]: row for row in detail_body["color_options"]}
    assert set(option_map.keys()) == {"black", "white"}
    assert option_map["black"]["slug"] == f"color-main-{suffix}"
    assert option_map["white"]["slug"] == f"color-main-{suffix}"
    assert option_map["black"]["variant_id"] == black_in_stock_id
    assert option_map["white"]["variant_id"] == white_in_stock_id
    assert option_map["black"]["is_current"] is True
    size_option_map = {row["color_key"]: row for row in detail_body["size_options"]}
    assert {"black", "white"}.issubset(set(size_option_map.keys()))
    assert size_option_map["black"]["variant_id"] == black_in_stock_id
    assert size_option_map["black"]["size"] == "One size"
    assert size_option_map["black"]["in_stock"] is True
    assert size_option_map["white"]["variant_id"] == white_in_stock_id
    assert size_option_map["white"]["size"] == "One size"
    assert size_option_map["white"]["in_stock"] is True

    catalog_response = client.get(f"/catalog/products?search={suffix}&limit=24&offset=0")
    assert catalog_response.status_code == 200
    catalog_row = next(row for row in catalog_response.json()["items"] if row["slug"] == f"color-main-{suffix}")
    catalog_keys = {row["key"] for row in catalog_row["colors"]}
    assert catalog_keys == {"black", "white"}


def test_catalog_products_color_filter_api():
    create_db_and_tables()
    suffix = uuid4().hex[:6]

    with Session(engine) as session:
        category = Category(name=f"Color Filter {suffix}", slug=f"color-filter-{suffix}")
        session.add(category)
        session.commit()
        session.refresh(category)

        model_black = Product(
            slug=f"model-black-{suffix}",
            name=f"Filter Model {suffix}",
            brand_name="FilterBrand",
            price=Decimal("120.00"),
            currency="KZT",
            category_id=category.id,
            is_active=True,
        )
        model_white = Product(
            slug=f"model-white-{suffix}",
            name=f"Filter Model {suffix}",
            brand_name="FilterBrand",
            price=Decimal("121.00"),
            currency="KZT",
            category_id=category.id,
            is_active=True,
        )
        no_color = Product(
            slug=f"model-none-{suffix}",
            name=f"No Color Model {suffix}",
            brand_name="FilterBrand",
            price=Decimal("122.00"),
            currency="KZT",
            category_id=category.id,
            is_active=True,
        )
        session.add(model_black)
        session.add(model_white)
        session.add(no_color)
        session.commit()
        session.refresh(model_black)
        session.refresh(model_white)
        session.refresh(no_color)

        session.add(
            ProductVariant(
                product_id=model_black.id,
                sku=f"model-black-sku-{suffix}",
                color="Black - buy online | INTERTOP",
                stock_quantity=5,
                is_active=True,
            )
        )
        session.add(
            ProductVariant(
                product_id=model_white.id,
                sku=f"model-white-sku-{suffix}",
                color="White - buy online | INTERTOP",
                stock_quantity=5,
                is_active=True,
            )
        )
        session.add(
            ProductVariant(
                product_id=no_color.id,
                sku=f"model-none-sku-{suffix}",
                color=None,
                stock_quantity=5,
                is_active=True,
            )
        )
        session.commit()

    response = client.get(f"/catalog/products?search={suffix}&color_keys=black,unknown")
    assert response.status_code == 200
    body = response.json()
    slugs = {row["slug"] for row in body["items"]}

    assert body["total"] == 1
    assert slugs == {f"model-black-{suffix}"}
    assert body["applied_filters"]["color_keys"] == ["black"]

    other_response = client.get(f"/catalog/products?search={suffix}&color_keys=other")
    assert other_response.status_code == 200
    other_body = other_response.json()
    assert other_body["total"] == 1
    assert {row["slug"] for row in other_body["items"]} == {f"model-none-{suffix}"}
    assert other_body["applied_filters"]["color_keys"] == ["other"]

    all_response = client.get(f"/catalog/products?search={suffix}")
    assert all_response.status_code == 200
    all_body = all_response.json()
    all_facet_colors = {row["key"]: row["count"] for row in all_body["facets"]["colors"]}
    assert all_facet_colors["black"] == 1
    assert all_facet_colors["white"] == 1
    assert all_facet_colors["other"] == 1


def test_catalog_products_color_facets_api():
    create_db_and_tables()
    suffix = uuid4().hex[:6]

    with Session(engine) as session:
        category = Category(name=f"Color Facet {suffix}", slug=f"color-facet-{suffix}")
        session.add(category)
        session.commit()
        session.refresh(category)

        model_a_black = Product(
            slug=f"facet-a-black-{suffix}",
            name=f"Facet Model A {suffix}",
            brand_name="FacetBrand",
            price=Decimal("130.00"),
            currency="KZT",
            category_id=category.id,
            is_active=True,
        )
        model_a_white = Product(
            slug=f"facet-a-white-{suffix}",
            name=f"Facet Model A {suffix}",
            brand_name="FacetBrand",
            price=Decimal("131.00"),
            currency="KZT",
            category_id=category.id,
            is_active=True,
        )
        model_b_blue = Product(
            slug=f"facet-b-blue-{suffix}",
            name=f"Facet Model B {suffix}",
            brand_name="FacetBrand",
            price=Decimal("132.00"),
            currency="KZT",
            category_id=category.id,
            is_active=True,
        )
        session.add(model_a_black)
        session.add(model_a_white)
        session.add(model_b_blue)
        session.commit()
        session.refresh(model_a_black)
        session.refresh(model_a_white)
        session.refresh(model_b_blue)

        session.add(
            ProductVariant(
                product_id=model_a_black.id,
                sku=f"facet-a-black-sku-{suffix}",
                color="Black - buy online | INTERTOP",
                stock_quantity=1,
                is_active=True,
            )
        )
        session.add(
            ProductVariant(
                product_id=model_a_white.id,
                sku=f"facet-a-white-sku-{suffix}",
                color="White - buy online | INTERTOP",
                stock_quantity=1,
                is_active=True,
            )
        )
        session.add(
            ProductVariant(
                product_id=model_b_blue.id,
                sku=f"facet-b-blue-sku-{suffix}",
                color="Blue - buy online | INTERTOP",
                stock_quantity=1,
                is_active=True,
            )
        )
        session.commit()

    response = client.get(f"/catalog/products?search={suffix}&color_keys=black")
    assert response.status_code == 200
    body = response.json()

    facet_colors = {row["key"]: row["count"] for row in body["facets"]["colors"]}
    assert body["total"] == 1
    assert body["applied_filters"]["color_keys"] == ["black"]
    assert facet_colors["black"] == 1
    assert facet_colors["white"] == 1
    assert facet_colors["blue"] == 1


def test_catalog_products_category_facets_respect_color_filter_api():
    create_db_and_tables()
    suffix = uuid4().hex[:6]

    with Session(engine) as session:
        category_a = Category(name=f"Color Cat A {suffix}", slug=f"color-cat-a-{suffix}")
        category_b = Category(name=f"Color Cat B {suffix}", slug=f"color-cat-b-{suffix}")
        session.add(category_a)
        session.add(category_b)
        session.commit()
        session.refresh(category_a)
        session.refresh(category_b)

        product_a_black = Product(
            slug=f"color-cat-a-black-{suffix}",
            name=f"Color Category Product A {suffix}",
            brand_name="FacetBrand",
            price=Decimal("151.00"),
            currency="KZT",
            category_id=category_a.id,
            is_active=True,
        )
        product_a_white = Product(
            slug=f"color-cat-a-white-{suffix}",
            name=f"Color Category Product A White {suffix}",
            brand_name="FacetBrand",
            price=Decimal("152.00"),
            currency="KZT",
            category_id=category_a.id,
            is_active=True,
        )
        product_b_blue = Product(
            slug=f"color-cat-b-blue-{suffix}",
            name=f"Color Category Product B {suffix}",
            brand_name="FacetBrand",
            price=Decimal("153.00"),
            currency="KZT",
            category_id=category_b.id,
            is_active=True,
        )
        session.add(product_a_black)
        session.add(product_a_white)
        session.add(product_b_blue)
        session.commit()
        session.refresh(product_a_black)
        session.refresh(product_a_white)
        session.refresh(product_b_blue)

        session.add(
            ProductVariant(
                product_id=product_a_black.id,
                sku=f"color-cat-a-black-sku-{suffix}",
                color="Black - buy online | INTERTOP",
                stock_quantity=4,
                is_active=True,
            )
        )
        session.add(
            ProductVariant(
                product_id=product_a_white.id,
                sku=f"color-cat-a-white-sku-{suffix}",
                color="White - buy online | INTERTOP",
                stock_quantity=4,
                is_active=True,
            )
        )
        session.add(
            ProductVariant(
                product_id=product_b_blue.id,
                sku=f"color-cat-b-blue-sku-{suffix}",
                color="Blue - buy online | INTERTOP",
                stock_quantity=4,
                is_active=True,
            )
        )
        session.commit()

        category_a_id = category_a.id
        category_b_id = category_b.id

    response = client.get(f"/catalog/products?search={suffix}&color_keys=black")
    assert response.status_code == 200
    body = response.json()

    assert body["total"] == 1
    assert {row["slug"] for row in body["items"]} == {f"color-cat-a-black-{suffix}"}
    facet_categories = {row["id"]: row["count"] for row in body["facets"]["categories"]}
    assert facet_categories[category_a_id] == 1
    assert category_b_id not in facet_categories


def test_catalog_products_category_facets_disjunctive_with_category_and_color_api():
    create_db_and_tables()
    suffix = uuid4().hex[:6]

    with Session(engine) as session:
        category_a = Category(name=f"Combo Cat A {suffix}", slug=f"combo-cat-a-{suffix}")
        category_b = Category(name=f"Combo Cat B {suffix}", slug=f"combo-cat-b-{suffix}")
        session.add(category_a)
        session.add(category_b)
        session.commit()
        session.refresh(category_a)
        session.refresh(category_b)

        product_a_black = Product(
            slug=f"combo-a-black-{suffix}",
            name=f"Combo Product A Black {suffix}",
            brand_name="FacetBrand",
            price=Decimal("161.00"),
            currency="KZT",
            category_id=category_a.id,
            is_active=True,
        )
        product_b_black = Product(
            slug=f"combo-b-black-{suffix}",
            name=f"Combo Product B Black {suffix}",
            brand_name="FacetBrand",
            price=Decimal("162.00"),
            currency="KZT",
            category_id=category_b.id,
            is_active=True,
        )
        product_b_blue = Product(
            slug=f"combo-b-blue-{suffix}",
            name=f"Combo Product B Blue {suffix}",
            brand_name="FacetBrand",
            price=Decimal("163.00"),
            currency="KZT",
            category_id=category_b.id,
            is_active=True,
        )
        session.add(product_a_black)
        session.add(product_b_black)
        session.add(product_b_blue)
        session.commit()
        session.refresh(product_a_black)
        session.refresh(product_b_black)
        session.refresh(product_b_blue)

        session.add(
            ProductVariant(
                product_id=product_a_black.id,
                sku=f"combo-a-black-sku-{suffix}",
                color="Black - buy online | INTERTOP",
                stock_quantity=3,
                is_active=True,
            )
        )
        session.add(
            ProductVariant(
                product_id=product_b_black.id,
                sku=f"combo-b-black-sku-{suffix}",
                color="Black - buy online | INTERTOP",
                stock_quantity=3,
                is_active=True,
            )
        )
        session.add(
            ProductVariant(
                product_id=product_b_blue.id,
                sku=f"combo-b-blue-sku-{suffix}",
                color="Blue - buy online | INTERTOP",
                stock_quantity=3,
                is_active=True,
            )
        )
        session.commit()

        category_a_id = category_a.id
        category_b_id = category_b.id

    response = client.get(
        f"/catalog/products?search={suffix}&category_ids={category_a_id}&color_keys=black"
    )
    assert response.status_code == 200
    body = response.json()

    assert body["total"] == 1
    assert {row["slug"] for row in body["items"]} == {f"combo-a-black-{suffix}"}
    facet_categories = {row["id"]: row["count"] for row in body["facets"]["categories"]}
    assert facet_categories[category_a_id] == 1
    assert facet_categories[category_b_id] == 1


def test_catalog_products_gender_filter_and_facets_api():
    create_db_and_tables()
    suffix = uuid4().hex[:6]

    with Session(engine) as session:
        category = Category(name=f"Gender Facet {suffix}", slug=f"gender-facet-{suffix}")
        session.add(category)
        session.commit()
        session.refresh(category)

        men_product = Product(
            slug=f"gender-men-{suffix}",
            name=f"Gender Model Men {suffix}",
            brand_name="GenderBrand",
            price=Decimal("141.00"),
            currency="KZT",
            category_id=category.id,
            gender="men",
            is_active=True,
        )
        women_product = Product(
            slug=f"gender-women-{suffix}",
            name=f"Gender Model Women {suffix}",
            brand_name="GenderBrand",
            price=Decimal("142.00"),
            currency="KZT",
            category_id=category.id,
            gender="women",
            is_active=True,
        )
        unknown_gender_product = Product(
            slug=f"gender-unknown-{suffix}",
            name=f"Gender Model Unknown {suffix}",
            brand_name="GenderBrand",
            price=Decimal("143.00"),
            currency="KZT",
            category_id=category.id,
            gender=None,
            is_active=True,
        )
        session.add(men_product)
        session.add(women_product)
        session.add(unknown_gender_product)
        session.commit()
        session.refresh(men_product)
        session.refresh(women_product)
        session.refresh(unknown_gender_product)

        session.add(
            ProductVariant(
                product_id=men_product.id,
                sku=f"gender-men-sku-{suffix}",
                color="Black - buy online | INTERTOP",
                stock_quantity=2,
                is_active=True,
            )
        )
        session.add(
            ProductVariant(
                product_id=women_product.id,
                sku=f"gender-women-sku-{suffix}",
                color="Blue - buy online | INTERTOP",
                stock_quantity=2,
                is_active=True,
            )
        )
        session.add(
            ProductVariant(
                product_id=unknown_gender_product.id,
                sku=f"gender-unknown-sku-{suffix}",
                color="Blue - buy online | INTERTOP",
                stock_quantity=2,
                is_active=True,
            )
        )
        session.commit()

    catalog_response = client.get(f"/catalog/products?search={suffix}&gender_keys=women")
    assert catalog_response.status_code == 200
    catalog_body = catalog_response.json()
    assert catalog_body["total"] == 1
    assert {row["slug"] for row in catalog_body["items"]} == {f"gender-women-{suffix}"}
    assert catalog_body["applied_filters"]["gender_keys"] == ["women"]
    gender_facets = {row["key"]: row["count"] for row in catalog_body["facets"]["genders"]}
    assert gender_facets["men"] == 1
    assert gender_facets["women"] == 1

    products_response = client.get(f"/products?search={suffix}&gender_keys=men")
    assert products_response.status_code == 200
    assert {row["slug"] for row in products_response.json()} == {f"gender-men-{suffix}"}

    combo_response = client.get(f"/catalog/products?search={suffix}&gender_keys=women&color_keys=blue")
    assert combo_response.status_code == 200
    combo_body = combo_response.json()
    assert combo_body["total"] == 1
    assert {row["slug"] for row in combo_body["items"]} == {f"gender-women-{suffix}"}
    combo_gender_facets = {row["key"]: row["count"] for row in combo_body["facets"]["genders"]}
    assert combo_gender_facets["women"] == 1
    assert "men" not in combo_gender_facets


def test_catalog_products_size_filter_requires_all_selected_sizes_api():
    create_db_and_tables()
    suffix = uuid4().hex[:6]

    with Session(engine) as session:
        category = Category(name=f"Size Match {suffix}", slug=f"size-match-{suffix}")
        session.add(category)
        session.commit()
        session.refresh(category)

        product_all = Product(
            slug=f"size-all-{suffix}",
            name=f"Size All Product {suffix}",
            brand_name="SizeBrand",
            price=Decimal("201.00"),
            currency="KZT",
            category_id=category.id,
            is_active=True,
        )
        product_only_41 = Product(
            slug=f"size-only-41-{suffix}",
            name=f"Size Only 41 Product {suffix}",
            brand_name="SizeBrand",
            price=Decimal("202.00"),
            currency="KZT",
            category_id=category.id,
            is_active=True,
        )
        product_only_42 = Product(
            slug=f"size-only-42-{suffix}",
            name=f"Size Only 42 Product {suffix}",
            brand_name="SizeBrand",
            price=Decimal("203.00"),
            currency="KZT",
            category_id=category.id,
            is_active=True,
        )
        session.add(product_all)
        session.add(product_only_41)
        session.add(product_only_42)
        session.commit()
        session.refresh(product_all)
        session.refresh(product_only_41)
        session.refresh(product_only_42)

        session.add(
            ProductVariant(
                product_id=product_all.id,
                sku=f"size-all-41-{suffix}",
                size="41",
                stock_quantity=5,
                is_active=True,
            )
        )
        session.add(
            ProductVariant(
                product_id=product_all.id,
                sku=f"size-all-42-{suffix}",
                size="42",
                stock_quantity=5,
                is_active=True,
            )
        )
        session.add(
            ProductVariant(
                product_id=product_only_41.id,
                sku=f"size-only41-{suffix}",
                size="41",
                stock_quantity=5,
                is_active=True,
            )
        )
        session.add(
            ProductVariant(
                product_id=product_only_42.id,
                sku=f"size-only42-{suffix}",
                size="42",
                stock_quantity=5,
                is_active=True,
            )
        )
        session.commit()

    response = client.get(f"/catalog/products?search={suffix}&size_keys=41,42,41")
    assert response.status_code == 200
    body = response.json()

    assert body["total"] == 1
    assert {row["slug"] for row in body["items"]} == {f"size-all-{suffix}"}
    assert len(body["applied_filters"]["size_keys"]) == 2
    assert set(body["applied_filters"]["size_keys"]) == {"41", "42"}


def test_catalog_products_size_filter_respects_in_stock_only_api():
    create_db_and_tables()
    suffix = uuid4().hex[:6]

    with Session(engine) as session:
        category = Category(name=f"Size Stock {suffix}", slug=f"size-stock-{suffix}")
        session.add(category)
        session.commit()
        session.refresh(category)

        oos_product = Product(
            slug=f"size-oos-{suffix}",
            name=f"Size OOS Product {suffix}",
            brand_name="SizeBrand",
            price=Decimal("211.00"),
            currency="KZT",
            category_id=category.id,
            is_active=True,
        )
        in_stock_product = Product(
            slug=f"size-stocked-{suffix}",
            name=f"Size Stocked Product {suffix}",
            brand_name="SizeBrand",
            price=Decimal("212.00"),
            currency="KZT",
            category_id=category.id,
            is_active=True,
        )
        session.add(oos_product)
        session.add(in_stock_product)
        session.commit()
        session.refresh(oos_product)
        session.refresh(in_stock_product)

        session.add(
            ProductVariant(
                product_id=oos_product.id,
                sku=f"size-oos-41-{suffix}",
                size="41",
                stock_quantity=0,
                is_active=True,
            )
        )
        session.add(
            ProductVariant(
                product_id=in_stock_product.id,
                sku=f"size-stocked-41-{suffix}",
                size="41",
                stock_quantity=3,
                is_active=True,
            )
        )
        session.commit()

    all_sizes = client.get(f"/catalog/products?search={suffix}&size_keys=41")
    assert all_sizes.status_code == 200
    all_body = all_sizes.json()
    assert all_body["total"] == 2
    assert {row["slug"] for row in all_body["items"]} == {f"size-oos-{suffix}", f"size-stocked-{suffix}"}

    in_stock_only = client.get(f"/catalog/products?search={suffix}&size_keys=41&in_stock_only=true")
    assert in_stock_only.status_code == 200
    in_stock_body = in_stock_only.json()
    assert in_stock_body["total"] == 1
    assert {row["slug"] for row in in_stock_body["items"]} == {f"size-stocked-{suffix}"}


def test_catalog_products_size_facets_and_related_facets_api():
    create_db_and_tables()
    suffix = uuid4().hex[:6]

    with Session(engine) as session:
        category_a = Category(name=f"Size Cat A {suffix}", slug=f"size-cat-a-{suffix}")
        category_b = Category(name=f"Size Cat B {suffix}", slug=f"size-cat-b-{suffix}")
        session.add(category_a)
        session.add(category_b)
        session.commit()
        session.refresh(category_a)
        session.refresh(category_b)

        product_41 = Product(
            slug=f"size-facet-41-{suffix}",
            name=f"Size Facet Product 41 {suffix}",
            brand_name="FacetBrand",
            price=Decimal("221.00"),
            currency="KZT",
            category_id=category_a.id,
            gender="men",
            is_active=True,
        )
        product_42 = Product(
            slug=f"size-facet-42-{suffix}",
            name=f"Size Facet Product 42 {suffix}",
            brand_name="FacetBrand",
            price=Decimal("222.00"),
            currency="KZT",
            category_id=category_b.id,
            gender="women",
            is_active=True,
        )
        session.add(product_41)
        session.add(product_42)
        session.commit()
        session.refresh(product_41)
        session.refresh(product_42)

        session.add(
            ProductVariant(
                product_id=product_41.id,
                sku=f"size-facet-41-sku-{suffix}",
                size="41",
                color="Black - buy online | INTERTOP",
                stock_quantity=4,
                is_active=True,
            )
        )
        session.add(
            ProductVariant(
                product_id=product_42.id,
                sku=f"size-facet-42-sku-{suffix}",
                size="42",
                color="Blue - buy online | INTERTOP",
                stock_quantity=4,
                is_active=True,
            )
        )
        session.commit()

        category_a_id = category_a.id
        category_b_id = category_b.id

    response = client.get(f"/catalog/products?search={suffix}&size_keys=41")
    assert response.status_code == 200
    body = response.json()

    assert body["total"] == 1
    assert {row["slug"] for row in body["items"]} == {f"size-facet-41-{suffix}"}
    assert body["applied_filters"]["size_keys"] == ["41"]

    size_facets = {row["key"]: row["count"] for row in body["facets"]["sizes"]}
    assert size_facets["41"] == 1
    assert size_facets["42"] == 1

    color_facets = {row["key"]: row["count"] for row in body["facets"]["colors"]}
    assert color_facets["black"] == 1
    assert "blue" not in color_facets

    category_facets = {row["id"]: row["count"] for row in body["facets"]["categories"]}
    assert category_facets[category_a_id] == 1
    assert category_b_id not in category_facets

    gender_facets = {row["key"]: row["count"] for row in body["facets"]["genders"]}
    assert gender_facets["men"] == 1
    assert "women" not in gender_facets


def test_catalog_products_ignores_invalid_size_keys_and_hides_invalid_size_facets_api():
    create_db_and_tables()
    suffix = uuid4().hex[:6]

    with Session(engine) as session:
        category = Category(name=f"Size Invalid {suffix}", slug=f"size-invalid-{suffix}")
        session.add(category)
        session.commit()
        session.refresh(category)

        valid_product = Product(
            slug=f"size-valid-{suffix}",
            name=f"Size Valid Product {suffix}",
            brand_name="SizeBrand",
            price=Decimal("231.00"),
            currency="KZT",
            category_id=category.id,
            is_active=True,
        )
        invalid_74_product = Product(
            slug=f"size-invalid-74-{suffix}",
            name=f"Size Invalid 74 Product {suffix}",
            brand_name="SizeBrand",
            price=Decimal("232.00"),
            currency="KZT",
            category_id=category.id,
            is_active=True,
        )
        invalid_99_product = Product(
            slug=f"size-invalid-99-{suffix}",
            name=f"Size Invalid 99 Product {suffix}",
            brand_name="SizeBrand",
            price=Decimal("233.00"),
            currency="KZT",
            category_id=category.id,
            is_active=True,
        )
        encoded_product = Product(
            slug=f"size-encoded-34-{suffix}",
            name=f"Size Encoded 34 Product {suffix}",
            brand_name="SizeBrand",
            price=Decimal("234.00"),
            currency="KZT",
            category_id=category.id,
            is_active=True,
        )
        session.add(valid_product)
        session.add(invalid_74_product)
        session.add(invalid_99_product)
        session.add(encoded_product)
        session.commit()
        session.refresh(valid_product)
        session.refresh(invalid_74_product)
        session.refresh(invalid_99_product)
        session.refresh(encoded_product)

        session.add(
            ProductVariant(
                product_id=valid_product.id,
                sku=f"size-valid-41-{suffix}",
                size="41",
                stock_quantity=5,
                is_active=True,
            )
        )
        session.add(
            ProductVariant(
                product_id=invalid_74_product.id,
                sku=f"size-invalid-74-sku-{suffix}",
                size="74",
                stock_quantity=5,
                is_active=True,
            )
        )
        session.add(
            ProductVariant(
                product_id=invalid_99_product.id,
                sku=f"size-invalid-99-sku-{suffix}",
                size="99",
                stock_quantity=5,
                is_active=True,
            )
        )
        session.add(
            ProductVariant(
                product_id=encoded_product.id,
                sku=f"size-encoded-34-sku-{suffix}",
                size="341487",
                stock_quantity=5,
                is_active=True,
            )
        )
        session.commit()

    all_response = client.get(f"/catalog/products?search={suffix}")
    assert all_response.status_code == 200
    all_body = all_response.json()
    assert all_body["total"] == 4
    assert {row["slug"] for row in all_body["items"]} == {
        f"size-valid-{suffix}",
        f"size-invalid-74-{suffix}",
        f"size-invalid-99-{suffix}",
        f"size-encoded-34-{suffix}",
    }
    size_facets = {row["key"]: row["count"] for row in all_body["facets"]["sizes"]}
    assert size_facets == {"34": 1, "41": 1}

    invalid_only = client.get(f"/catalog/products?search={suffix}&size_keys=99,abc")
    assert invalid_only.status_code == 200
    invalid_only_body = invalid_only.json()
    assert invalid_only_body["total"] == 4
    assert invalid_only_body["applied_filters"]["size_keys"] == []

    mixed = client.get(f"/catalog/products?search={suffix}&size_keys=34,99")
    assert mixed.status_code == 200
    mixed_body = mixed.json()
    assert mixed_body["total"] == 1
    assert {row["slug"] for row in mixed_body["items"]} == {f"size-encoded-34-{suffix}"}
    assert mixed_body["applied_filters"]["size_keys"] == ["34"]


def test_catalog_products_combined_filters_keep_disjunctive_facets_api():
    create_db_and_tables()
    suffix = uuid4().hex[:6]

    with Session(engine) as session:
        category_a = Category(name=f"Combo Mix Cat A {suffix}", slug=f"combo-mix-cat-a-{suffix}")
        category_b = Category(name=f"Combo Mix Cat B {suffix}", slug=f"combo-mix-cat-b-{suffix}")
        session.add(category_a)
        session.add(category_b)
        session.commit()
        session.refresh(category_a)
        session.refresh(category_b)

        product_match = Product(
            slug=f"combo-mix-match-{suffix}",
            name=f"Combo Mix Match {suffix}",
            brand_name="ComboMixBrand",
            price=Decimal("301.00"),
            currency="KZT",
            category_id=category_a.id,
            gender="men",
            is_active=True,
        )
        product_same_category_other_size = Product(
            slug=f"combo-mix-size-{suffix}",
            name=f"Combo Mix Size {suffix}",
            brand_name="ComboMixBrand",
            price=Decimal("302.00"),
            currency="KZT",
            category_id=category_a.id,
            gender="men",
            is_active=True,
        )
        product_other_category = Product(
            slug=f"combo-mix-category-{suffix}",
            name=f"Combo Mix Category {suffix}",
            brand_name="ComboMixBrand",
            price=Decimal("303.00"),
            currency="KZT",
            category_id=category_b.id,
            gender="men",
            is_active=True,
        )
        product_other_gender = Product(
            slug=f"combo-mix-gender-{suffix}",
            name=f"Combo Mix Gender {suffix}",
            brand_name="ComboMixBrand",
            price=Decimal("304.00"),
            currency="KZT",
            category_id=category_a.id,
            gender="women",
            is_active=True,
        )
        product_other_color = Product(
            slug=f"combo-mix-color-{suffix}",
            name=f"Combo Mix Color {suffix}",
            brand_name="ComboMixBrand",
            price=Decimal("305.00"),
            currency="KZT",
            category_id=category_a.id,
            gender="men",
            is_active=True,
        )
        session.add(product_match)
        session.add(product_same_category_other_size)
        session.add(product_other_category)
        session.add(product_other_gender)
        session.add(product_other_color)
        session.commit()
        session.refresh(product_match)
        session.refresh(product_same_category_other_size)
        session.refresh(product_other_category)
        session.refresh(product_other_gender)
        session.refresh(product_other_color)

        session.add(
            ProductVariant(
                product_id=product_match.id,
                sku=f"combo-mix-match-{suffix}",
                color="Black - buy online | INTERTOP",
                size="41",
                stock_quantity=3,
                is_active=True,
            )
        )
        session.add(
            ProductVariant(
                product_id=product_same_category_other_size.id,
                sku=f"combo-mix-size-{suffix}",
                color="Black - buy online | INTERTOP",
                size="42",
                stock_quantity=3,
                is_active=True,
            )
        )
        session.add(
            ProductVariant(
                product_id=product_other_category.id,
                sku=f"combo-mix-category-{suffix}",
                color="Black - buy online | INTERTOP",
                size="41",
                stock_quantity=3,
                is_active=True,
            )
        )
        session.add(
            ProductVariant(
                product_id=product_other_gender.id,
                sku=f"combo-mix-gender-{suffix}",
                color="Black - buy online | INTERTOP",
                size="41",
                stock_quantity=3,
                is_active=True,
            )
        )
        session.add(
            ProductVariant(
                product_id=product_other_color.id,
                sku=f"combo-mix-color-{suffix}",
                color="Blue - buy online | INTERTOP",
                size="41",
                stock_quantity=3,
                is_active=True,
            )
        )
        session.commit()

        category_a_id = category_a.id
        category_b_id = category_b.id

    response = client.get(
        f"/catalog/products?search={suffix}&category_ids={category_a_id}&color_keys=black&size_keys=41&gender_keys=men"
    )
    assert response.status_code == 200
    body = response.json()

    assert body["total"] == 1
    assert {row["slug"] for row in body["items"]} == {f"combo-mix-match-{suffix}"}
    assert body["applied_filters"]["category_ids"] == [category_a_id]
    assert body["applied_filters"]["color_keys"] == ["black"]
    assert body["applied_filters"]["size_keys"] == ["41"]
    assert body["applied_filters"]["gender_keys"] == ["men"]

    category_facets = {row["id"]: row["count"] for row in body["facets"]["categories"]}
    assert category_facets[category_a_id] == 1
    assert category_facets[category_b_id] == 1

    color_facets = {row["key"]: row["count"] for row in body["facets"]["colors"]}
    assert color_facets["black"] == 1
    assert color_facets["blue"] == 1

    size_facets = {row["key"]: row["count"] for row in body["facets"]["sizes"]}
    assert size_facets["41"] == 1
    assert size_facets["42"] == 1

    gender_facets = {row["key"]: row["count"] for row in body["facets"]["genders"]}
    assert gender_facets["men"] == 1
    assert gender_facets["women"] == 1


def test_order_idempotency_api():
    create_db_and_tables()
    client.cookies.clear()
    product_id, variant_id = _seed_product(slug="order-product", sku="order-product-v1", stock=5)
    anon_id = f"anon-api-{uuid4().hex[:8]}"
    idem_key = f"api-order-idempotency-{uuid4().hex[:8]}"

    payload = {
        "idempotency_key": idem_key,
        "shipping": {
            "full_name": "Guest Buyer",
            "email": "guest@example.com",
            "phone": "123456",
            "address_line1": "Street 1",
            "city": "City",
            "postal_code": "10000",
            "country": "US",
        },
        "items": [{"product_id": product_id, "variant_id": variant_id, "quantity": 2}],
    }

    headers = {"X-Anonymous-Id": anon_id}
    first = client.post("/orders", json=payload, headers=headers)
    second = client.post("/orders", json=payload, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]


def test_cart_item_ownership_and_merge_api():
    create_db_and_tables()
    client.cookies.clear()
    product_id, variant_id = _seed_product(slug="cart-product", sku="cart-product-v1", stock=20)
    anon1 = f"anon-owner-1-{uuid4().hex[:8]}"
    anon2 = f"anon-owner-2-{uuid4().hex[:8]}"

    anon1_headers = {"X-Anonymous-Id": anon1}
    anon2_headers = {"X-Anonymous-Id": anon2}

    add_anon = client.post(
        "/cart/items",
        headers=anon1_headers,
        json={"product_id": product_id, "variant_id": variant_id, "quantity": 1},
    )
    assert add_anon.status_code == 200

    cart_anon = client.get("/cart", headers=anon1_headers)
    assert cart_anon.status_code == 200
    item_id = cart_anon.json()["items"][0]["id"]

    wrong_patch = client.patch(f"/cart/items/{item_id}", headers=anon2_headers, json={"quantity": 2})
    wrong_delete = client.delete(f"/cart/items/{item_id}", headers=anon2_headers)
    assert wrong_patch.status_code == 403
    assert wrong_delete.status_code == 403

    email = f"merge-{uuid4().hex[:8]}@example.com"
    reg = client.post(
        "/auth/register",
        json={"email": email, "password": "secret123", "full_name": "Merge User"},
    )
    assert reg.status_code == 200
    login = client.post("/auth/login", json={"email": email, "password": "secret123"})
    assert login.status_code == 200

    user_add = client.post(
        "/cart/items",
        json={"product_id": product_id, "variant_id": variant_id, "quantity": 2},
    )
    assert user_add.status_code == 200

    merge = client.post("/cart/merge", headers=anon1_headers)
    assert merge.status_code == 200
    assert merge.json()["merged"] is True

    user_cart = client.get("/cart")
    assert user_cart.status_code == 200
    lines = user_cart.json()["items"]
    assert len(lines) == 1
    assert lines[0]["quantity"] == 3


def test_add_to_cart_stock_validation_api():
    create_db_and_tables()
    client.cookies.clear()
    product_id, variant_id = _seed_product(slug="stock-product", sku="stock-product-v1", stock=1)
    headers = {"X-Anonymous-Id": f"anon-stock-check-{uuid4().hex[:8]}"}

    first = client.post(
        "/cart/items",
        headers=headers,
        json={"product_id": product_id, "variant_id": variant_id, "quantity": 1},
    )
    second = client.post(
        "/cart/items",
        headers=headers,
        json={"product_id": product_id, "variant_id": variant_id, "quantity": 1},
    )

    assert first.status_code == 200
    assert second.status_code == 409


def test_media_upload_hybrid_api():
    create_db_and_tables()
    admin = _admin_client()

    external = admin.post(
        "/admin/media/upload",
        data={"source_url": "https://cdn.example.com/p/1.jpg"},
    )
    assert external.status_code == 200
    assert external.json()["storage_mode"] == "external_cdn"
    assert external.json()["source_url"] == "https://cdn.example.com/p/1.jpg"

    file_upload = admin.post(
        "/admin/media/upload",
        files={"file": ("boot.png", b"png-bytes", "image/png")},
    )
    assert file_upload.status_code == 200
    assert file_upload.json()["storage_mode"] == "local_copy"
    file_path = file_upload.json()["file_path"]
    assert file_path
    assert Path(file_path).exists()

    wrong_type = admin.post(
        "/admin/media/upload",
        files={"file": ("file.txt", b"text-bytes", "text/plain")},
    )
    assert wrong_type.status_code == 415


def test_recommendation_model_and_train_endpoints_api():
    create_db_and_tables()
    admin = _admin_client()

    build_dataset = admin.post("/admin/jobs/build_training_dataset_job/run")
    assert build_dataset.status_code == 200

    train = admin.post("/admin/recommendations/train")
    assert train.status_code == 200
    assert train.json()["job_name"] == "train_ranker_job"

    model = admin.get("/admin/recommendations/model")
    assert model.status_code == 200
    assert "is_ready" in model.json()


def test_dedupe_jobs_endpoints_api():
    create_db_and_tables()
    admin = _admin_client()
    suffix = uuid4().hex[:8]

    with Session(engine) as session:
        category = Category(name=f"Dedupe API {suffix}", slug=f"dedupe-api-{suffix}")
        session.add(category)
        session.commit()
        session.refresh(category)

        canonical_product = Product(
            slug=f"dedupe-api-canonical-{suffix}",
            name=f"Dedupe API Model {suffix}",
            brand_name="DedupeAPI",
            price=Decimal("175.00"),
            currency="KZT",
            category_id=category.id,
            is_active=True,
        )
        duplicate_product = Product(
            slug=f"dedupe-api-duplicate-{suffix}",
            name=f"Dedupe API Model {suffix}",
            brand_name="DedupeAPI",
            price=Decimal("175.00"),
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
            sku=f"dedupe-api-var-a-{suffix}",
            size="42",
            color="Black",
            stock_quantity=1,
            is_active=True,
        )
        duplicate_variant = ProductVariant(
            product_id=duplicate_product.id,
            sku=f"dedupe-api-var-b-{suffix}",
            size="42",
            color="Black",
            stock_quantity=4,
            is_active=True,
        )
        session.add(canonical_variant)
        session.add(duplicate_variant)
        session.commit()

        source_item_1 = SourceCatalogItem(
            source_system="dedupe_api_source",
            source_product_id=f"api-a-{suffix}",
            source_url=f"https://supplier.local/dedupe/{suffix}/a",
            source_title=f"Dedupe API Model {suffix}",
            source_brand="DedupeAPI",
            source_category_path="api/dedupe",
            source_price=Decimal("175.00"),
            source_currency="KZT",
            source_media_json=[f"https://img.local/{suffix}-main.jpg"],
        )
        source_item_2 = SourceCatalogItem(
            source_system="dedupe_api_source",
            source_product_id=f"api-b-{suffix}",
            source_url=f"https://supplier.local/dedupe/{suffix}/b",
            source_title=f"Dedupe API Model {suffix}",
            source_brand="DedupeAPI",
            source_category_path="api/dedupe",
            source_price=Decimal("175.00"),
            source_currency="KZT",
            source_media_json=[f"https://img.local/{suffix}-main.jpg"],
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
        session.commit()

        canonical_product_id = canonical_product.id
        duplicate_product_id = duplicate_product.id
        canonical_variant_id = canonical_variant.id

    dry_run = admin.post("/admin/jobs/dedupe_products_dry_run_job/run")
    assert dry_run.status_code == 200
    assert dry_run.json()["job_name"] == "dedupe_products_dry_run_job"
    assert dry_run.json()["details_json"]["clusters_total"] == 1

    apply_run = admin.post("/admin/jobs/dedupe_products_apply_job/run")
    assert apply_run.status_code == 200
    assert apply_run.json()["job_name"] == "dedupe_products_apply_job"
    assert apply_run.json()["details_json"]["applied_clusters"] == 1

    with Session(engine) as session:
        canonical = session.get(Product, canonical_product_id)
        duplicate = session.get(Product, duplicate_product_id)
        assert canonical is not None
        assert duplicate is not None
        assert canonical.is_active is True
        assert duplicate.is_active is False

        canonical_variant = session.get(ProductVariant, canonical_variant_id)
        assert canonical_variant is not None
        assert canonical_variant.stock_quantity == 4

    products = client.get(f"/products?search=Dedupe API Model {suffix}")
    assert products.status_code == 200
    active_ids = {row["id"] for row in products.json()}
    assert canonical_product_id in active_ids
    assert duplicate_product_id not in active_ids


def test_normalize_variant_sizes_job_endpoint_api():
    create_db_and_tables()
    admin = _admin_client()
    suffix = uuid4().hex[:6]

    with Session(engine) as session:
        category = Category(name=f"Normalize API {suffix}", slug=f"normalize-api-{suffix}")
        session.add(category)
        session.commit()
        session.refresh(category)

        product = Product(
            slug=f"normalize-api-product-{suffix}",
            name=f"Normalize API Product {suffix}",
            brand_name="SizeBrand",
            price=Decimal("240.00"),
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
                sku=f"normalize-api-44-5-{suffix}",
                size="44,5",
                stock_quantity=3,
                is_active=True,
            )
        )
        session.add(
            ProductVariant(
                product_id=product.id,
                sku=f"normalize-api-341487-{suffix}",
                size="341487",
                stock_quantity=3,
                is_active=True,
            )
        )
        session.add(
            ProductVariant(
                product_id=product.id,
                sku=f"normalize-api-74-{suffix}",
                size="74",
                stock_quantity=3,
                is_active=True,
            )
        )
        session.add(
            ProductVariant(
                product_id=product.id,
                sku=f"normalize-api-99-{suffix}",
                size="99",
                stock_quantity=3,
                is_active=True,
            )
        )
        session.add(
            ProductVariant(
                product_id=product.id,
                sku=f"normalize-api-41-{suffix}",
                size="41",
                stock_quantity=3,
                is_active=True,
            )
        )
        session.add(
            ProductVariant(
                product_id=product.id,
                sku=f"normalize-api-empty-{suffix}",
                size=None,
                stock_quantity=3,
                is_active=True,
            )
        )
        session.commit()

    response = admin.post("/admin/jobs/normalize_variant_sizes_job/run")
    assert response.status_code == 200
    payload = response.json()
    assert payload["job_name"] == "normalize_variant_sizes_job"
    details = payload["details_json"]
    assert {"processed", "normalized", "invalid_cleared", "unchanged", "stock_updated_from_encoded"}.issubset(
        set(details.keys())
    )

    expected_size_by_sku = {
        f"normalize-api-41-{suffix}": "41",
        f"normalize-api-44-5-{suffix}": "44.5",
        f"normalize-api-341487-{suffix}": "34",
        f"normalize-api-74-{suffix}": None,
        f"normalize-api-99-{suffix}": None,
        f"normalize-api-empty-{suffix}": None,
    }
    with Session(engine) as session:
        variants = session.exec(
            select(ProductVariant).where(
                ProductVariant.sku.in_(list(expected_size_by_sku.keys()))
            )
        ).all()
        size_by_sku = {variant.sku: variant.size for variant in variants}
        stock_by_sku = {variant.sku: variant.stock_quantity for variant in variants}

    assert size_by_sku == expected_size_by_sku
    assert stock_by_sku[f"normalize-api-341487-{suffix}"] == 1487


def test_bootstrap_exports_endpoint_api(tmp_path):
    create_db_and_tables()
    admin = _admin_client()

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
                "source_system": "bootstrap_api",
                "source_product_id": "sku-api-1",
                "source_url": "https://supplier.local/item/sku-api-1",
                "source_title": "API Boot",
                "source_description": "desc",
                "source_brand": "BrandZ",
                "source_category_path": "boots",
                "source_attributes_json": "{}",
                "source_price": "150.00",
                "source_compare_at_price": "",
                "source_currency": "KZT",
                "source_stock_json": "{\"sizes\": [\"42\"]}",
                "source_media_json": "[\"https://img/api-boot.jpg\"]",
                "normalized_payload_json": "{}",
                "normalized_status": "pending",
                "last_seen_at": datetime.utcnow().isoformat(),
            }
        ],
    )
    _write_csv(
        exports_dir / "source_product_links.csv",
        ["id", "source_system", "source_product_id", "source_item_id", "product_id", "sync_status", "missing_runs_count", "last_synced_at"],
        [
            {
                "id": 1,
                "source_system": "bootstrap_api",
                "source_product_id": "sku-api-1",
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
                "external_source_system": "bootstrap_api",
                "external_source_product_id": "sku-api-1",
                "canonical_url": "https://footy.local/p/api-boot",
                "title": "API Boot",
                "brand_name": "BrandZ",
                "category_slug": "boots",
                "description": "desc",
                "seller_name": "",
                "color": "Black",
                "current_price": "150.00",
                "compare_at_price": "",
                "currency": "KZT",
                "is_active": "1",
            }
        ],
    )
    _write_csv(
        exports_dir / "product_images.csv",
        ["id", "product_id", "storage_mode", "source_url", "file_path", "cdn_asset_id", "is_primary", "sort_order", "created_at"],
        [{"id": 1, "product_id": 1, "storage_mode": "external_cdn", "source_url": "https://img/api-boot.jpg", "file_path": "", "cdn_asset_id": "", "is_primary": "1", "sort_order": "0", "created_at": datetime.utcnow().isoformat()}],
    )
    _write_csv(
        exports_dir / "product_variants.csv",
        ["id", "product_id", "size", "color", "stock_status", "metadata_json", "created_at", "updated_at"],
        [{"id": 1, "product_id": 1, "size": "42", "color": "Black", "stock_status": "in_stock", "metadata_json": "{}", "created_at": datetime.utcnow().isoformat(), "updated_at": datetime.utcnow().isoformat()}],
    )
    _write_csv(
        exports_dir / "product_tags.csv",
        ["id", "product_id", "tag", "created_at", "updated_at"],
        [{"id": 1, "product_id": 1, "tag": "NEW", "created_at": datetime.utcnow().isoformat(), "updated_at": datetime.utcnow().isoformat()}],
    )
    _write_csv(
        exports_dir / "import_runs.csv",
        ["id", "source_system", "run_type", "status", "root_url", "checkpoint_json", "pages_total", "pages_processed", "items_seen_count", "items_created_count", "items_updated_count", "items_failed_count", "error_summary", "started_at", "finished_at"],
        [{"id": 1, "source_system": "bootstrap_api", "run_type": "import", "status": "completed", "root_url": "", "checkpoint_json": "{}", "pages_total": 1, "pages_processed": 1, "items_seen_count": 1, "items_created_count": 1, "items_updated_count": 0, "items_failed_count": 0, "error_summary": "", "started_at": datetime.utcnow().isoformat(), "finished_at": datetime.utcnow().isoformat()}],
    )

    from app import api as api_module

    original_service = api_module.job_orchestrator.bootstrap_service
    api_module.job_orchestrator.bootstrap_service = ExportBootstrapService(exports_dir=exports_dir, report_dir=report_dir)
    try:
        response = admin.post("/admin/imports/bootstrap-exports")
        assert response.status_code == 200
        assert response.json()["job_name"] == "bootstrap_exports_job"
    finally:
        api_module.job_orchestrator.bootstrap_service = original_service
