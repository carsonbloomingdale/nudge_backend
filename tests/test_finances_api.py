from __future__ import annotations

import time
from datetime import date


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_transactions_import_dedupe_and_pie(client, register_user):
    token, _, _ = register_user(suffix="finance_1")
    h = _auth_headers(token)

    create = client.post(
        "/api/finances/transactions",
        headers=h,
        json={
            "occurred_on": "2026-03-20",
            "amount_minor": -1599,
            "currency": "USD",
            "merchant": "Coffee Bar",
            "description": "morning coffee",
        },
    )
    assert create.status_code == 200, create.text
    tid = create.json()["transaction_id"]

    imp = client.post(
        "/api/finances/transactions/import",
        headers=h,
        json={
            "source": "csv",
            "fuzzy_days": 2,
            "items": [
                {
                    "occurred_on": "2026-03-21",
                    "amount_minor": -1599,
                    "currency": "USD",
                    "merchant": "coffee bar",
                    "description": "same charge",
                },
                {
                    "occurred_on": "2026-03-22",
                    "amount_minor": -4000,
                    "currency": "USD",
                    "merchant": "Grocery Mart",
                    "description": "groceries",
                },
            ],
        },
    )
    assert imp.status_code == 200, imp.text
    summary = imp.json()
    assert summary["rows_total"] == 2
    assert summary["duplicates_skipped"] == 1
    assert summary["net_added"] == 1

    hide = client.patch(f"/api/finances/transactions/{tid}", headers=h, json={"is_hidden_from_charts": True})
    assert hide.status_code == 200, hide.text

    pie = client.get("/api/finances/analytics/pie?from_date=2026-03-01&to_date=2026-03-31", headers=h)
    assert pie.status_code == 200, pie.text
    pie_body = pie.json()
    assert pie_body["mode"] == "spend"
    assert pie_body["total_minor"] == 4000
    assert len(pie_body["slices"]) == 1


def test_spend_pie_optional_unspent_income_slice(client, register_user):
    token, _, _ = register_user(suffix="finance_8")
    h = _auth_headers(token)

    r1 = client.post(
        "/api/finances/transactions",
        headers=h,
        json={
            "occurred_on": "2026-03-10",
            "amount_minor": 100000,
            "currency": "USD",
            "merchant": "Payroll",
            "description": "income",
        },
    )
    assert r1.status_code == 200, r1.text
    r2 = client.post(
        "/api/finances/transactions",
        headers=h,
        json={
            "occurred_on": "2026-03-11",
            "amount_minor": -25000,
            "currency": "USD",
            "merchant": "Rent",
            "description": "expense",
        },
    )
    assert r2.status_code == 200, r2.text

    pie = client.get(
        "/api/finances/analytics/pie?from_date=2026-03-01&to_date=2026-03-31&include_unspent_income=true",
        headers=h,
    )
    assert pie.status_code == 200, pie.text
    body = pie.json()
    assert body["mode"] == "spend"
    assert body["income_minor"] == 100000
    assert body["spend_minor"] == 25000
    assert body["unspent_income_minor"] == 75000
    assert any(s["category"] == "unspent_income" and s["amount_minor"] == 75000 for s in body["slices"])


def test_budgets_and_utilization(client, register_user):
    token, _, _ = register_user(suffix="finance_2")
    h = _auth_headers(token)

    tx = client.post(
        "/api/finances/transactions",
        headers=h,
        json={
            "occurred_on": "2026-03-15",
            "amount_minor": 2500,
            "currency": "USD",
            "merchant": "Coffee Spot",
            "description": "coffee",
            "category": "food",
        },
    )
    assert tx.status_code == 200, tx.text

    budget = client.post(
        "/api/finances/budgets",
        headers=h,
        json={
            "category": "food",
            "period": "monthly",
            "period_start": "2026-03-01",
            "period_end": "2026-03-31",
            "amount_minor": 10000,
        },
    )
    assert budget.status_code == 200, budget.text

    util = client.get("/api/finances/analytics/budgets-utilization", headers=h)
    assert util.status_code == 200, util.text
    rows = util.json()
    assert len(rows) == 1
    assert rows[0]["spent_minor"] == 2500
    assert rows[0]["remaining_minor"] == 7500
    assert rows[0]["used_percent"] == 25.0


