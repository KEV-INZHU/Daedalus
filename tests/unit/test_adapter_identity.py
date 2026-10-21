"""The review cache key names the reviewer precisely enough that a different model never shares a verdict."""

from types import SimpleNamespace

from daedalus.verification.evidence_cache import adapter_identity


def test_http_reviewers_on_different_endpoints_do_not_share_an_identity():
    a = SimpleNamespace(name="openai-compatible", review_model="qwen3-coder", base_url="http://localhost:11434/v1")
    b = SimpleNamespace(name="openai-compatible", review_model="qwen3-coder", base_url="https://openrouter.ai/api/v1")
    assert adapter_identity(a) != adapter_identity(b)
    assert adapter_identity(a) == "openai-compatible:qwen3-coder@http://localhost:11434/v1"


def test_cli_adapters_keep_their_identity_without_an_endpoint():
    assert adapter_identity(SimpleNamespace(name="claude-code", review_model="opus")) == "claude-code:opus"


def test_no_configured_model_means_no_identity():
    assert adapter_identity(SimpleNamespace(name="openai-compatible", base_url="http://x")) is None
