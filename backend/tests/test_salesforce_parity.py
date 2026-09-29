from app.connectors.application.salesforce import _normalize_sfdc_value, SalesforceConnector


def test_sfdc_datetime_normalization():
    # Real bug fix (Phase 11): ISO datetime with Z suffix must normalize to
    # the valid 4-digit +0000 UTC offset, matching Salesforce's own REST
    # API datetime format — the old "+000" (3 digits) is not a valid
    # ISO-8601/RFC offset and fails to parse under both Python's own
    # datetime.fromisoformat() and dateutil.parser.parse(), breaking
    # checkpoint.py's decode_key() for any DateTime key-range column
    # sourced via Salesforce's Bulk API 2.0.
    val_z = "2021-01-20T15:56:42.000Z"
    assert _normalize_sfdc_value(val_z) == "2021-01-20T15:56:42.000+0000"
    from datetime import datetime
    datetime.fromisoformat(_normalize_sfdc_value(val_z))  # must not raise

    # Plain ISO without millis
    val_z_short = "2021-01-20T15:56:42Z"
    assert _normalize_sfdc_value(val_z_short) == "2021-01-20T15:56:42+0000"
    datetime.fromisoformat(_normalize_sfdc_value(val_z_short))  # must not raise

    # Non-Z values or non-datetime strings remain unchanged
    val_other = "2021-01-20T15:56:42.000+0000"
    assert _normalize_sfdc_value(val_other) == "2021-01-20T15:56:42.000+0000"

    text_val = "Hello ಕನ್ನಡ தமிழ்"
    assert _normalize_sfdc_value(text_val) == "Hello ಕನ್ನಡ தமிழ்"


def test_sfdc_compound_address_field_expansion():
    # Verify that queryable field names expand compound address fields (BillingAddress, ShippingAddress)
    connector = SalesforceConnector({"username": "u", "host": "h"}, {"password": "p"})
    
    # Mock describe fields for Order object containing compound BillingAddress and ShippingAddress
    connector._field_cache["Order"] = [
        {"name": "Id", "type": "id"},
        {"name": "OrderNumber", "type": "string"},
        {"name": "BillingAddress", "type": "address"},
        {"name": "BillingStreet", "type": "textarea"},
        {"name": "BillingCity", "type": "string"},
        {"name": "BillingState", "type": "string"},
        {"name": "BillingPostalCode", "type": "string"},
        {"name": "BillingCountry", "type": "string"},
        {"name": "ShippingAddress", "type": "address"},
        {"name": "ShippingStreet", "type": "textarea"},
        {"name": "ShippingCity", "type": "string"},
        {"name": "ShippingState", "type": "string"},
        {"name": "ShippingPostalCode", "type": "string"},
        {"name": "ShippingCountry", "type": "string"},
    ]

    fields = connector._queryable_field_names("Order")
    
    # BillingAddress and ShippingAddress must expand into their queryable scalar components
    assert "BillingStreet" in fields
    assert "BillingCity" in fields
    assert "BillingState" in fields
    assert "BillingPostalCode" in fields
    assert "BillingCountry" in fields
    
    assert "ShippingStreet" in fields
    assert "ShippingCity" in fields
    assert "ShippingState" in fields
    assert "ShippingPostalCode" in fields
    assert "ShippingCountry" in fields

    # Main address compound placeholder must NOT be in the SOQL field list directly
    assert "BillingAddress" not in fields
    assert "ShippingAddress" not in fields


def test_unicode_character_integrity():
    # Non-Latin characters across Kannada, Hindi, Tamil, Japanese, Chinese, Arabic
    kannada = "ಬೆಂಗಳೂರು"
    hindi = "ग्राहक"
    tamil = "சென்னை"
    japanese = "東京"
    chinese = "北京"
    arabic = "دبي"

    for text in [kannada, hindi, tamil, japanese, chinese, arabic]:
        normalized = _normalize_sfdc_value(text)
        assert normalized == text
