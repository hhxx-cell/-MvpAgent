"""The CI scanner should catch leaked data without leaking it in diagnostics."""

from __future__ import annotations

import json

import pytest

from scripts.scan_synthetic_logs import main, scan_file


@pytest.mark.parametrize(
    ("value", "label"),
    [
        ("13800138000", "mobile_phone"),
        ("北京市朝阳区望京街道阜通东大街6号", "china_address"),
        ("alice@company.com", "email"),
        ("110101199001011234", "china_id_card"),
        ("6222021234567890123", "bank_card"),
        ("Bearer abcdefghijklmnopqrstuvwx", "credential"),
        ("api_key=abcdefghijklmnop", "credential_assignment"),
    ],
)
def test_scan_rejects_sensitive_shapes(tmp_path, value, label):
    path = tmp_path / "logs.jsonl"
    path.write_text(json.dumps({"synthetic": True, "turns": [{"content": value}]}) + "\n", encoding="utf-8")
    assert any(label in finding for finding in scan_file(path))


def test_scan_accepts_synthetic_identifiers_and_reserved_contact(tmp_path):
    path = tmp_path / "logs.jsonl"
    path.write_text(
        json.dumps(
            {
                "synthetic": True,
                "user_id": "user_a",
                "order_id": "O00001",
                "contact": "demo@example.com",
                "shipping_address": "<redacted>",
                "turns": [{"content": "请查询 O00001；电话和地址未提供。"}],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert scan_file(path) == []


def test_scan_rejects_unstructured_address_field_and_missing_marker(tmp_path):
    path = tmp_path / "logs.jsonl"
    path.write_text(json.dumps({"shipping_address": "某某小区 3 单元"}) + "\n", encoding="utf-8")
    findings = scan_file(path)
    assert any("address_field" in finding for finding in findings)
    assert any("synthetic marker missing" in finding for finding in findings)


def test_scan_rejects_credential_field_even_without_known_prefix(tmp_path):
    path = tmp_path / "logs.jsonl"
    path.write_text(json.dumps({"synthetic": True, "access_token": "opaque-secret-value"}) + "\n", encoding="utf-8")
    assert any("credential_field" in finding for finding in scan_file(path))


def test_scan_reports_location_without_value(tmp_path, monkeypatch, capsys):
    path = tmp_path / "logs.jsonl"
    phone = "13800138000"
    path.write_text(json.dumps({"synthetic": True, "turns": [{"content": phone}]}) + "\n", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["scan_synthetic_logs.py", str(path)])
    assert main() == 1
    output = capsys.readouterr().err
    assert "line 1 $.turns[0].content: mobile_phone" in output
    assert phone not in output
