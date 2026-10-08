"""Reject sensitive data accidentally added to the generated conversation logs.

This is a conservative shape-based check. It cannot determine whether a value
belongs to a real person, so phone, address and credential-shaped values are
rejected even when someone intended them as examples. Findings never print the
matched value.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

SENSITIVE_PATTERNS = {
    "mobile_phone": re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d[- ]?\d{4}[- ]?\d{4}(?!\d)"),
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b"),
    "china_id_card": re.compile(
        r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"
    ),
    "bank_card": re.compile(r"(?<!\d)\d{16,19}(?!\d)"),
    "china_address": re.compile(r"[\u4e00-\u9fff]{2,40}(?:大道|街道|胡同|路|街|巷|弄)\s*\d{1,5}号"),
    "street_address": re.compile(
        r"\b\d{1,5}\s+[A-Za-z][A-Za-z\s]{2,40}\s+(?:Street|St|Road|Rd|Avenue|Ave|Lane|Ln)\b",
        re.IGNORECASE,
    ),
    "credential": re.compile(
        r"\b(?:Bearer\s+[A-Za-z0-9._~+/-]{12,}|sk-(?:proj-)?[A-Za-z0-9_-]{16,}|"
        r"gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
        r"AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{20,})\b",
        re.IGNORECASE,
    ),
    "credential_assignment": re.compile(
        r"\b(?:api[_-]?key|access[_-]?token|secret(?:_key)?|password)\s*[:=]\s*" r"[\"']?[A-Za-z0-9._~+/-]{12,}",
        re.IGNORECASE,
    ),
}
ADDRESS_KEYS = frozenset({"address", "shipping_address", "delivery_address", "street_address", "详细地址", "收货地址"})
CREDENTIAL_KEYS = frozenset(
    {"api_key", "access_token", "refresh_token", "token", "secret", "secret_key", "password", "authorization", "cookie"}
)
SAFE_EMAIL_DOMAINS = frozenset({"example.com", "example.org", "example.net", "localhost"})
SAFE_EMAIL_SUFFIXES = (".example", ".invalid", ".test")
PLACEHOLDER_ADDRESSES = frozenset({"<redacted>", "redacted", "placeholder", "example", "test"})


def _labels_for_text(value: str) -> set[str]:
    labels: set[str] = set()
    for label, pattern in SENSITIVE_PATTERNS.items():
        if label == "email":
            for match in pattern.finditer(value):
                domain = match.group().rsplit("@", 1)[1].lower()
                if domain not in SAFE_EMAIL_DOMAINS and not domain.endswith(SAFE_EMAIL_SUFFIXES):
                    labels.add(label)
        elif pattern.search(value):
            labels.add(label)
    return labels


def scan_value(value: Any, path: str = "$") -> list[str]:
    """Return paths and finding types, without copying sensitive values."""
    if isinstance(value, dict):
        findings: list[str] = []
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if isinstance(key, str) and key.lower() in ADDRESS_KEYS and isinstance(child, str):
                if child.strip() and child.strip().lower() not in PLACEHOLDER_ADDRESSES:
                    findings.append(f"{child_path}: address_field")
            if isinstance(key, str) and key.lower() in CREDENTIAL_KEYS and isinstance(child, str):
                if child.strip() and child.strip().lower() not in PLACEHOLDER_ADDRESSES:
                    findings.append(f"{child_path}: credential_field")
            findings.extend(scan_value(child, child_path))
        return findings
    if isinstance(value, list):
        return [finding for index, child in enumerate(value) for finding in scan_value(child, f"{path}[{index}]")]
    if isinstance(value, str):
        return [f"{path}: {label}" for label in sorted(_labels_for_text(value))]
    return []


def scan_file(path: Path) -> list[str]:
    findings: list[str] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                findings.append(f"line {line_number}: invalid JSON")
                continue
            if not isinstance(record, dict):
                findings.append(f"line {line_number}: expected object")
                continue
            if record.get("synthetic") is not True:
                findings.append(f"line {line_number}: synthetic marker missing")
            findings.extend(f"line {line_number} {item}" for item in scan_value(record))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=Path("eval/logs.jsonl"))
    args = parser.parse_args()
    try:
        findings = scan_file(args.path)
    except (OSError, UnicodeError) as exc:
        print(f"Cannot scan {args.path}: {type(exc).__name__}", file=sys.stderr)
        return 1
    if findings:
        for finding in findings:
            print(finding, file=sys.stderr)
        print(f"Sensitive data scan failed: {len(findings)} finding(s)", file=sys.stderr)
        return 1
    print(f"Synthetic log scan passed: {args.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
