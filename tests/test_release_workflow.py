from pathlib import Path


def test_release_workflow_binds_downloadable_supply_chain_evidence() -> None:
    """服务端 release 必须固定镜像摘要并发布可下载的供应链证据。"""

    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    required_fragments = (
        'test "$GITHUB_REF" = "refs/tags/v${version}"',
        "steps.build.outputs.digest",
        "cosign verify",
        "anchore/sbom-action@",
        "actions/attest-build-provenance@",
        "pip-audit==2.9.0",
        "pip-audit.json",
        "pip-audit.json.sigstore.json",
        "spdx.json.sigstore.json",
        'cosign sign-blob --yes --bundle "$sbom.sigstore.json" "$sbom"',
        "provenance.jsonl",
        "sha256sum --check",
    )

    for fragment in required_fragments:
        assert fragment in workflow


def test_ci_enforces_every_server_quality_contract() -> None:
    """服务端 CI 必须执行格式、覆盖率、文档和空白字符门禁。"""

    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    required_fragments = (
        "uv run ruff format --check .",
        "uv run ruff check .",
        "uv run mypy",
        "--cov-fail-under=70",
        "uv run python scripts/check_docstrings.py",
        "git diff --check",
    )

    for fragment in required_fragments:
        assert fragment in workflow
