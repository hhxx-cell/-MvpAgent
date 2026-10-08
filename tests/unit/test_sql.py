"""Security and correctness checks for the SQL boundary."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agent.sql import (
    SchemaCatalog,
    SQLExecutionError,
    SQLValidationError,
    UnsupportedSQLQuestion,
    ValidatedQuery,
    execute_sql,
    generate_sql,
    run_sql_with_corrections,
    validate_sql,
)


@pytest.fixture
def business_db(tmp_path):
    path = tmp_path / "ecommerce.db"
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE orders (
          order_id TEXT PRIMARY KEY, user_id TEXT, tenant_id TEXT,
          sku_id TEXT, quantity INTEGER, amount_cents INTEGER, status INTEGER,
          created_at TEXT, updated_at TEXT, currency TEXT
        );
        CREATE TABLE logistics (
          logistics_id TEXT PRIMARY KEY, order_id TEXT, carrier TEXT,
          tracking_no TEXT, status INTEGER, updated_at TEXT, delivered_at TEXT
        );
        CREATE TABLE products (
          sku_id TEXT PRIMARY KEY, name TEXT, category TEXT, price_cents INTEGER
        );
        INSERT INTO orders VALUES
          ('O00001','alice','tenantA','S1',1,1299,3,'2026-08-02','2026-08-04','CNY'),
          ('O00002','bob','tenantA','S1',2,2598,3,'2026-08-03','2026-08-05','CNY'),
          ('O00003','alice','tenantB','S1',1,9999,3,'2026-08-06','2026-08-07','CNY');
        INSERT INTO logistics VALUES
          ('L1','O00001','test','TRACK1',2,'2026-08-04',NULL),
          ('L2','O00002','test','TRACK2',2,'2026-08-05',NULL),
          ('L3','O00003','test','TRACK3',2,'2026-08-07',NULL);
        INSERT INTO products VALUES ('S1','红色鞋子','鞋类',1299);
        """)
    connection.commit()
    connection.close()
    catalog = SchemaCatalog.from_mapping(
        {
            "orders": [
                "order_id",
                "user_id",
                "tenant_id",
                "sku_id",
                "quantity",
                "amount_cents",
                "status",
                "created_at",
                "updated_at",
                "currency",
            ],
            "logistics": [
                "logistics_id",
                "order_id",
                "carrier",
                "tracking_no",
                "status",
                "updated_at",
                "delivered_at",
            ],
            "products": ["sku_id", "name", "category", "price_cents"],
        },
        status_codes={
            "orders": {
                0: "待支付",
                1: "已支付",
                2: "已发货",
                3: "已完成",
                4: "退款处理中",
                5: "退款失败",
                6: "已退款",
                7: "已取消",
            }
        },
    )
    return path, catalog


def _run(sql, business_db, *, user="alice", tenant="tenantA", params=None):
    path, catalog = business_db
    validated = validate_sql(sql, user, catalog, params, tenant_id=tenant)
    return execute_sql(validated, path)


def test_orders_are_scoped_by_user_and_tenant(business_db):
    rows = _run("SELECT o.order_id, o.amount_cents FROM orders AS o ORDER BY o.order_id", business_db)
    assert rows == [{"order_id": "O00001", "amount_cents": 1299}]
    assert _run("SELECT COUNT(o.order_id) AS n FROM orders o", business_db) == [{"n": 1}]


def test_direct_logistics_access_is_scoped(business_db):
    rows = _run("SELECT l.order_id, l.tracking_no FROM logistics l", business_db)
    assert rows == [{"order_id": "O00001", "tracking_no": "TRACK1"}]


def test_cte_union_and_subquery_are_all_scoped(business_db):
    query = (
        "WITH owned AS (SELECT o.order_id FROM orders o), "
        "shipped AS (SELECT l.order_id FROM logistics l) "
        "SELECT owned.order_id FROM owned "
        "UNION SELECT shipped.order_id FROM shipped "
        "UNION SELECT nested.order_id FROM "
        "(SELECT o.order_id FROM orders o) AS nested"
    )
    assert _run(query, business_db) == [{"order_id": "O00001"}]


def test_exists_subquery_is_scoped(business_db):
    query = (
        "SELECT o.order_id FROM orders o "
        "WHERE EXISTS (SELECT l.order_id FROM logistics l WHERE l.order_id = o.order_id)"
    )
    assert _run(query, business_db) == [{"order_id": "O00001"}]


@pytest.mark.parametrize(
    "candidate",
    [
        "SELECT * FROM orders",
        "SELECT COUNT(*) FROM orders",
        "SELECT order_id FROM orders; DROP TABLE orders",
        "DELETE FROM orders",
        "PRAGMA table_info(orders)",
        "ATTACH DATABASE 'other.db' AS other",
        "SELECT name FROM sqlite_master",
        "SELECT load_extension('evil') FROM orders",
        "SELECT user_id FROM orders",
        "SELECT order_id FROM orders -- bypass",
        "WITH RECURSIVE x AS (SELECT order_id FROM orders) SELECT order_id FROM x",
    ],
)
def test_unsafe_sql_is_rejected(candidate, business_db):
    _, catalog = business_db
    with pytest.raises(SQLValidationError):
        validate_sql(candidate, "alice", catalog, tenant_id="tenantA")