def test_unknown_categories_normalize_to_other(client, register_user):
    token, _, _ = register_user(suffix="finance_4")
    h = _auth_headers(token)
    tx = client.post(
        "/api/finances/transactions",
        headers=h,
        json={
            "occurred_on": "2026-03-15",
            "amount_minor": 500,
            "currency": "USD",
            "merchant": "Misc",
            "description": "misc",
            "category": "not-a-real-category",
        },
    )
    assert tx.status_code == 200, tx.text
    assert tx.json()["category"] == "other"


def test_import_rows_support_fuzzy_column_names(client, register_user):
    token, _, _ = register_user(suffix="finance_5")
    h = _auth_headers(token)

    r = client.post(
        "/api/finances/transactions/import",
        headers=h,
        json={
            "source": "csv",
            "rows": [
                {
                    "Transaction Date": "2026-03-18",
                    "Value": "12.34",
                    "Payee": "Local Cafe",
                    "Memo": "latte",
                    "Account Name": "Checking",
                },
                {
                    "date": "2026-03-18",
                    "amount": "12.34",
                    "merchant_name": "local cafe",
                    "description": "duplicate row by fuzzy fields",
                },
            ],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rows_total"] == 2
    assert body["duplicates_skipped"] == 1
    assert body["net_added"] == 1


def test_import_rows_ai_header_fallback(client, register_user, monkeypatch):
    token, _, _ = register_user(suffix="finance_6")
    h = _auth_headers(token)

    async def _fake_ai_map(headers):
        _ = headers
        return {"BookedOn": "occurred_on", "TxnValue": "amount_minor", "StoreName": "merchant"}

    import finances_api

    monkeypatch.setattr(finances_api, "_ai_map_import_headers", _fake_ai_map)

    r = client.post(
        "/api/finances/transactions/import",
        headers=h,
        json={
            "rows": [
                {
                    "BookedOn": "2026-03-10",
                    "TxnValue": "55.10",
                    "StoreName": "Market Fresh",
                }
            ]
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rows_total"] == 1
    assert body["net_added"] == 1


def test_import_items_bank_export_shape_with_excel_date(client, register_user):
    token, _, _ = register_user(suffix="finance_7")
    h = _auth_headers(token)
    r = client.post(
        "/api/finances/transactions/import",
        headers=h,
        json={
            "source": "import",
            "fuzzy_days": 30,
            "items": [
                {
                    "Date": 46106.833333333336,
                    "Description": "SUNOCO 0056362700",
                    "Type": "DEBIT_CARD",
                    "Amount": -7.91,
                    "Current balance": 10604.51,
                    "Status": "Posted",
                }
            ],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rows_total"] == 1
    assert body["net_added"] == 1
    assert body["categorization_job_id"] is not None
    assert body["categorization_status"] in {"queued", "running", "completed", "partial"}


def test_ai_categorization_job_polling(client, register_user):
    token, _, _ = register_user(suffix="finance_3")
    h = _auth_headers(token)

    tx = client.post(
        "/api/finances/transactions",
        headers=h,
        json={
            "occurred_on": str(date.today()),
            "amount_minor": 1200,
            "currency": "USD",
            "merchant": "Uber",
            "description": "ride home",
        },
    )
    assert tx.status_code == 200, tx.text

    job = client.post("/api/finances/categorization-jobs", headers=h, json={"uncategorized_only": True})
    assert job.status_code == 200, job.text
    job_id = job.json()["job_id"]

    terminal = {"completed", "partial", "failed"}
    status = None
    for _ in range(30):
        jr = client.get(f"/api/finances/categorization-jobs/{job_id}", headers=h)
        assert jr.status_code == 200, jr.text
        status = jr.json()["status"]
        if status in terminal:
            break
        time.sleep(0.05)
    assert status in terminal

    lst = client.get("/api/finances/transactions", headers=h)
    assert lst.status_code == 200, lst.text
    txs = lst.json()
    assert txs[0]["category"] in {
        "transport",
        "food",
        "entertainment",
        "housing",
        "health",
        "income",
        "utilities",
        "shopping",
        "travel",
        "debt_payments",
        "transfers",
        "other",
    }
