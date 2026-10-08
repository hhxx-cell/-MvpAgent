"""The opt-in evaluator reads live model config without saving secrets."""

import json
from argparse import Namespace
from pathlib import Path

import pytest

import eval.eval as evaluation
from eval.eval import configured_model_from_env, redact_model_config


def test_configured_model_is_opt_in_and_evidence_is_redacted(tmp_path: Path, monkeypatch) -> None:
    for key in ("LLM_PROVIDER", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / ".env"
    path.write_text(
        "LLM_PROVIDER=openai_compatible\n"
        "LLM_BASE_URL=https://example.test/v1/\n"
        "LLM_MODEL=test-model\n"
        "LLM_API_KEY=test-secret-value\n",
        encoding="utf-8",
    )

    config = configured_model_from_env(path)

    assert config["llm_provider"] == "openai_compatible"
    assert config["llm_model"] == "test-model"
    report = {
        "mode": "in-process (configured model)",
        "cases": [{"note": "test-secret-value at https://example.test/v1/chat/completions"}],
    }
    saved = json.dumps(redact_model_config(report, config))
    assert "test-secret-value" not in saved
    assert "https://example.test/v1" not in saved
    assert "[redacted]" in saved


def test_configured_model_rejects_rules_without_exposing_key(tmp_path: Path, monkeypatch) -> None:
    for key in ("LLM_PROVIDER", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / ".env"
    path.write_text(
        "LLM_PROVIDER=rules\n"
        "LLM_BASE_URL=https://example.test/v1\n"
        "LLM_MODEL=test-model\n"
        "LLM_API_KEY=test-secret-value\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="LLM_PROVIDER") as error:
        configured_model_from_env(path)
    assert "test-secret-value" not in str(error.value)
    assert "https://example.test/v1" not in str(error.value)


def test_live_main_redacts_saved_report_without_calling_model(tmp_path: Path, monkeypatch) -> None:
    config = {
        "llm_provider": "openai_compatible",
        "llm_base_url": "https://example.test/v1",
        "llm_model": "test-model",
        "llm_api_key": "test-secret-value",
    }
    output_json = tmp_path / "live.json"
    output_markdown = tmp_path / "live.md"
    args = Namespace(
        use_configured_model=True,
        in_process=True,
        golden=tmp_path / "unused.jsonl",
        base_url="http://unused",
        output_json=output_json,
        output_markdown=output_markdown,
        baseline_json=None,
        change_note="",
        case_ids=["safe_probe"],
        limit=None,
    )
    monkeypatch.setattr(evaluation, "parse_args", lambda: args)
    monkeypatch.setattr(evaluation, "configured_model_from_env", lambda _: config)
    monkeypatch.setattr(evaluation, "load_golden", lambda _: [{"id": "safe_probe"}])
    monkeypatch.setattr(evaluation, "database_hash", lambda: "synthetic-hash")

    async def fake_evaluate(cases, model_config):
        assert [case["id"] for case in cases] == ["safe_probe"]
        assert model_config == config
        return [
            evaluation.CaseResult(
                case_id="safe_probe",
                category="safe",
                status="passed",
                safety_critical=False,
                checks=[],
                answer_excerpt="test-secret-value https://example.test/v1/chat/completions",
                observed_route=[],
                observed_tools=[],
                duration_seconds=0.1,
            )
        ]

    monkeypatch.setattr(evaluation, "evaluate_in_process", fake_evaluate)

    assert evaluation.main() == 0
    for path in (output_json, output_markdown):
        saved = path.read_text(encoding="utf-8")
        assert "test-secret-value" not in saved
        assert "https://example.test/v1" not in saved
    assert json.loads(output_json.read_text(encoding="utf-8"))["mode"] == "in-process (configured model)"
