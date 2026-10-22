"""Verification Provider connectors (Compliance & Verification module) —
India business/tax identity and corporate document checks (PAN, GSTIN,
TAN, Aadhaar, MCA, Udyam). See base.py's verification verb cluster
(verify_pan/verify_gstin/...) for the shared contract, and
mock_provider.py for the only implementation today — a local dev/test
gateway, never a live government/KYC integration."""
