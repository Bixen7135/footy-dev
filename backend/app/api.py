from __future__ import annotations

import os
from pathlib import Path
from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile, status
from sqlmodel import Session, func, select

from .config import get_settings
from .database import get_db_session
from .deps import extract_refresh_token, get_current_user, get_optional_user, require_admin
from .models import (
    CMSPage,
    Cart,
    CartItem,
    CartStatus,
    Category,
    ImportRun,
    JobRun,
    Order,
    OrderStatusHistory,
    Product,
    ProductImage,
    ProductTag,
    ProductVariant,
    SourceCatalogItem,
    SourceCategoryMap,
    SourceSystemConfig,
    Tag,
    User,
    UserEvent,
    UserRole,
    WishlistItem,
)
from .schemas import (
    CatalogProductsOut,
    CMSPageIn,
    CMSPagePatchIn,
    CartItemIn,
    CartItemPatchIn,
    CartOut,
    ChangePasswordIn,
    EventBatchIn,
    ImportRunIn,
    ImportRunOut,
    MappingRequest,
    MediaUploadOut,
    OrderCreateIn,
    OrderOut,
    ProductCreateIn,
    ProductImageIn,
    ProductOut,
    ProductPatchIn,
    ProductVariantIn,
    RecommendationResponse,
    RefreshTokenResponse,
    SourceSystemIn,
    SourceSystemPatchIn,
    TokenResponse,
    UserLoginIn,
    UserOut,
    UserRegisterIn,
    WishlistIn,
)
from .security import create_token, decode_token, hash_password, verify_password
from .services import (
    CartService,
    CatalogService,
    EventIngestionService,
    IngestionService,
    JobOrchestrator,
    MappingService,
    OrderService,
    RecommendationService,
    SyncService,
)

settings = get_settings()
ALLOWED_COLOR_KEYS = {
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
}
ALLOWED_GENDER_KEYS = {"men", "women"}


def _set_auth_cookies(response: Response, access_token: str, refresh_token: str) -> None:
    cookie_kwargs = {
        "secure": settings.cookie_secure,
        "samesite": settings.cookie_samesite,
        "path": settings.cookie_path,
        "domain": settings.cookie_domain,
    }
    response.set_cookie(
        key=settings.access_cookie_name,
        value=access_token,
        httponly=True,
        max_age=settings.access_token_minutes * 60,
        **cookie_kwargs,
    )
    response.set_cookie(
        key=settings.refresh_cookie_name,
        value=refresh_token,
        httponly=True,
        max_age=settings.refresh_token_minutes * 60,
        **cookie_kwargs,
    )


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(
        settings.access_cookie_name,
        path=settings.cookie_path,
        domain=settings.cookie_domain,
    )
    response.delete_cookie(
        settings.refresh_cookie_name,
        path=settings.cookie_path,
        domain=settings.cookie_domain,
    )


def _ensure_anonymous_cookie(request: Request, response: Response) -> str:
    anonymous_id = request.headers.get("X-Anonymous-Id") or request.cookies.get(settings.anonymous_cookie_name)
    if anonymous_id:
        return anonymous_id

    anonymous_id = f"anon-{uuid4().hex}"
    response.set_cookie(
        key=settings.anonymous_cookie_name,
        value=anonymous_id,
        httponly=False,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        path=settings.cookie_path,
        domain=settings.cookie_domain,
        max_age=60 * 60 * 24 * 90,
    )
    return anonymous_id


def _parse_category_ids(category_ids: Optional[str]) -> list[int]:
    if not category_ids:
        return []
    parsed: set[int] = set()
    for raw in category_ids.split(","):
        value = raw.strip()
        if not value:
            continue
        try:
            category_id = int(value)
        except ValueError:
            continue
        if category_id > 0:
            parsed.add(category_id)
    return sorted(parsed)


def _parse_color_keys(color_keys: Optional[str]) -> list[str]:
    if not color_keys:
        return []
    parsed: list[str] = []
    seen: set[str] = set()
    for raw in color_keys.split(","):
        value = raw.strip().lower()
        if not value or value in seen or value not in ALLOWED_COLOR_KEYS:
            continue
        seen.add(value)
        parsed.append(value)
    return parsed


def _parse_gender_keys(gender_keys: Optional[str]) -> list[str]:
    if not gender_keys:
        return []
    parsed: list[str] = []
    seen: set[str] = set()
    for raw in gender_keys.split(","):
        value = raw.strip().lower()
        if not value or value in seen or value not in ALLOWED_GENDER_KEYS:
            continue
        seen.add(value)
        parsed.append(value)
    return parsed


def _parse_size_keys(size_keys: Optional[str]) -> list[str]:
    if not size_keys:
        return []
    parsed: list[str] = []
    seen: set[str] = set()
    for raw in size_keys.split(","):
        value = raw.strip().lower()
        if not value or value in seen:
            continue
        seen.add(value)
        parsed.append(value)
    return parsed


