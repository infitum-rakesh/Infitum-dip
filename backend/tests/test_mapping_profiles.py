"""
Regression tests for Document Mapping Profiles (Document Intelligence:
Dynamic JSON Extraction spec) — mapping a Document's extracted fields
onto a target shape via the EXISTING ColumnMapping/ColumnMappingVersion
versioning system (no second versioning system — see
models/column_mapping.py's module docstring), gated behind
mapping_profiles_enabled (OFF by default).

Covers: the fetch_columns() document/invoice-schema branch, create_mapping
mutual-exclusivity validation, the full create -> validate -> publish ->
apply lifecycle against a real document (rename + constant_value +
transformation), lookup-row rejection, unpublished-version rejection,
document-type-mismatch rejection, and DocumentMappedOutput's append-only
history. Exercises real production code throughout (mapping_service,
document_mapping_service, mapping_execution, transform_engine) against
the real test SQLite DB — no mocks, since nothing here crosses a genuine
external-network boundary.
"""
import pytest

from app.models.column_mapping import ColumnMapping
from app.models.document import Document, DocumentStatus
from app.schemas.mapping import ObjectRef
from app.services import document_mapping_service, mapping_service


def _doc(organization_id: int, *, document_type: str = "INVOICE", fields=None) -> Document:
    fields = fields if fields is not None else [
        {"name": "invoice_number", "type": "string", "value": "INV-100"},
        {"name": "vendor_name", "type": "string", "value": "acme corp"},
    ]
    return Document(
        organization_id=organization_id, filename="invoice.pdf", storage_path="/tmp/invoice.pdf",
        extraction_result={"document_type": document_type, "fields": fields},
    )


async def _create_published_mapping(db, *, organization_id: int, mappings: list[dict], document_type: str = "INVOICE"):
    mapping = await mapping_service.create_mapping(
        db, organization_id=organization_id, name="Invoice -> Invoice Schema",
        source=ObjectRef(document_type=document_type), target=ObjectRef(target_format="INVOICE_SCHEMA"),
        mappings=mappings, created_by=None,
    )
    version = await mapping_service.get_version(db, mapping.id, 1)
    await mapping_service.run_validate_version(db, organization_id=organization_id, version=version)
    await db.refresh(version)
    version = await mapping_service.publish_version(db, mapping=mapping, version=version, actor_id=None)
    await db.refresh(mapping)
    return mapping, version


# ── fetch_columns() branching ───────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_columns_document_source_returns_taxonomy_fields(db, make_org):
    org = await make_org()
    cols = await mapping_service.fetch_columns(db, ObjectRef(document_type="INVOICE"), org.id)
    names = {c.name for c in cols}
    assert "invoice_number" in names
    assert "vendor_name" in names
    # Table-shaped fields (line_items) are excluded — scalar-only source.
    assert "line_items" not in names


@pytest.mark.asyncio
async def test_fetch_columns_invoice_schema_target_returns_allowlisted_columns(db, make_org):
    org = await make_org()
    cols = await mapping_service.fetch_columns(db, ObjectRef(target_format="INVOICE_SCHEMA"), org.id)
    names = {c.name for c in cols}
    assert "invoice_number" in names
    assert "total" in names
    # Internal workflow/bookkeeping columns must never be offered as a
    # mappable target.
    assert "status" not in names
    assert "final_decision" not in names
    assert "erp_connection_id" not in names


# ── create_mapping mutual-exclusivity validation ────────────────────

@pytest.mark.asyncio
async def test_create_mapping_rejects_both_document_type_and_connection_id(db, make_org):
    org = await make_org()
    with pytest.raises(mapping_service.MappingValidationError):
        await mapping_service.create_mapping(
            db, organization_id=org.id, name="Bad",
            source=ObjectRef(document_type="INVOICE", connection_id=1), target=ObjectRef(target_format="INVOICE_SCHEMA"),
            mappings=[], created_by=None,
        )


@pytest.mark.asyncio
async def test_create_mapping_rejects_neither_document_type_nor_connection_id(db, make_org):
    org = await make_org()
    with pytest.raises(mapping_service.MappingValidationError):
        await mapping_service.create_mapping(
            db, organization_id=org.id, name="Bad",
            source=ObjectRef(), target=ObjectRef(target_format="INVOICE_SCHEMA"),
            mappings=[], created_by=None,
        )


@pytest.mark.asyncio
async def test_create_mapping_document_source_persists_discriminators(db, make_org):
    org = await make_org()
    mapping = await mapping_service.create_mapping(
        db, organization_id=org.id, name="Invoice Profile",
        source=ObjectRef(document_type="INVOICE"), target=ObjectRef(target_format="INVOICE_SCHEMA"),
        mappings=[{"id": "m1", "target_column": "invoice_number", "source_columns": ["invoice_number"]}],
        created_by=None,
    )
    assert mapping.source_document_type == "INVOICE"
    assert mapping.source_connection_id is None
    assert mapping.target_format == "INVOICE_SCHEMA"
    assert mapping.target_connection_id is None