def test_parameter_binding_cannot_override_authorization(business_db):
    path, catalog = business_db
    query = validate_sql(
        "SELECT o.order_id FROM orders o WHERE o.order_id = :order_id",
        "alice",
        catalog,
        {"order_id": "O00002"},
        tenant_id="tenantA",
    )
    assert execute_sql(query, path) == []
    for params in ({"auth_principal_id": "bob"}, {"order_id": "O00001"}):
        with pytest.raises(SQLValidationError):
            validate_sql("SELECT o.order_id FROM orders o", "alice", catalog, params, tenant_id="tenantA")


def test_row_limit_and_provenance(business_db):
    path, catalog = business_db
    query = validate_sql(
        "SELECT o.order_id FROM orders o LIMIT 99999",
        "alice",
        catalog,
        tenant_id="tenantA",
        max_rows=1,
    )
    assert "LIMIT 1" in query.sql
    assert len(execute_sql(query, path)) == 1
    with pytest.raises(TypeError):
        ValidatedQuery("DELETE FROM orders", {}, 100)
    with pytest.raises(TypeError):
        execute_sql("SELECT order_id FROM orders", path)  # type: ignore[arg-type]


def test_missing_tenant_fails_closed(business_db):
    _, catalog = business_db
    with pytest.raises(SQLValidationError) as error:
        validate_sql("SELECT o.order_id FROM orders o", "alice", catalog, tenant_id=None)
    assert error.value.code == "FORBIDDEN_RESOURCE"


def test_generator_last_month_is_bound_and_accurate(business_db):
    path, catalog = business_db
    fixed_now = datetime(2026, 9, 24, tzinfo=timezone(timedelta(hours=8)))
    candidate = generate_sql("上个月消费金额是多少", catalog, now=fixed_now)
    assert candidate.parameters == {
        "spent_status_0": 1,
        "spent_status_1": 2,
        "spent_status_2": 3,
        "spent_status_3": 4,
        "spent_status_4": 5,
        "start_date": "2026-08-01",
        "end_date": "2026-09-01",
    }
    query = validate_sql(candidate.sql, "alice", catalog, candidate.parameters, tenant_id="tenantA")
    assert execute_sql(query, path) == [{"total_cents": 1299, "order_count": 1}]


def test_spending_template_requires_status_dictionary(business_db):
    _, catalog = business_db
    catalog_without_codes = SchemaCatalog.from_mapping(catalog.columns)
    with pytest.raises(UnsupportedSQLQuestion):
        generate_sql("上个月买了多少钱", catalog_without_codes)


def test_correctable_execution_error_revalidates_replacement(business_db):
    path, catalog = business_db
    bad = "SELECT order_id FROM orders o JOIN logistics l ON l.order_id = o.order_id"
    fixed = "SELECT o.order_id FROM orders o JOIN logistics l ON l.order_id = o.order_id"
    result = run_sql_with_corrections(
        bad,
        "alice",
        "tenantA",
        catalog,
        path,
        corrector=lambda sql, code, schema: fixed,
    )
    assert result.rows == [{"order_id": "O00001"}]
    assert result.corrections == 1


def test_default_corrector_repairs_ambiguous_order_id(business_db):
    path, catalog = business_db
    ambiguous = "SELECT order_id FROM orders o JOIN logistics l ON l.order_id = o.order_id"
    result = run_sql_with_corrections(ambiguous, "alice", "tenantA", catalog, path)
    assert result.corrections == 1
    assert result.rows == [{"order_id": "O00001"}]


def test_default_corrector_translates_date_trunc_to_sqlite(business_db):
    path, catalog = business_db
    candidate = (
        "SELECT DATE_TRUNC('month', o.created_at) AS month_start, "
        "COUNT(o.order_id) AS order_count FROM orders o GROUP BY month_start"
    )
    result = run_sql_with_corrections(candidate, "alice", "tenantA", catalog, path)
    assert result.corrections == 1
    assert result.rows == [{"month_start": "2026-08-01", "order_count": 1}]


def test_correction_stops_after_two_retries(business_db):
    path, catalog = business_db
    bad = "SELECT order_id FROM orders o JOIN logistics l ON l.order_id = o.order_id"
    calls = []

    def unchanged(sql, code, schema):
        calls.append(code)
        return sql

    with pytest.raises(SQLExecutionError):
        run_sql_with_corrections(
            bad,
            "alice",
            "tenantA",
            catalog,
            path,
            corrector=unchanged,
            max_corrections=2,
        )
    assert calls == ["SQL_COLUMN_ERROR", "SQL_COLUMN_ERROR"]
