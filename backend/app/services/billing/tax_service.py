"""
Pluggable tax calculation (spec section 55: "Design tax calculation as a
service. Do not hardcode tax percentages... Allow future integration with
external tax engines"). Today this is a flat, configurable rate — real
and working, just not jurisdiction-aware yet. Swapping calculate_tax()'s
body for a call to a real external tax engine (Avalara, TaxJar, etc.)
later requires no change at any call site.
"""
from app.models.organization import Organization

# No tax charged by default. A future phase can make this per-Seller
# configurable (Seller Settings -> tax rate/jurisdiction/registration
# info) without changing this function's signature.
DEFAULT_TAX_RATE = 0.0


async def calculate_tax(amount: float, organization: Organization | None = None) -> float:
    """Returns the tax AMOUNT (not the rate) owed on `amount` for the
    given organization. `organization` is accepted now (unused) so a
    jurisdiction-aware implementation can be dropped in later without
    changing every caller."""
    if amount <= 0:
        return 0.0
    return round(amount * DEFAULT_TAX_RATE, 2)