# ── apply_mapping_profile_to_document() — real end-to-end execution ──

@pytest.mark.asyncio
async def test_apply_mapping_profile_rename_constant_and_transformation(db, make_org):
    org = await make_org()
    mapping, version = await _create_published_mapping(
        db, organization_id=org.id,
        mappings=[
            {"id": "m1", "target_column": "invoice_number", "source_columns": ["invoice_number"]},
            {"id": "m2", "target_column": "vendor_name", "source_columns": ["vendor_name"], "transformation": "UPPER(vendor_name)"},
            {"id": "m3", "target_column": "currency", "source_columns": [], "constant_value": "USD"},
        ],
    )
    doc = _doc(org.id)
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    output = await document_mapping_service.apply_mapping_profile_to_document(
        db, document=doc, mapping_id=mapping.id, organization_id=org.id, created_by=None,
    )

    assert output.status == "SUCCESS"
    assert output.mapping_version == 1
    assert output.target_format == "INVOICE_SCHEMA"
    assert output.row_count == 1
    row = output.rows[0]
    assert row["invoice_number"] == "INV-100"
    assert row["vendor_name"] == "ACME CORP"
    assert row["currency"] == "USD"


@pytest.mark.asyncio
async def test_apply_mapping_profile_rejects_lookup_rows(db, make_org):
    org = await make_org()
    mapping, _version = await _create_published_mapping(
        db, organization_id=org.id,
        mappings=[
            {
                "id": "m1", "target_column": "invoice_number", "source_columns": ["invoice_number"],
                "lookup": {"table": "x", "key_column": "k", "return_column": "v"},
            },
        ],
    )
    doc = _doc(org.id)
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    with pytest.raises(document_mapping_service.DocumentMappingError, match="lookup"):
        await document_mapping_service.apply_mapping_profile_to_document(
            db, document=doc, mapping_id=mapping.id, organization_id=org.id, created_by=None,
        )


@pytest.mark.asyncio
async def test_apply_mapping_profile_rejects_unpublished_version(db, make_org):
    org = await make_org()
    mapping = await mapping_service.create_mapping(
        db, organization_id=org.id, name="Draft Profile",
        source=ObjectRef(document_type="INVOICE"), target=ObjectRef(target_format="INVOICE_SCHEMA"),
        mappings=[{"id": "m1", "target_column": "invoice_number", "source_columns": ["invoice_number"]}],
        created_by=None,
    )
    doc = _doc(org.id)
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    with pytest.raises(document_mapping_service.DocumentMappingError, match="PUBLISHED"):
        await document_mapping_service.apply_mapping_profile_to_document(
            db, document=doc, mapping_id=mapping.id, organization_id=org.id, created_by=None,
        )


@pytest.mark.asyncio
async def test_apply_mapping_profile_rejects_document_type_mismatch(db, make_org):
    org = await make_org()
    mapping, _version = await _create_published_mapping(
        db, organization_id=org.id, document_type="INVOICE",
        mappings=[{"id": "m1", "target_column": "invoice_number", "source_columns": ["invoice_number"]}],
    )
    doc = _doc(org.id, document_type="PURCHASE_ORDER")
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    with pytest.raises(document_mapping_service.DocumentMappingError, match="document_type"):
        await document_mapping_service.apply_mapping_profile_to_document(
            db, document=doc, mapping_id=mapping.id, organization_id=org.id, created_by=None,
        )


@pytest.mark.asyncio
async def test_apply_mapping_profile_rejects_mapping_whose_source_is_not_a_document(db, make_org):
    """A standard connector-sourced mapping (source_document_type=None —
    the shape every pre-existing ColumnMapping row has) must be rejected
    by the document-apply path with a clear error, not silently
    misinterpreted. Constructed directly (bypassing create_mapping's own
    discriminator validation, which rightly refuses to create this state
    today) to simulate that pre-existing-row shape."""
    org = await make_org()
    mapping = ColumnMapping(
        organization_id=org.id, name="Not a document profile",
        source_connection_id=None, source_document_type=None,
        target_connection_id=None, target_format="INVOICE_SCHEMA",
        mappings=[], status="DRAFT", latest_version=1, current_published_version=None,
    )
    db.add(mapping)
    await db.commit()
    await db.refresh(mapping)

    doc = _doc(org.id)
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    with pytest.raises(document_mapping_service.DocumentMappingError, match="document_type"):
        await document_mapping_service.apply_mapping_profile_to_document(
            db, document=doc, mapping_id=mapping.id, organization_id=org.id, created_by=None,
        )


