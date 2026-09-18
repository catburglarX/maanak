"""Products, package variants and their identifier history.

A ``Product`` is one package variant of one commodity from one brand: the thing an
officer actually holds. Identifiers (GTIN and other barcodes) and responsible
parties change over a product's life, so both are kept as history tables rather
than overwritten columns.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..domain.enums import PackageType, QuantityKind, values
from .base import Base, TimestampMixin, enum_check, uuid_pk, version_column


class Product(Base, TimestampMixin):
    """One package variant.

    ``declared_net_quantity`` is what the label claims, held as NUMERIC so that
    legal arithmetic never touches binary floating point.
    """

    __tablename__ = "products"
    __table_args__ = (
        enum_check("package_type", values(PackageType), "products_package_type"),
        enum_check("quantity_kind", values(QuantityKind), "products_quantity_kind"),
        Index("ix_products_brand_name", "brand", "name"),
        Index("ix_products_search", "name", "brand", "commodity_category"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    version: Mapped[int] = version_column()

    brand: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    #: Rule 2(a)-style common or generic name, distinct from the brand name.
    common_generic_name: Mapped[str | None] = mapped_column(String(240))
    commodity_category: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    #: Set when the product is a food article; drives the food-label tool.
    is_food: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    package_type: Mapped[str] = mapped_column(String(30), nullable=False, default=PackageType.OTHER)
    quantity_kind: Mapped[str | None] = mapped_column(String(20))
    declared_net_quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    declared_net_quantity_unit: Mapped[str | None] = mapped_column(String(16))
    #: Net quantity converted to the SI base unit for the dimension (g, ml, m, count).
    net_quantity_base: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))

    is_imported: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    country_of_origin: Mapped[str | None] = mapped_column(String(80))
    is_multipiece: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pieces_per_package: Mapped[int | None] = mapped_column()

    notes: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), index=True
    )

    identifiers: Mapped[list[ProductIdentifier]] = relationship(
        back_populates="product", cascade="all, delete-orphan", lazy="selectin"
    )
    responsible_parties: Mapped[list[ResponsibleParty]] = relationship(
        back_populates="product", cascade="all, delete-orphan", lazy="selectin"
    )

    __mapper_args__ = {"version_id_col": version}

    @property
    def primary_gtin(self) -> str | None:
        for identifier in self.identifiers:
            if identifier.is_primary and identifier.scheme == "gtin":
                return identifier.value
        return None

    @property
    def display_name(self) -> str:
        return f"{self.brand} {self.name}".strip()


class ProductIdentifier(Base, TimestampMixin):
    """A barcode or other identifier observed on a package.

    A barcode is evidence of an identifier printed on a package. It is not proof
    that the package is genuine, so ``officer_confirmed`` is recorded separately.
    """

    __tablename__ = "product_identifiers"
    __table_args__ = (
        UniqueConstraint(
            "scheme", "value", "product_id", name="uq_product_identifiers_scheme_value_product_id"
        ),
        Index("ix_product_identifiers_value", "value"),
        enum_check(
            "scheme",
            ("gtin", "ean8", "upca", "itf14", "isbn", "internal", "other"),
            "product_identifiers_scheme",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scheme: Mapped[str] = mapped_column(String(20), nullable=False, default="gtin")
    value: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Result of the GS1 modulo-10 check-digit calculation at capture time.
    check_digit_valid: Mapped[bool | None] = mapped_column(Boolean)
    barcode_symbology: Mapped[str | None] = mapped_column(String(40))
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    observed_from: Mapped[date | None] = mapped_column(Date)
    observed_to: Mapped[date | None] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="officer_entry")
    officer_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    recorded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    product: Mapped[Product] = relationship(back_populates="identifiers")


class ResponsibleParty(Base, TimestampMixin):
    """Manufacturer, packer, importer or marketer recorded for a product."""

    __tablename__ = "responsible_parties"
    __table_args__ = (
        enum_check(
            "party_role",
            ("manufacturer", "packer", "importer", "marketer", "brand_owner"),
            "responsible_parties_party_role",
        ),
        Index("ix_responsible_parties_product_role", "product_id", "party_role"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    party_role: Mapped[str] = mapped_column(String(20), nullable=False)
    legal_name: Mapped[str] = mapped_column(String(240), nullable=False)
    address_line: Mapped[str | None] = mapped_column(Text)
    locality: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(80))
    pin_code: Mapped[str | None] = mapped_column(String(12))
    country: Mapped[str | None] = mapped_column(String(80))
    consumer_care_name: Mapped[str | None] = mapped_column(String(160))
    consumer_care_email: Mapped[str | None] = mapped_column(String(320))
    consumer_care_phone: Mapped[str | None] = mapped_column(String(40))
    observed_from: Mapped[date | None] = mapped_column(Date)
    observed_to: Mapped[date | None] = mapped_column(Date)
    source_evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("evidence.id", ondelete="SET NULL")
    )
    recorded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    product: Mapped[Product] = relationship(back_populates="responsible_parties")


class ProductMergeRecord(Base):
    """Audit of a controlled product merge, kept so a merge can be explained."""

    __tablename__ = "product_merge_records"

    id: Mapped[uuid.UUID] = uuid_pk()
    source_product_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    target_product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    merged_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    merged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
