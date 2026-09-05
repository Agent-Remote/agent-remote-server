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


def test_ci_and_release_preserve_performance_contracts() -> None:
    """CI 与发布流程必须保留可复现依赖、缓存、超时和路径分流。"""

    ci = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    release = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    prepare = Path(".github/workflows/prepare-release.yml").read_text(encoding="utf-8")
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    for workflow in (ci, release, prepare):
        assert "concurrency:" in workflow
        assert "timeout-minutes:" in workflow
    for fragment in (
        "dorny/paths-filter@v4.0.3",
        "enable-cache: true",
        "uv sync --frozen",
        "fail_ci_if_error: false",
    ):
        assert fragment in ci
    assert "cache-from: type=gha" in release
    assert "cache-to: type=gha,mode=max" in release
    assert "COPY pyproject.toml uv.lock ./" in dockerfile
    assert "ENV UV_HTTP_RETRIES=10" in dockerfile
    assert "ENV UV_HTTP_TIMEOUT=60" in dockerfile
    assert "uv sync --frozen --no-dev --no-install-project" in dockerfile
    assert dockerfile.index("uv sync --frozen --no-dev --no-install-project") < dockerfile.index(
        "COPY README.md LICENSE ./"
    )
