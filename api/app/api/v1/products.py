"""Products, package variants and identifiers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ...db import get_db
from ...deps import Principal, requires
from ...errors import ConflictError, NotFoundError, ValidationError
from ...models.inspection import Inspection
from ...models.product import Product, ProductIdentifier, ProductMergeRecord, ResponsibleParty
from ...schemas.common import Page, PageNumber, PageSize
from ...schemas.workspace import (
    GtinCheckResponse,
    IdentifierInput,
    IdentifierOut,
    ProductCreateRequest,
    ProductDetail,
    ProductSummary,
    ProductUpdateRequest,
    ResponsiblePartyInput,
    ResponsiblePartyOut,
)
from ...security import Permission
from ...services import audit as audit_service
from ...services import gtin
from ...services.extraction import units as unit_utils

router = APIRouter(prefix="/products", tags=["products"])


async def load_product(db: AsyncSession, product_id: uuid.UUID) -> Product | None:
    """Load a product with its identifiers and responsible parties.

    ``populate_existing`` forces a refresh of an instance already in the session's
    identity map, which is what makes this safe to call straight after creating a
    product in the same request: without it the relationship collections would be
    unloaded and touching them would attempt a lazy load inside an async session.
    """
    return await db.scalar(
        select(Product)
        .options(
            selectinload(Product.identifiers),
            selectinload(Product.responsible_parties),
        )
        .where(Product.id == product_id)
        .execution_options(populate_existing=True)
    )


def _summary(product: Product, *, inspection_count: int = 0) -> ProductSummary:
    return ProductSummary(
        id=str(product.id),
        version=product.version,
        brand=product.brand,
        name=product.name,
        common_generic_name=product.common_generic_name,
        commodity_category=product.commodity_category,
        is_food=product.is_food,
        package_type=product.package_type,
        quantity_kind=product.quantity_kind,
        declared_net_quantity=product.declared_net_quantity,
        declared_net_quantity_unit=product.declared_net_quantity_unit,
        net_quantity_base=product.net_quantity_base,
        is_imported=product.is_imported,
        country_of_origin=product.country_of_origin,
        is_multipiece=product.is_multipiece,
        pieces_per_package=product.pieces_per_package,
        primary_gtin=product.primary_gtin,
        inspection_count=inspection_count,
        updated_at=product.updated_at,
    )


def _detail(product: Product, *, inspection_count: int = 0) -> ProductDetail:
    return ProductDetail(
        **_summary(product, inspection_count=inspection_count).model_dump(),
        notes=product.notes,
        identifiers=[
            IdentifierOut(
                id=str(item.id),
                scheme=item.scheme,
                value=item.value,
                formatted=gtin.format_for_display(item.value),
                check_digit_valid=item.check_digit_valid,
                barcode_symbology=item.barcode_symbology,
                is_primary=item.is_primary,
                officer_confirmed=item.officer_confirmed,
                source=item.source,
            )
            for item in product.identifiers
        ],
        responsible_parties=[
            ResponsiblePartyOut(
                id=str(item.id),
                party_role=item.party_role,
                legal_name=item.legal_name,
                address_line=item.address_line,
                locality=item.locality,
                state=item.state,
                pin_code=item.pin_code,
                country=item.country,
                consumer_care_name=item.consumer_care_name,
                consumer_care_email=item.consumer_care_email,
                consumer_care_phone=item.consumer_care_phone,
            )
            for item in product.responsible_parties
        ],
    )


def _base_quantity(amount: Decimal | None, unit: str | None) -> tuple[Decimal | None, str | None]:
    """Convert a declared quantity to its SI base unit, exactly."""
    if amount is None or not unit:
        return None, None
    converted = unit_utils.convert(amount, unit)
    if converted is None:
        raise ValidationError(
            f"{unit!r} is not a unit this system recognises for a net quantity.",
            details={"field": "declared_net_quantity_unit"},
        )
    dimension, base_amount, _base_unit = converted
    return base_amount, dimension.value


@router.get("/gtin-check", response_model=GtinCheckResponse, summary="Validate a barcode number")
async def check_gtin(
    value: str = Query(min_length=1, max_length=40),
    _principal: Principal = Depends(requires(Permission.PRODUCT_READ)),
) -> GtinCheckResponse:
    """Check a barcode number's structure and GS1 check digit.

    A valid check digit means the number is well-formed. It is not evidence that the
    package is genuine or that the number belongs to this product.
    """
    result = gtin.check(value)
    return GtinCheckResponse(
        input=value,
        acceptable=result.acceptable,
        normalised=result.normalised,
        length=result.length,
        scheme=result.scheme,
        check_digit_valid=result.check_digit_valid,
        problem=result.problem,
        india_gs1_prefix=result.india_prefix,
        note=(
            "A valid check digit confirms the number is well-formed. It does not "
            "confirm that the package is genuine or that the number was issued for "
            "this product."
        ),
    )


@router.get("", response_model=Page[ProductSummary], summary="Search products")
async def list_products(
    page: PageNumber = Query(1),
    page_size: PageSize = Query(25),
    search: str = Query("", max_length=160),
    barcode: str = Query("", max_length=32),
    category: str | None = Query(None, max_length=120),
    principal: Principal = Depends(requires(Permission.PRODUCT_READ)),
    db: AsyncSession = Depends(get_db),
) -> Page[ProductSummary]:
    """Products are a shared national reference and are not jurisdiction-scoped.

    Inspections are scoped; the product catalogue is not, because the same package
    variant is sold in every district and duplicating it per jurisdiction would defeat
    the point of a shared repository.
    """
    conditions: list[Any] = [Product.merged_into_id.is_(None)]
    if search:
        pattern = f"%{search.lower()}%"
        conditions.append(
            or_(
                func.lower(Product.brand).like(pattern),
                func.lower(Product.name).like(pattern),
                func.lower(Product.common_generic_name).like(pattern),
            )
        )
    if barcode:
        digits = gtin.normalise(barcode)
        conditions.append(
            Product.id.in_(
                select(ProductIdentifier.product_id).where(ProductIdentifier.value == digits)
            )
        )
    if category:
        conditions.append(Product.commodity_category == category)

    total = int(await db.scalar(select(func.count()).select_from(Product).where(*conditions)) or 0)
    rows = list(
        await db.scalars(
            select(Product)
            .where(*conditions)
            .order_by(Product.updated_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    counts = await _inspection_counts(db, [row.id for row in rows])
    return Page.build(
        [_summary(row, inspection_count=counts.get(row.id, 0)) for row in rows],
        page=page,
        page_size=page_size,
        total=total,
    )


async def _inspection_counts(
    db: AsyncSession, product_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    if not product_ids:
        return {}
    rows = await db.execute(
        select(Inspection.product_id, func.count())
        .where(Inspection.product_id.in_(product_ids))
        .group_by(Inspection.product_id)
    )
    return {product_id: int(total) for product_id, total in rows}


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ProductDetail,
    summary="Create a product",
)
async def create_product(
    payload: ProductCreateRequest,
    request: Request,
    principal: Principal = Depends(requires(Permission.PRODUCT_CREATE)),
    db: AsyncSession = Depends(get_db),
) -> ProductDetail:
    product = await create_product_record(
        db, principal.audit_context(request), payload=payload, created_by_id=principal.id
    )
    await db.commit()
    loaded = await load_product(db, product.id)
    return _detail(loaded or product)


async def create_product_record(
    db: AsyncSession,
    context: audit_service.AuditContext,
    *,
    payload: ProductCreateRequest,
    created_by_id: uuid.UUID,
) -> Product:
    """Shared by the products endpoint and by inspection creation."""
    base_amount, dimension = _base_quantity(
        payload.declared_net_quantity, payload.declared_net_quantity_unit
    )

    product = Product(
        id=uuid.uuid4(),
        brand=payload.brand.strip(),
        name=payload.name.strip(),
        common_generic_name=(payload.common_generic_name or "").strip() or None,
        commodity_category=payload.commodity_category.strip(),
        is_food=payload.is_food,
        package_type=payload.package_type,
        quantity_kind=payload.quantity_kind.value if payload.quantity_kind else dimension,
        declared_net_quantity=payload.declared_net_quantity,
        declared_net_quantity_unit=(
            unit_utils.normalise_unit(payload.declared_net_quantity_unit)
            if payload.declared_net_quantity_unit
            else None
        ),
        net_quantity_base=base_amount,
        is_imported=payload.is_imported,
        country_of_origin=(payload.country_of_origin or "").strip() or None,
        is_multipiece=payload.is_multipiece,
        pieces_per_package=payload.pieces_per_package,
        notes=payload.notes,
        created_by_id=created_by_id,
    )
    db.add(product)
    await db.flush()

    for identifier in payload.identifiers:
        await _attach_identifier(
            db, product=product, identifier=identifier, recorded_by_id=created_by_id
        )
    for party in payload.responsible_parties:
        _attach_party(db, product=product, party=party, recorded_by_id=created_by_id)
    await db.flush()

    await audit_service.record(
        db,
        context,
        action="product.created",
        entity_type="product",
        entity_id=product.id,
        new_values={
            "brand": product.brand,
            "name": product.name,
            "commodity_category": product.commodity_category,
            "identifiers": [item.value for item in payload.identifiers],
        },
    )
    return product


async def _attach_identifier(
    db: AsyncSession,
    *,
    product: Product,
    identifier: IdentifierInput,
    recorded_by_id: uuid.UUID | None,
    source: str = "officer_entry",
    symbology: str | None = None,
) -> ProductIdentifier:
    result = gtin.check(identifier.value) if identifier.scheme != "internal" else None
    normalised = result.normalised if result and result.normalised else identifier.value.strip()

    if result is not None and not result.acceptable:
        # Recorded anyway with the check-digit result, because an officer may
        # legitimately need to record a barcode that is itself wrong on the package.
        pass

    existing = await db.scalar(
        select(ProductIdentifier).where(
            ProductIdentifier.scheme == identifier.scheme,
            ProductIdentifier.value == normalised,
            ProductIdentifier.product_id != product.id,
        )
    )
    if existing is not None:
        other = await db.get(Product, existing.product_id)
        raise ConflictError(
            f"Barcode {normalised} is already recorded against "
            f"{other.display_name if other else 'another product'}. "
            "Use that product, or merge the records if they are the same package.",
            code="identifier_already_used",
            details={
                "existing_product_id": str(existing.product_id),
                "existing_product": other.display_name if other else None,
            },
        )

    # Determine primary status with an explicit query. Touching
    # ``product.identifiers`` here would trigger a lazy load, which is not
    # permitted inside an async session.
    existing_count = int(
        await db.scalar(
            select(func.count())
            .select_from(ProductIdentifier)
            .where(
                ProductIdentifier.product_id == product.id,
                ProductIdentifier.is_primary.is_(True),
            )
        )
        or 0
    )

    row = ProductIdentifier(
        id=uuid.uuid4(),
        product_id=product.id,
        scheme=identifier.scheme,
        value=normalised,
        check_digit_valid=result.check_digit_valid if result else None,
        barcode_symbology=symbology,
        is_primary=existing_count == 0,
        source=source,
        officer_confirmed=identifier.officer_confirmed,
        recorded_by_id=recorded_by_id,
    )
    db.add(row)
    return row


def _attach_party(
    db: AsyncSession,
    *,
    product: Product,
    party: ResponsiblePartyInput,
    recorded_by_id: uuid.UUID | None,
) -> ResponsibleParty:
    row = ResponsibleParty(
        id=uuid.uuid4(),
        product_id=product.id,
        party_role=party.party_role,
        legal_name=party.legal_name.strip(),
        address_line=party.address_line,
        locality=party.locality,
        state=party.state,
        pin_code=party.pin_code,
        country=party.country,
        consumer_care_name=party.consumer_care_name,
        consumer_care_email=(party.consumer_care_email or "").lower() or None,
        consumer_care_phone=party.consumer_care_phone,
        recorded_by_id=recorded_by_id,
    )
    db.add(row)
    return row


@router.get("/{product_id}", response_model=ProductDetail, summary="Read one product")
async def read_product(
    product_id: uuid.UUID,
    _principal: Principal = Depends(requires(Permission.PRODUCT_READ)),
    db: AsyncSession = Depends(get_db),
) -> ProductDetail:
    product = await load_product(db, product_id)
    if product is None:
        raise NotFoundError("That product was not found.")
    counts = await _inspection_counts(db, [product.id])
    return _detail(product, inspection_count=counts.get(product.id, 0))


@router.patch("/{product_id}", response_model=ProductDetail, summary="Update a product")
async def update_product(
    product_id: uuid.UUID,
    payload: ProductUpdateRequest,
    request: Request,
    principal: Principal = Depends(requires(Permission.PRODUCT_UPDATE)),
    db: AsyncSession = Depends(get_db),
) -> ProductDetail:
    product = await load_product(db, product_id)
    if product is None:
        raise NotFoundError("That product was not found.")
    if product.version != payload.expected_version:
        from ...errors import StaleRecordError

        raise StaleRecordError(
            "This product changed after you loaded it. Reload and try again.",
            details={"your_version": payload.expected_version, "current_version": product.version},
        )

    changes = payload.model_dump(exclude_unset=True, exclude={"expected_version"})
    before = {key: getattr(product, key, None) for key in changes}

    for field, value in changes.items():
        if value is None:
            continue
        setattr(product, field, value.value if hasattr(value, "value") else value)

    if product.declared_net_quantity is not None and product.declared_net_quantity_unit:
        base_amount, dimension = _base_quantity(
            product.declared_net_quantity, product.declared_net_quantity_unit
        )
        product.net_quantity_base = base_amount
        product.quantity_kind = product.quantity_kind or dimension

    await db.flush()
    await audit_service.record(
        db,
        principal.audit_context(request),
        action="product.updated",
        entity_type="product",
        entity_id=product.id,
        entity_version=product.version,
        old_values={key: str(value) for key, value in before.items()},
        new_values={key: str(getattr(product, key, None)) for key in changes},
    )
    await db.commit()
    loaded = await load_product(db, product.id)
    return _detail(loaded or product)


@router.post(
    "/{product_id}/identifiers",
    status_code=status.HTTP_201_CREATED,
    response_model=IdentifierOut,
    summary="Record a barcode against a product",
)
async def add_identifier(
    product_id: uuid.UUID,
    payload: IdentifierInput,
    request: Request,
    principal: Principal = Depends(requires(Permission.PRODUCT_UPDATE)),
    db: AsyncSession = Depends(get_db),
) -> IdentifierOut:
    product = await db.get(Product, product_id)
    if product is None:
        raise NotFoundError("That product was not found.")
    row = await _attach_identifier(
        db, product=product, identifier=payload, recorded_by_id=principal.id
    )
    await audit_service.record(
        db,
        principal.audit_context(request),
        action="product.identifier_added",
        entity_type="product",
        entity_id=product.id,
        new_values={"scheme": row.scheme, "value": row.value},
    )
    await db.commit()
    return IdentifierOut(
        id=str(row.id),
        scheme=row.scheme,
        value=row.value,
        formatted=gtin.format_for_display(row.value),
        check_digit_valid=row.check_digit_valid,
        barcode_symbology=row.barcode_symbology,
        is_primary=row.is_primary,
        officer_confirmed=row.officer_confirmed,
        source=row.source,
    )


@router.post(
    "/{product_id}/merge/{target_id}",
    response_model=ProductDetail,
    summary="Merge one product into another",
)
async def merge_products(
    product_id: uuid.UUID,
    target_id: uuid.UUID,
    reason: str = Query(min_length=10, max_length=1000),
    request: Request = None,  # type: ignore[assignment]
    principal: Principal = Depends(requires(Permission.PRODUCT_MERGE)),
    db: AsyncSession = Depends(get_db),
) -> ProductDetail:
    """Point one product record at another, keeping a record of what was merged.

    Inspections are repointed; the source row is retained and marked merged so an
    older report that cites it still resolves.
    """
    if product_id == target_id:
        raise ValidationError("A product cannot be merged into itself.")

    source = await load_product(db, product_id)
    target = await load_product(db, target_id)
    if source is None or target is None:
        raise NotFoundError("One of those products was not found.")
    if source.merged_into_id is not None:
        raise ConflictError("That product has already been merged.", code="already_merged")

    snapshot = {
        "brand": source.brand,
        "name": source.name,
        "commodity_category": source.commodity_category,
        "identifiers": [item.value for item in source.identifiers],
    }

    from sqlalchemy import update as sql_update

    moved = await db.execute(
        sql_update(Inspection)
        .where(Inspection.product_id == source.id)
        .values(product_id=target.id)
    )
    for identifier in source.identifiers:
        identifier.product_id = target.id
        identifier.is_primary = False

    source.merged_into_id = target.id
    db.add(
        ProductMergeRecord(
            id=uuid.uuid4(),
            source_product_id=source.id,
            target_product_id=target.id,
            reason=reason,
            source_snapshot=snapshot,
            merged_by_id=principal.id,
            merged_at=datetime.now(UTC),
        )
    )
    await db.flush()

    await audit_service.record(
        db,
        principal.audit_context(request),
        action="product.merged",
        entity_type="product",
        entity_id=target.id,
        old_values=snapshot,
        new_values={
            "merged_from": str(source.id),
            "inspections_moved": int(moved.rowcount or 0),
        },
        reason=reason,
    )
    await db.commit()
    target = await load_product(db, target.id) or target
    counts = await _inspection_counts(db, [target.id])
    return _detail(target, inspection_count=counts.get(target.id, 0))
