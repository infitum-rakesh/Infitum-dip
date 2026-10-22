"""Compliance & Verification module — India Business/Tax Identity/
Corporate Document Verification. See verification_orchestrator.py and
verification_status.py for the shared pipeline/status model every
check-type service (identity_verification_service.py today; tax/company/
udyam/moa services in later phases) plugs into.

Note on package location: the original spec sketched a top-level
`app/modules/compliance/` tree; this codebase has no `app/modules/`
precedent anywhere else (models/services/schemas/api are each their own
top-level package), so this module follows the existing
`app/services/approval/`-style nested-subpackage convention instead —
same reuse-what's-already-here principle the rest of this build follows."""