def _parse_brand_keys(brand_keys: Optional[str]) -> list[str]:
    if not brand_keys:
        return []
    parsed: list[str] = []
    seen: set[str] = set()
    for raw in brand_keys.split(","):
        value = " ".join(raw.strip().lower().split())
        if not value or value in seen:
            continue
        seen.add(value)
        parsed.append(value)
    return parsed


auth_router = APIRouter(prefix="/auth", tags=["auth"])


@auth_router.post("/register", response_model=UserOut)
def register(
    payload: UserRegisterIn,
    session: Session = Depends(get_db_session),
):
    existing = session.exec(select(User).where(User.email == payload.email.lower())).first()
    if existing:
        raise HTTPException(status_code=409, detail="Email already exists")

    user = User(
        email=payload.email.lower(),
        password_hash=hash_password(payload.password),
        full_name=payload.full_name,
        phone=payload.phone,
        role=UserRole.CUSTOMER,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@auth_router.post("/login", response_model=TokenResponse)
def login(
    payload: UserLoginIn,
    response: Response,
    session: Session = Depends(get_db_session),
):
    user = session.exec(select(User).where(User.email == payload.email.lower())).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    access_token = create_token(str(user.id), settings.access_token_minutes, "access")
    refresh_token = create_token(str(user.id), settings.refresh_token_minutes, "refresh")
    _set_auth_cookies(response, access_token, refresh_token)
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@auth_router.post("/refresh", response_model=RefreshTokenResponse)
def refresh_auth(
    request: Request,
    response: Response,
    session: Session = Depends(get_db_session),
):
    refresh_token = extract_refresh_token(request)
    if not refresh_token:
        raise HTTPException(status_code=401, detail="Refresh token missing")

    payload = decode_token(refresh_token, expected_type="refresh")
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    user_id_raw = payload.get("sub")
    try:
        user_id = int(user_id_raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Invalid token subject") from exc

    user = session.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found")

    new_access = create_token(str(user.id), settings.access_token_minutes, "access")
    new_refresh = create_token(str(user.id), settings.refresh_token_minutes, "refresh")
    _set_auth_cookies(response, new_access, new_refresh)
    return RefreshTokenResponse(access_token=new_access, refresh_token=new_refresh)


@auth_router.post("/logout")
def logout(response: Response):
    _clear_auth_cookies(response)
    return {"ok": True}


@auth_router.get("/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)):
    return current_user


@auth_router.post("/change-password")
def change_password(
    payload: ChangePasswordIn,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
):
    if not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="Current password invalid")

    current_user.password_hash = hash_password(payload.new_password)
    session.add(current_user)
    session.commit()
    return {"ok": True}


admin_import_router = APIRouter(prefix="/admin/imports", tags=["admin-imports"])
ingestion_service = IngestionService()
mapping_service = MappingService()
sync_service = SyncService()


@admin_import_router.post("/run", response_model=ImportRunOut)
def run_import(
    payload: ImportRunIn,
    _: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    run = ingestion_service.run_import(session, payload.source_system, payload.items)
    return run


@admin_import_router.get("", response_model=list[ImportRunOut])
def list_imports(
    _: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    return session.exec(select(ImportRun).order_by(ImportRun.started_at.desc()).limit(100)).all()


@admin_import_router.get("/{run_id}", response_model=ImportRunOut)
def get_import(
    run_id: int,
    _: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    run = session.get(ImportRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Import run not found")
    return run


@admin_import_router.post("/{run_id}/retry", response_model=ImportRunOut)
def retry_import(
    run_id: int,
    _: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    previous = session.get(ImportRun, run_id)
    if not previous:
        raise HTTPException(status_code=404, detail="Import run not found")
    previous.retry_count += 1
    session.add(previous)
    session.commit()
    return previous


@admin_import_router.post("/sync-products")
def sync_products(
    _: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    decisions = sync_service.sync_pending(session)
    return {"processed": len(decisions), "decisions": [d.model_dump() for d in decisions]}


@admin_import_router.post("/bootstrap-exports")
def bootstrap_exports(
    _: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    run = job_orchestrator.run_bootstrap_exports_job(session)
    return run


source_product_router = APIRouter(prefix="/admin/source-products", tags=["admin-source-products"])


@source_product_router.get("")
def list_source_products(
    unmapped_only: bool = Query(default=False),
    _: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    items = session.exec(select(SourceCatalogItem).order_by(SourceCatalogItem.updated_at.desc()).limit(200)).all()
    if not unmapped_only:
        return items

    filtered = []
    for item in items:
        mapping = session.exec(
            select(SourceCategoryMap).where(
                SourceCategoryMap.source_system == item.source_system,
                SourceCategoryMap.source_category_key == item.source_category_path.strip().lower(),
            )
        ).first()
        if not mapping:
            filtered.append(item)
    return filtered


@source_product_router.post("/{source_item_id}/map")
def map_source_product(
    source_item_id: int,
    payload: MappingRequest,
    _: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    source_item = session.get(SourceCatalogItem, source_item_id)
    if not source_item:
        raise HTTPException(status_code=404, detail="Source item not found")
    mapping_service.apply_mapping(session, payload)
    return {"ok": True}


source_system_router = APIRouter(prefix="/admin/source-systems", tags=["admin-source-systems"])


@source_system_router.get("")
def list_source_systems(
    _: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    return session.exec(select(SourceSystemConfig).order_by(SourceSystemConfig.source_system.asc())).all()


@source_system_router.post("")
def create_source_system(
    payload: SourceSystemIn,
    _: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    existing = session.exec(
        select(SourceSystemConfig).where(SourceSystemConfig.source_system == payload.source_system)
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Source system already exists")

    row = SourceSystemConfig(**payload.model_dump())
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@source_system_router.patch("/{source_system_id}")
def patch_source_system(
    source_system_id: int,
    payload: SourceSystemPatchIn,
    _: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    row = session.get(SourceSystemConfig, source_system_id)
    if not row:
        raise HTTPException(status_code=404, detail="Source system not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    row.updated_at = datetime.utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row

catalog_router = APIRouter(tags=["catalog"])
catalog_service = CatalogService()


@catalog_router.get("/products", response_model=list[ProductOut])
def list_products(
    search: Optional[str] = None,
    category_id: Optional[int] = None,
    category_ids: Optional[str] = Query(default=None),
    color_keys: Optional[str] = Query(default=None),
    gender_keys: Optional[str] = Query(default=None),
    brand_keys: Optional[str] = Query(default=None),
    min_price: Optional[Decimal] = None,
    max_price: Optional[Decimal] = None,
    in_stock_only: bool = False,
    limit: int = 24,
    offset: int = 0,
    session: Session = Depends(get_db_session),
):
    parsed_category_ids = _parse_category_ids(category_ids)
    parsed_color_keys = _parse_color_keys(color_keys)
    parsed_gender_keys = _parse_gender_keys(gender_keys)
    parsed_brand_keys = _parse_brand_keys(brand_keys)
    return catalog_service.list_products(
        session=session,
        search=search,
        category_id=category_id,
        category_ids=parsed_category_ids,
        color_keys=parsed_color_keys,
        gender_keys=parsed_gender_keys,
        brand_keys=parsed_brand_keys,
        min_price=min_price,
        max_price=max_price,
        in_stock_only=in_stock_only,
        limit=limit,
        offset=offset,
    )


@catalog_router.get("/catalog/products", response_model=CatalogProductsOut)
def list_catalog_products(
    search: Optional[str] = None,
    category_id: Optional[int] = None,
    category_ids: Optional[str] = Query(default=None),
    color_keys: Optional[str] = Query(default=None),
    gender_keys: Optional[str] = Query(default=None),
    size_keys: Optional[str] = Query(default=None),
    brand_keys: Optional[str] = Query(default=None),
    min_price: Optional[Decimal] = None,
    max_price: Optional[Decimal] = None,
    in_stock_only: bool = False,
    limit: int = Query(default=24, ge=1, le=120),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_db_session),
):
    parsed_category_ids = _parse_category_ids(category_ids)
    parsed_color_keys = _parse_color_keys(color_keys)
    parsed_gender_keys = _parse_gender_keys(gender_keys)
    parsed_size_keys = _parse_size_keys(size_keys)
    parsed_brand_keys = _parse_brand_keys(brand_keys)
    return catalog_service.list_catalog_products(
        session=session,
        search=search,
        category_id=category_id,
        category_ids=parsed_category_ids,
        color_keys=parsed_color_keys,
        gender_keys=parsed_gender_keys,
        size_keys=parsed_size_keys,
        brand_keys=parsed_brand_keys,
        min_price=min_price,
        max_price=max_price,
        in_stock_only=in_stock_only,
        limit=limit,
        offset=offset,
    )


@catalog_router.get("/products/{slug_or_id}", response_model=ProductOut)
def get_product(slug_or_id: str, session: Session = Depends(get_db_session)):
    product = catalog_service.get_product(session, slug_or_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


@catalog_router.get("/categories")
def get_categories(session: Session = Depends(get_db_session)):
    active_product_category_ids = (
        select(Product.category_id)
        .where(Product.is_active.is_(True))
        .group_by(Product.category_id)
    )
    return session.exec(
        select(Category)
        .where(
            Category.is_active.is_(True),
            Category.id.in_(active_product_category_ids),
        )
        .order_by(Category.name.asc())
    ).all()


@catalog_router.get("/tags")
def get_tags(session: Session = Depends(get_db_session)):
    return session.exec(select(Tag).where(Tag.is_active.is_(True))).all()


@catalog_router.get("/products/{product_id}/similar", response_model=list[ProductOut])
def similar_products(product_id: int, session: Session = Depends(get_db_session)):
    products = catalog_service.similar_products(session, product_id, limit=12)
    if products is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return products


product_admin_router = APIRouter(prefix="/admin/products", tags=["admin-products"])


@product_admin_router.post("", response_model=ProductOut)
def create_product(
    payload: ProductCreateIn,
    _: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    product = Product(**payload.model_dump())
    session.add(product)
    session.commit()
    session.refresh(product)
    return product


@product_admin_router.patch("/{product_id}", response_model=ProductOut)
def patch_product(
    product_id: int,
    payload: ProductPatchIn,
    _: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    product = session.get(Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(product, key, value)
        if key not in product.admin_overrides_json:
            product.admin_overrides_json.append(key)
    product.updated_at = datetime.utcnow()
    session.add(product)
    session.commit()
    session.refresh(product)
    return product


@product_admin_router.post("/{product_id}/tags")
def attach_tag(
    product_id: int,
    tag_id: int,
    _: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    product = session.get(Product, product_id)
    tag = session.get(Tag, tag_id)
    if not product or not tag:
        raise HTTPException(status_code=404, detail="Product or tag not found")
    link = session.exec(
        select(ProductTag).where(ProductTag.product_id == product_id, ProductTag.tag_id == tag_id)
    ).first()
    if not link:
        session.add(ProductTag(product_id=product_id, tag_id=tag_id))
        session.commit()
    return {"ok": True, "product_id": product_id, "tag_id": tag_id}


@product_admin_router.delete("/{product_id}/tags/{tag_id}")
def detach_tag(
    product_id: int,
    tag_id: int,
    _: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    link = session.exec(
        select(ProductTag).where(ProductTag.product_id == product_id, ProductTag.tag_id == tag_id)
    ).first()
    if link:
        session.delete(link)
        session.commit()
    return {"ok": True, "product_id": product_id, "tag_id": tag_id}


@product_admin_router.post("/{product_id}/variants")
def add_variant(
    product_id: int,
    payload: ProductVariantIn,
    _: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    product = session.get(Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    row = ProductVariant(product_id=product_id, **payload.model_dump())
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


variant_admin_router = APIRouter(prefix="/admin/variants", tags=["admin-variants"])


@variant_admin_router.patch("/{variant_id}")
def patch_variant(
    variant_id: int,
    payload: ProductVariantIn,
    _: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    row = session.get(ProductVariant, variant_id)
    if not row:
        raise HTTPException(status_code=404, detail="Variant not found")
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    row.updated_at = datetime.utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


media_admin_router = APIRouter(prefix="/admin", tags=["admin-media"])


def _allowed_mime_types() -> set[str]:
    return {value.strip().lower() for value in settings.allowed_upload_mime.split(",") if value.strip()}


def _resolve_file_extension(upload_file: UploadFile) -> str:
    if upload_file.filename and "." in upload_file.filename:
        ext = Path(upload_file.filename).suffix.lower()
        if ext:
            return ext
    mapping = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    return mapping.get((upload_file.content_type or "").lower(), ".bin")


@media_admin_router.post("/media/upload", response_model=MediaUploadOut)
async def upload_media(
    source_url: Optional[str] = Form(default=None),
    file: UploadFile | None = File(default=None),
    _: User = Depends(require_admin),
):
    # Backward-compatible no-op mode for older clients that called this endpoint without payload.
    if not source_url and file is None:
        return MediaUploadOut(ok=True, storage_mode="external_cdn", source_url=None, file_path=None)

    if source_url and file is None:
        return MediaUploadOut(
            ok=True,
            storage_mode="external_cdn",
            source_url=source_url.strip(),
            file_path=None,
        )

    assert file is not None
    content_type = (file.content_type or "").lower()
    if content_type not in _allowed_mime_types():
        raise HTTPException(status_code=415, detail=f"Unsupported content type: {content_type}")

    payload = await file.read()
    if len(payload) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="File exceeds max_upload_bytes")

    upload_root = Path(settings.upload_dir)
    upload_root.mkdir(parents=True, exist_ok=True)
    date_dir = datetime.utcnow().strftime("%Y-%m-%d")
    target_dir = upload_root / date_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    extension = _resolve_file_extension(file)
    target_name = f"{uuid4().hex}{extension}"
    target_path = target_dir / target_name
    target_path.write_bytes(payload)

    relative_path = os.path.relpath(target_path, start=Path.cwd())
    return MediaUploadOut(
        ok=True,
        storage_mode="local_copy",
        file_path=relative_path.replace("\\", "/"),
        source_url=source_url.strip() if source_url else None,
        content_type=content_type,
        size_bytes=len(payload),
    )


@media_admin_router.post("/products/{product_id}/images")
def add_product_image(
    product_id: int,
    payload: ProductImageIn,
    _: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    product = session.get(Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    if not payload.source_url and not payload.file_path:
        raise HTTPException(status_code=422, detail="source_url or file_path is required")
    if payload.file_path and (".." in payload.file_path or payload.file_path.startswith(("/", "\\"))):
        raise HTTPException(status_code=422, detail="Invalid file_path")
    image = ProductImage(product_id=product_id, **payload.model_dump())
    session.add(image)
    session.commit()
    session.refresh(image)
    return image


@media_admin_router.delete("/products/{product_id}/images/{image_id}")
def delete_product_image(
    product_id: int,
    image_id: int,
    _: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    image = session.get(ProductImage, image_id)
    if not image or image.product_id != product_id:
        raise HTTPException(status_code=404, detail="Image not found")
    session.delete(image)
    session.commit()
    return {"ok": True}


cart_router = APIRouter(prefix="/cart", tags=["cart"])
cart_service = CartService()


def _get_actor_ids(
    request: Request,
    user: Optional[User],
    fallback_anonymous_id: Optional[str] = None,
) -> tuple[Optional[int], Optional[str]]:
    if user:
        return user.id, None
    anonymous_id = (
        request.headers.get("X-Anonymous-Id")
        or request.cookies.get(settings.anonymous_cookie_name)
        or fallback_anonymous_id
    )
    if not anonymous_id:
        raise HTTPException(status_code=400, detail="Missing anonymous identity")
    return None, anonymous_id


def _assert_cart_item_ownership(
    session: Session,
    item: CartItem,
    user_id: Optional[int],
    anonymous_id: Optional[str],
) -> None:
    cart = session.get(Cart, item.cart_id)
    if not cart:
        raise HTTPException(status_code=404, detail="Cart not found")

    if user_id and cart.user_id == user_id:
        return
    if anonymous_id and cart.anonymous_id == anonymous_id:
        return
    raise HTTPException(status_code=403, detail="Cart item does not belong to actor")


@cart_router.get("", response_model=CartOut)
def get_cart(
    request: Request,
    response: Response,
    user: Optional[User] = Depends(get_optional_user),
    session: Session = Depends(get_db_session),
):
    fallback_anonymous_id = None
    if not user and not (
        request.headers.get("X-Anonymous-Id") or request.cookies.get(settings.anonymous_cookie_name)
    ):
        fallback_anonymous_id = _ensure_anonymous_cookie(request, response)
    user_id, anonymous_id = _get_actor_ids(request, user, fallback_anonymous_id)
    cart = cart_service.get_or_create_cart(session, user_id, anonymous_id)
    items = session.exec(select(CartItem).where(CartItem.cart_id == cart.id)).all()
    subtotal = cart_service.cart_subtotal(session, cart.id)
    return CartOut(
        id=cart.id,
        status=cart.status,
        items=[
            {
                "id": i.id,
                "product_id": i.product_id,
                "variant_id": i.variant_id,
                "quantity": i.quantity,
                "unit_price_snapshot": i.unit_price_snapshot,
            }
            for i in items
        ],
        subtotal=subtotal,
    )


@cart_router.post("/items")
def add_cart_item(
    payload: CartItemIn,
    request: Request,
    response: Response,
    user: Optional[User] = Depends(get_optional_user),
    session: Session = Depends(get_db_session),
):
    fallback_anonymous_id = None
    if not user and not (
        request.headers.get("X-Anonymous-Id") or request.cookies.get(settings.anonymous_cookie_name)
    ):
        fallback_anonymous_id = _ensure_anonymous_cookie(request, response)
    user_id, anonymous_id = _get_actor_ids(request, user, fallback_anonymous_id)
    cart = cart_service.get_or_create_cart(session, user_id, anonymous_id)
    product = session.get(Product, payload.product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    if not product.is_active:
        raise HTTPException(status_code=409, detail="Product is inactive")
    if product.price is None or product.price <= 0:
        raise HTTPException(status_code=409, detail="Product price unavailable")

    resolved_variant_id = payload.variant_id
    if payload.variant_id:
        variant = session.get(ProductVariant, payload.variant_id)
        if not variant or variant.product_id != payload.product_id or not variant.is_active:
            raise HTTPException(status_code=409, detail="Variant unavailable")
        if variant.stock_quantity < payload.quantity:
            raise HTTPException(status_code=409, detail="Insufficient stock")
    else:
        variant = session.exec(
            select(ProductVariant).where(
                ProductVariant.product_id == payload.product_id,
                ProductVariant.is_active.is_(True),
                ProductVariant.stock_quantity > 0,
            )
        ).first()
        if not variant:
            raise HTTPException(status_code=409, detail="No in-stock variant available")
        resolved_variant_id = variant.id

    existing = session.exec(
        select(CartItem).where(
            CartItem.cart_id == cart.id,
            CartItem.product_id == payload.product_id,
            CartItem.variant_id == resolved_variant_id,
        )
    ).first()

    if existing:
        if variant.stock_quantity < existing.quantity + payload.quantity:
            raise HTTPException(status_code=409, detail="Insufficient stock")
        existing.quantity += payload.quantity
        session.add(existing)
    else:
        session.add(
            CartItem(
                cart_id=cart.id,
                product_id=payload.product_id,
                variant_id=resolved_variant_id,
                quantity=payload.quantity,
                unit_price_snapshot=product.price,
            )
        )
    session.commit()
    return {"ok": True}


@cart_router.patch("/items/{item_id}")
def patch_cart_item(
    item_id: int,
    payload: CartItemPatchIn,
    request: Request,
    user: Optional[User] = Depends(get_optional_user),
    session: Session = Depends(get_db_session),
):
    user_id, anonymous_id = _get_actor_ids(request, user)
    item = session.get(CartItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Cart item not found")
    _assert_cart_item_ownership(session, item, user_id, anonymous_id)
    variant = session.get(ProductVariant, item.variant_id) if item.variant_id else None
    if variant and variant.stock_quantity < payload.quantity:
        raise HTTPException(status_code=409, detail="Insufficient stock")
    item.quantity = payload.quantity
    item.updated_at = datetime.utcnow()
    session.add(item)
    session.commit()
    return {"ok": True}


@cart_router.delete("/items/{item_id}")
def delete_cart_item(
    item_id: int,
    request: Request,
    user: Optional[User] = Depends(get_optional_user),
    session: Session = Depends(get_db_session),
):
    user_id, anonymous_id = _get_actor_ids(request, user)
    item = session.get(CartItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Cart item not found")
    _assert_cart_item_ownership(session, item, user_id, anonymous_id)
    session.delete(item)
    session.commit()
    return {"ok": True}


@cart_router.delete("")
def clear_cart(
    request: Request,
    user: Optional[User] = Depends(get_optional_user),
    session: Session = Depends(get_db_session),
):
    user_id, anonymous_id = _get_actor_ids(request, user)
    cart = cart_service.get_or_create_cart(session, user_id, anonymous_id)
    session.exec(select(CartItem).where(CartItem.cart_id == cart.id)).all()
    for row in session.exec(select(CartItem).where(CartItem.cart_id == cart.id)).all():
        session.delete(row)
    session.commit()
    return {"ok": True}


@cart_router.post("/merge")
def merge_cart(
    request: Request,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
):
    anonymous_id = request.headers.get("X-Anonymous-Id") or request.cookies.get(settings.anonymous_cookie_name)
    if not anonymous_id:
        raise HTTPException(status_code=400, detail="Missing anonymous_id")

    anon_cart = session.exec(
        select(Cart).where(Cart.anonymous_id == anonymous_id, Cart.status == CartStatus.ACTIVE)
    ).first()
    if not anon_cart:
        return {"ok": True, "merged": False}

    user_cart = cart_service.get_or_create_cart(session, current_user.id, None)
    items = session.exec(select(CartItem).where(CartItem.cart_id == anon_cart.id)).all()
    for item in items:
        existing = session.exec(
            select(CartItem).where(
                CartItem.cart_id == user_cart.id,
                CartItem.product_id == item.product_id,
                CartItem.variant_id == item.variant_id,
            )
        ).first()
        if existing:
            existing.quantity += item.quantity
            session.add(existing)
            session.delete(item)
        else:
            item.cart_id = user_cart.id
            session.add(item)
    anon_cart.status = CartStatus.CONVERTED
    anon_cart.merged_into_cart_id = user_cart.id
    session.add(anon_cart)
    session.commit()
    return {"ok": True, "merged": True}

wishlist_router = APIRouter(prefix="/wishlist", tags=["wishlist"])


@wishlist_router.get("")
def list_wishlist(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
):
    return session.exec(
        select(WishlistItem).where(WishlistItem.user_id == current_user.id)
    ).all()


@wishlist_router.post("")
def add_wishlist(
    payload: WishlistIn,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
):
    exists = session.exec(
        select(WishlistItem).where(
            WishlistItem.user_id == current_user.id,
            WishlistItem.product_id == payload.product_id,
            WishlistItem.variant_id == payload.variant_id,
        )
    ).first()
    if exists:
        return {"ok": True}
    row = WishlistItem(user_id=current_user.id, **payload.model_dump())
    session.add(row)
    session.commit()
    return {"ok": True}


@wishlist_router.delete("/{product_id}")
def remove_wishlist(
    product_id: int,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
):
    rows = session.exec(
        select(WishlistItem).where(
            WishlistItem.user_id == current_user.id,
            WishlistItem.product_id == product_id,
        )
    ).all()
    for row in rows:
        session.delete(row)
    session.commit()
    return {"ok": True}


orders_router = APIRouter(tags=["orders"])
order_service = OrderService()


@orders_router.post("/orders", response_model=OrderOut)
def create_order(
    payload: OrderCreateIn,
    request: Request,
    response: Response,
    current_user: Optional[User] = Depends(get_optional_user),
    session: Session = Depends(get_db_session),
):
    anonymous_id = request.headers.get("X-Anonymous-Id") or request.cookies.get(settings.anonymous_cookie_name)
    if not current_user and not anonymous_id:
        anonymous_id = _ensure_anonymous_cookie(request, response)
    order = order_service.create_order(session, current_user, anonymous_id, payload)
    return order


@orders_router.get("/orders", response_model=list[OrderOut])
def list_orders(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
):
    if current_user.role == UserRole.ADMIN:
        return session.exec(select(Order).order_by(Order.created_at.desc()).limit(200)).all()
    return session.exec(
        select(Order).where(Order.user_id == current_user.id).order_by(Order.created_at.desc())
    ).all()


@orders_router.get("/orders/{order_id}", response_model=OrderOut)
def get_order(
    order_id: int,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
):
    order = session.get(Order, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if current_user.role != UserRole.ADMIN and order.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not allowed")
    return order


admin_order_router = APIRouter(prefix="/admin/orders", tags=["admin-orders"])


@admin_order_router.patch("/{order_id}/status")
def patch_order_status(
    order_id: int,
    new_status: str,
    current_user: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    order = session.get(Order, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    previous = order.status
    order.status = new_status
    order.updated_at = datetime.utcnow()
    session.add(order)
    session.add(
        OrderStatusHistory(
            order_id=order.id,
            previous_status=previous,
            new_status=new_status,
            changed_by_user_id=current_user.id,
        )
    )
    session.commit()
    return {"ok": True}


@admin_order_router.post("/{order_id}/note")
def add_order_note(
    order_id: int,
    note: str,
    current_user: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    order = session.get(Order, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    session.add(
        OrderStatusHistory(
            order_id=order.id,
            previous_status=order.status,
            new_status=order.status,
            changed_by_user_id=current_user.id,
            admin_note=note,
        )
    )
    session.commit()
    return {"ok": True}


pages_router = APIRouter(tags=["cms"])


@pages_router.get("/pages/{slug}")
def get_page(slug: str, session: Session = Depends(get_db_session)):
    page = session.exec(select(CMSPage).where(CMSPage.slug == slug, CMSPage.is_published.is_(True))).first()
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")
    return page


@pages_router.post("/admin/pages")
def create_page(
    payload: CMSPageIn,
    _: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    page = CMSPage(**payload.model_dump())
    session.add(page)
    session.commit()
    session.refresh(page)
    return page


@pages_router.patch("/admin/pages/{page_id}")
def patch_page(
    page_id: int,
    payload: CMSPagePatchIn,
    _: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    page = session.get(CMSPage, page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(page, key, value)
    page.updated_at = datetime.utcnow()
    session.add(page)
    session.commit()
    session.refresh(page)
    return page


events_router = APIRouter(prefix="/events", tags=["events"])
event_service = EventIngestionService()


@events_router.post("/batch")
def ingest_events(payload: EventBatchIn, session: Session = Depends(get_db_session)):
    inserted = event_service.ingest_batch(session, payload)
    return {"inserted": inserted}


recommendations_router = APIRouter(tags=["recommendations"])
recommendation_service = RecommendationService()


@recommendations_router.get("/recommendations", response_model=RecommendationResponse)
def get_recommendations(
    context: str,
    limit: int = 12,
    current_product_id: Optional[int] = None,
    anonymous_id: Optional[str] = None,
    user: Optional[User] = Depends(get_optional_user),
    session: Session = Depends(get_db_session),
):
    return recommendation_service.get_recommendations(
        session=session,
        context=context,
        limit=limit,
        user_id=user.id if user else None,
        anonymous_id=anonymous_id,
        current_product_id=current_product_id,
    )


@recommendations_router.get("/me/recently-viewed")
def recently_viewed(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session),
):
    events = session.exec(
        select(UserEvent)
        .where(UserEvent.user_id == current_user.id, UserEvent.event_type == "product_view")
        .order_by(UserEvent.created_at.desc())
        .limit(50)
    ).all()
    product_ids = []
    for event in events:
        if event.product_id and event.product_id not in product_ids:
            product_ids.append(event.product_id)
    products = [session.get(Product, pid) for pid in product_ids]
    return [p for p in products if p]


@recommendations_router.get("/admin/recommendations/metrics")
def recommendation_metrics(
    _: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    impressions = session.exec(
        select(func.count(UserEvent.id)).where(UserEvent.event_type == "recommendation_impression")
    ).one()
    clicks = session.exec(
        select(func.count(UserEvent.id)).where(UserEvent.event_type == "recommendation_click")
    ).one()
    ctr = float(clicks / impressions) if impressions else 0.0
    return {"impressions": impressions, "clicks": clicks, "ctr": ctr}


@recommendations_router.get("/admin/recommendations/model")
def recommendation_model_status(
    _: User = Depends(require_admin),
):
    return recommendation_service.get_model_status()


@recommendations_router.post("/admin/recommendations/train")
def recommendation_train(
    _: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    return job_orchestrator.run_train_ranker_job(session)


@recommendations_router.get("/admin/recommendations/jobs")
def recommendation_jobs(
    _: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    return session.exec(
        select(JobRun)
        .where(
            JobRun.job_name.in_(
                [
                    "recompute_trending_job",
                    "recompute_similarity_job",
                    "build_training_dataset_job",
                    "train_ranker_job",
                    "recommendation_shadow_eval_job",
                ]
            )
        )
        .order_by(JobRun.started_at.desc())
        .limit(100)
    ).all()


@recommendations_router.get("/admin/recommendations/quality")
def recommendation_quality(
    days: int = 7,
    _: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    return recommendation_service.get_quality_metrics(session=session, window_days=days)


analytics_router = APIRouter(prefix="/admin/analytics", tags=["analytics"])


@analytics_router.get("/overview")
def analytics_overview(
    _: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    return {
        "products": session.exec(select(func.count(Product.id))).one(),
        "orders": session.exec(select(func.count(Order.id))).one(),
        "events": session.exec(select(func.count(UserEvent.id))).one(),
    }


@analytics_router.get("/products/{product_id}")
def analytics_product(
    product_id: int,
    _: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    events = session.exec(
        select(UserEvent.event_type, func.count(UserEvent.id))
        .where(UserEvent.product_id == product_id)
        .group_by(UserEvent.event_type)
    ).all()
    return {"product_id": product_id, "events": [{"type": e[0], "count": e[1]} for e in events]}


@analytics_router.get("/search-terms")
def analytics_search_terms(
    _: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    rows = session.exec(
        select(UserEvent.metadata_json, func.count(UserEvent.id))
        .where(UserEvent.event_type == "search")
        .group_by(UserEvent.metadata_json)
        .limit(100)
    ).all()
    return [{"metadata": r[0], "count": r[1]} for r in rows]


@analytics_router.get("/categories")
def analytics_categories(
    _: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    rows = session.exec(
        select(Product.category_id, func.count(Product.id)).group_by(Product.category_id)
    ).all()
    return [{"category_id": r[0], "products": r[1]} for r in rows]


@analytics_router.get("/recommendations")
def analytics_recommendations(
    _: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    impressions = session.exec(
        select(func.count(UserEvent.id)).where(UserEvent.event_type == "recommendation_impression")
    ).one()
    clicks = session.exec(
        select(func.count(UserEvent.id)).where(UserEvent.event_type == "recommendation_click")
    ).one()
    return {"impressions": impressions, "clicks": clicks}


jobs_router = APIRouter(prefix="/admin/jobs", tags=["jobs"])
job_orchestrator = JobOrchestrator()


@jobs_router.get("")
def list_jobs(
    _: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    return session.exec(select(JobRun).order_by(JobRun.started_at.desc()).limit(200)).all()


@jobs_router.post("/{job_name}/run")
def run_job(
    job_name: str,
    _: User = Depends(require_admin),
    session: Session = Depends(get_db_session),
):
    runners = {
        "bootstrap_exports_job": job_orchestrator.run_bootstrap_exports_job,
        "normalize_source_catalog_job": job_orchestrator.run_normalize_source_catalog_job,
        "sync_source_to_store_job": job_orchestrator.run_sync_source_to_store_job,
        "normalize_variant_sizes_job": job_orchestrator.run_normalize_variant_sizes_job,
        "cleanup_stale_source_products_job": job_orchestrator.run_cleanup_stale_source_products_job,
        "aggregate_events_job": job_orchestrator.run_aggregate_events_job,
        "recompute_recently_viewed_job": job_orchestrator.run_recompute_recently_viewed_job,
        "recompute_trending_job": job_orchestrator.run_recompute_trending_job,
        "recompute_similarity_job": job_orchestrator.run_recompute_similarity_job,
        "build_training_dataset_job": job_orchestrator.run_build_training_dataset_job,
        "train_ranker_job": job_orchestrator.run_train_ranker_job,
        "recommendation_shadow_eval_job": job_orchestrator.run_recommendation_shadow_eval_job,
        "dedupe_products_dry_run_job": job_orchestrator.run_dedupe_products_dry_run_job,
        "dedupe_products_apply_job": job_orchestrator.run_dedupe_products_apply_job,
        "refresh_product_primary_images_job": job_orchestrator.run_refresh_product_primary_images_job,
        "cleanup_old_events_job": job_orchestrator.run_cleanup_old_events_job,
    }
    runner = runners.get(job_name)
    if not runner:
        raise HTTPException(status_code=404, detail="Unknown job")
    run = runner(session)
    return run


health_router = APIRouter(tags=["health"])


@health_router.get("/health")
def health():
    return {"status": "ok"}


@health_router.get("/health/ready")
def ready(session: Session = Depends(get_db_session)):
    session.exec(select(func.count(User.id))).one()
    return {"status": "ready"}


def build_app(*, lifespan=None) -> FastAPI:
    app = FastAPI(title=settings.app_name, lifespan=lifespan)

    app.include_router(auth_router)
    app.include_router(admin_import_router)
    app.include_router(source_product_router)
    app.include_router(source_system_router)
    app.include_router(catalog_router)
    app.include_router(product_admin_router)
    app.include_router(variant_admin_router)
    app.include_router(media_admin_router)
    app.include_router(cart_router)
    app.include_router(wishlist_router)
    app.include_router(orders_router)
    app.include_router(admin_order_router)
    app.include_router(pages_router)
    app.include_router(events_router)
    app.include_router(recommendations_router)
    app.include_router(analytics_router)
    app.include_router(jobs_router)
    app.include_router(health_router)

    return app