@pytest.mark.asyncio
async def test_apply_mapping_profile_is_append_only_history(db, make_org):
    """Re-running the same mapping against the same document produces a
    NEW DocumentMappedOutput row, not an overwrite — "preserve historical
    outputs" taken literally."""
    org = await make_org()
    mapping, _version = await _create_published_mapping(
        db, organization_id=org.id,
        mappings=[{"id": "m1", "target_column": "invoice_number", "source_columns": ["invoice_number"]}],
    )
    doc = _doc(org.id)
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    out1 = await document_mapping_service.apply_mapping_profile_to_document(
        db, document=doc, mapping_id=mapping.id, organization_id=org.id, created_by=None,
    )
    out2 = await document_mapping_service.apply_mapping_profile_to_document(
        db, document=doc, mapping_id=mapping.id, organization_id=org.id, created_by=None,
    )
    assert out1.id != out2.id

    history = await document_mapping_service.list_mapped_outputs(db, doc.id, org.id)
    assert len(history) == 2
    assert history[0].id == out2.id  # newest-first


@pytest.mark.asyncio
async def test_apply_mapping_profile_requires_extracted_fields(db, make_org):
    org = await make_org()
    mapping, _version = await _create_published_mapping(
        db, organization_id=org.id,
        mappings=[{"id": "m1", "target_column": "invoice_number", "source_columns": ["invoice_number"]}],
    )
    doc = _doc(org.id, fields=[])
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    with pytest.raises(document_mapping_service.DocumentMappingError, match="no extracted fields"):
        await document_mapping_service.apply_mapping_profile_to_document(
            db, document=doc, mapping_id=mapping.id, organization_id=org.id, created_by=None,
        )


# ── Mapping Editor UI support — sample documents + dry-run preview ──

@pytest.mark.asyncio
async def test_list_sample_documents_finds_completed_documents_of_type(db, make_org):
    org = await make_org()
    doc = _doc(org.id, document_type="INVOICE")
    doc.status = DocumentStatus.COMPLETED
    db.add(doc)
    await db.commit()

    matches = await document_mapping_service.list_sample_documents(db, organization_id=org.id, document_type="INVOICE")
    assert len(matches) == 1
    assert matches[0]["id"] == doc.id
    assert matches[0]["filename"] == doc.filename


@pytest.mark.asyncio
async def test_list_sample_documents_excludes_other_document_types(db, make_org):
    org = await make_org()
    doc = _doc(org.id, document_type="PURCHASE_ORDER")
    doc.status = DocumentStatus.COMPLETED
    db.add(doc)
    await db.commit()

    matches = await document_mapping_service.list_sample_documents(db, organization_id=org.id, document_type="INVOICE")
    assert matches == []


@pytest.mark.asyncio
async def test_preview_mapping_against_document_does_not_persist(db, make_org):
    org = await make_org()
    doc = _doc(org.id)
    doc.status = DocumentStatus.COMPLETED
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    result = await document_mapping_service.preview_mapping_against_document(
        db, document=doc, source_document_type="INVOICE", target_format="INVOICE_SCHEMA",
        mappings=[
            {"id": "m1", "target_column": "invoice_number", "source_columns": ["invoice_number"]},
            {"id": "m2", "target_column": "vendor_name", "source_columns": ["vendor_name"], "transformation": "UPPER(vendor_name)"},
        ],
        organization_id=org.id,
    )
    assert result["row_count"] == 1
    assert result["rows"][0]["invoice_number"] == "INV-100"
    assert result["rows"][0]["vendor_name"] == "ACME CORP"

    history = await document_mapping_service.list_mapped_outputs(db, doc.id, org.id)
    assert history == []  # a preview is a dry-run — never recorded as history


@pytest.mark.asyncio
async def test_preview_mapping_against_document_rejects_lookup_rows(db, make_org):
    org = await make_org()
    doc = _doc(org.id)
    doc.status = DocumentStatus.COMPLETED
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    with pytest.raises(document_mapping_service.DocumentMappingError, match="lookup"):
        await document_mapping_service.preview_mapping_against_document(
            db, document=doc, source_document_type="INVOICE", target_format="INVOICE_SCHEMA",
            mappings=[{
                "id": "m1", "target_column": "invoice_number", "source_columns": ["invoice_number"],
                "lookup": {"table": "x", "key_column": "k", "return_column": "v"},
            }],
            organization_id=org.id,
        )


# ── Feature flag default ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mapping_profiles_enabled_defaults_off():
    from app.models.settings import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["document_intelligence"]["mapping_profiles_enabled"] is False
