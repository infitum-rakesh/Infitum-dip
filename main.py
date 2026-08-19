from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse


ROOT_DIR = Path(__file__).resolve().parent

app = FastAPI(title="DMS Control Center API", version="1.0.0")


BOOTSTRAP_DATA: dict[str, Any] = {
    "hero": {
        "reliability": 76,
        "datasets": "1,248",
        "incidents": 19,
        "stats": [
            {"value": "99%", "label": "Freshness coverage"},
            {"value": "1.4B", "label": "Rows scanned daily"},
            {"value": "21m", "label": "Median alert resolution"},
        ],
    },
    "dashboard": {
        "metrics": [
            {"label": "Reliability Score", "value": "76", "trend": "Up 4", "detail": "vs yesterday"},
            {"label": "Datasets", "value": "1,248", "trend": "Up 3", "detail": "monitored assets"},
            {"label": "Validation Runs", "value": "574", "trend": "Up 8%", "detail": "success rate"},
            {"label": "Incidents", "value": "19", "trend": "Down 4", "detail": "open incidents"},
        ],
        "runSummary": [
            {"value": "84", "label": "Passed"},
            {"value": "42", "label": "Failed"},
            {"value": "29", "label": "Warning"},
        ],
        "incidents": [
            {"text": "Customer Profile validation failure", "tone": "danger", "age": "1 min ago"},
            {"text": "Country remap anomaly", "tone": "warning", "age": "9 min ago"},
            {"text": "Payments reconciliation failed", "tone": "danger", "age": "21 min ago"},
        ],
        "failingRules": [
            {"rule": "CUSTOMER_COUNTRY_VALID", "failures": 19},
            {"rule": "ORDER_AMOUNT_CHECK", "failures": 14},
            {"rule": "EMAIL_FORMAT_CHECK", "failures": 9},
        ],
        "observability": [
            {"value": "99%", "label": "Freshness"},
            {"value": "74%", "label": "Volume"},
            {"value": "87%", "label": "Null %"},
            {"value": "93%", "label": "Schema"},
            {"value": "4", "label": "Anomalies"},
            {"value": "2", "label": "Open RCA"},
        ],
    },
    "connections": {
        "db": {
            "title": "DB Connections",
            "button": "Add DB",
            "columns": ["ENV", "TYPE", "HOST", "PORT", "DB", "USER", "PASSWORD", "ACTIONS"],
            "rows": [
                ["PROD", "Snowflake", "sf-prod.company.net", "443", "ANALYTICS", "svc_dms_prod", "********", "Edit | Test"],
                ["UAT", "Oracle", "ora-uat.company.net", "1521", "SALES", "svc_dms_uat", "********", "Edit | Test"],
                ["DEV", "PostgreSQL", "pg-dev.company.net", "5432", "sandbox", "svc_dms_dev", "********", "Edit | Test"],
            ],
        },
        "api": {
            "title": "API Connections",
            "button": "Add API",
            "columns": ["ENV", "NAME", "BASE URL", "AUTH", "TIMEOUT", "STATUS", "ACTIONS"],
            "rows": [
                ["PROD", "Customer API", "https://api.company.net/customers", "OAuth2", "30s", "Healthy", "Edit | Test"],
                ["UAT", "Billing API", "https://api-uat.company.net/billing", "Token", "45s", "Healthy", "Edit | Test"],
            ],
        },
        "sftp": {
            "title": "SFTP Connections",
            "button": "Add SFTP",
            "columns": ["ENV", "NAME", "HOST", "PORT", "DIRECTORY", "USER", "STATUS", "ACTIONS"],
            "rows": [
                ["PROD", "Bank Settlement", "sftp.bank.net", "22", "/inbound/settlement", "ops_sync", "Healthy", "Edit | Test"],
                ["UAT", "Vendor Files", "sftp.vendor.net", "22", "/uat/outbound", "vendor_uat", "Warning", "Edit | Test"],
            ],
        },
    },
    "pipelines": {
        "rows": [
            ["Customer_Gold_Load", "/pipelines/customer", "ELT", "Oracle_PROD", "Snowflake_PROD", "Run | Edit"],
            ["Payments_Recon", "/pipelines/payments", "Reconciliation", "Oracle_PROD", "Snowflake_PROD", "Run | Edit"],
            ["Daily_File_Ingest", "/pipelines/files", "SFTP Ingest", "Vendor SFTP", "Raw Bronze", "Run | Edit"],
            ["Country_Mapping_Refresh", "/pipelines/reference", "Reference Load", "SFTP", "Customer Gold", "Run | Edit"],
        ]
    },
}


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "backend": "fastapi"}


@app.get("/api/bootstrap")
def bootstrap() -> dict[str, Any]:
    return BOOTSTRAP_DATA


@app.post("/api/login")
async def login(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid request body") from exc

    email = payload.get("email") or "demo@company.com"
    return JSONResponse(
        {
            "success": True,
            "user": {
                "email": email,
                "role": "Data Operations",
            },
        }
    )


@app.get("/{full_path:path}")
def frontend(full_path: str) -> FileResponse:
    safe_path = Path(full_path).as_posix().lstrip("/")
    candidate = (ROOT_DIR / safe_path).resolve()

    if candidate.is_file() and str(candidate).startswith(str(ROOT_DIR)):
        return FileResponse(candidate)

    return FileResponse(ROOT_DIR / "index.html")