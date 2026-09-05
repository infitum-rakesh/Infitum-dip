"""
ModulePrice — the per-module price catalog a Seller edits in Settings.

Where this fits: Plan.price already covers a *fixed bundle's* price, but
there was nowhere to price a single module on its own — needed for two
real gaps found live: (1) enabling a module ad hoc from Purchased Modules
never generated a bill (no price to bill it at), and (2) the new "Custom"
plan option (pick modules individually instead of a fixed Plan) needs a
per-module amount to sum into a total. One row per module, keyed on the
same MODULE_CATALOG keys services/seller/module_service.py already
defines — never a second, drifting module vocabulary.

Seeded with sensible defaults for every catalog module at startup (see
plan_seed.py) so a lookup never comes up empty; a Seller can then edit
any price for real in Settings -> Module Prices.
"""
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import PlatformBase


class ModulePrice(PlatformBase):
    __tablename__ = "module_prices"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    module: Mapped[str] = mapped_column(String(60), unique=True, index=True, nullable=False)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(8), default="USD")

    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<ModulePrice {self.module} {self.currency} {self.price}>"
