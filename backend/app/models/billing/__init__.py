"""
Multi-Subscription Billing & Payments Platform (2026) — invoicing,
payments, payment gateways and in-app notifications. Kept as its own
package (mirrors services/billing/ and payment_gateways/) rather than
flat files in app/models/ directly, purely because of how many tables
this feature adds — no functional difference in how SQLAlchemy sees
them; every model here still registers on PlatformBase.metadata exactly
like every other model in this codebase.
"""
