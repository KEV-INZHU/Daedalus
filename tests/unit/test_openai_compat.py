"""HTTP reviewers: OpenAI-compatible and Anthropic adapters against a local stub server."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from conftest import HUMAN, make_repo
from daedalus import doctor, plugins
from daedalus.adapters import make_adapter
from daedalus.adapters.base import AgentRequest
from daedalus.adapters.openai_compat import HttpReviewAdapter, build_review_prompt
from daedalus.core.errors import CapabilityError
from daedalus.orchestration.ariadne import Ariadne
from daedalus.orchestration.lifecycle import launch_review

CLEAN = {"summary": "looks right", "findings": []}
FOUND = {"summary": "one problem", "findings": [
    {"severity": "major", "title": "Off by one", "evidence": "src/lib.py:1 uses x = 1", "required_change": "fix it"}]}


class Stub:
    """Speaks both API shapes. `script` is a list of (status, body, delay) consumed per request."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.script: list[tuple[int, dict | str, float]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                status, payload, delay = stub.script.pop(0) if stub.script else (200, reply(CLEAN), 0)
                time.sleep(delay)
                data = (payload if isinstance(payload, str) else json.dumps(payload)).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                if status == 429:
                    self.send_header("Retry-After", "0")
                self.end_headers()
                try:
                    self.wfile.write(data)
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def reply(verdict: dict | str, style: str = "openai", tokens: tuple[int, int] = (1000, 200)) -> dict:
    text = verdict if isinstance(verdict, str) else "Reviewed.\n```json\n" + json.dumps(verdict) + "\n```"
    if style == "anthropic":
        return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1]}}
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": tokens[0], "completion_tokens": tokens[1]}}


@pytest.fixture
def stub():
    s = Stub()
    yield s
    s.server.shutdown()


def adapter(stub: Stub, name: str = "openai-compatible", **extra) -> HttpReviewAdapter:
    cfg = {"name": name, "base_url": stub.url, "review_model": "qwen3-coder", **extra}
    a = make_adapter(cfg)
    assert isinstance(a, HttpReviewAdapter)
    a.retry_delay_s = 0.0
    return a


def review_request(cwd: Path, prompt: str = "Review this.\n```diff\n+++ b/x\n```", **kw) -> AgentRequest:
    return AgentRequest("r", "review-aristotle", "aristotle", prompt, cwd, timeout_s=kw.pop("timeout_s", 10),
                        read_only=True, **kw)


# ------------------------------------------------------------------ verdicts
def test_openai_compatible_approving_verdict(stub, tmp_path):
    stub.script = [(200, reply(CLEAN), 0)]
    res = adapter(stub).run_agent(review_request(tmp_path))
    assert res.status == "COMPLETED" and res.structured == CLEAN
    sent = stub.requests[0]
    assert sent["path"] == "/v1/chat/completions" and sent["body"]["model"] == "qwen3-coder"
    assert "authorization" not in sent["headers"]  # local, no key configured
    assert res.usage == {"model": "qwen3-coder", "input_tokens": 1000, "output_tokens": 200}
    assert res.cost == 0 and res.session_id


def test_anthropic_findings_and_key_header(stub, tmp_path, monkeypatch):
    monkeypatch.setenv("MY_KEY", "sekret")
    stub.script = [(200, reply(FOUND, "anthropic", (50, 7)), 0)]
    res = adapter(stub, "anthropic", api_key_env="MY_KEY").run_agent(review_request(tmp_path))
    assert res.status == "COMPLETED" and res.structured["findings"][0]["title"] == "Off by one"
    sent = stub.requests[0]
    assert sent["path"] == "/v1/messages"
    assert sent["headers"]["x-api-key"] == "sekret" and sent["headers"]["anthropic-version"]
    assert sent["body"]["max_tokens"] > 0 and res.usage["input_tokens"] == 50 and res.usage["output_tokens"] == 7


def test_bearer_key_for_openai_style(stub, tmp_path, monkeypatch):
    monkeypatch.setenv("MY_KEY", "sekret")
    adapter(stub, api_key_env="MY_KEY").run_agent(review_request(tmp_path))
    assert stub.requests[0]["headers"]["authorization"] == "Bearer sekret"


def test_reasoning_block_is_ignored(stub, tmp_path):
    stub.script = [(200, reply("<think>{\"summary\": \"draft\"}</think>\n```json\n{\"summary\": \"s\"}\n```"), 0)]
    assert adapter(stub).run_agent(review_request(tmp_path)).structured == {"summary": "s"}


@pytest.mark.parametrize("text", ["I think it is fine.", "```json\n{not json\n```", ""])
def test_malformed_or_empty_output_is_failed_never_approval(stub, tmp_path, text):
    stub.script = [(200, reply(text), 0)]
    res = adapter(stub).run_agent(review_request(tmp_path))
    assert res.status == "FAILED" and res.structured is None and res.error


def test_http_error_and_unreachable_are_failed(stub, tmp_path, monkeypatch):
    monkeypatch.setenv("K", "sekret")
    stub.script = [(401, {"error": "bad key"}, 0)]
    res = adapter(stub, api_key_env="K").run_agent(review_request(tmp_path))
    assert res.status == "FAILED" and "401" in res.error and "sekret" not in res.error
    stub.server.shutdown()
    stub.server.server_close()
    assert adapter(stub).run_agent(review_request(tmp_path)).status == "FAILED"


def test_429_is_retried_once(stub, tmp_path):
    stub.script = [(429, {"error": "slow down"}, 0), (200, reply(CLEAN), 0)]
    res = adapter(stub).run_agent(review_request(tmp_path))
    assert res.status == "COMPLETED" and len(stub.requests) == 2
    stub.requests.clear()
    stub.script = [(429, {}, 0), (429, {}, 0), (200, reply(CLEAN), 0)]
    res = adapter(stub).run_agent(review_request(tmp_path))
    assert res.status == "FAILED" and "429" in res.error and len(stub.requests) == 2


def test_timeout_and_stop(stub, tmp_path):
    stub.script = [(200, reply(CLEAN), 3)]
    t0 = time.monotonic()
    res = adapter(stub).run_agent(review_request(tmp_path, timeout_s=0.5))
    assert res.status == "TIMED_OUT" and time.monotonic() - t0 < 2.5
    stub.script = [(200, reply(CLEAN), 3)]
    res = adapter(stub).run_agent(review_request(tmp_path, should_stop=lambda: True))
    assert res.status == "CANCELLED"


def test_cost_accounting(stub, tmp_path):
    a = adapter(stub, cost_per_mtok={"input": 3, "output": 15})
    assert a.capabilities.cost_reporting and a.expected_cost_basis() == "cash"
    stub.script = [(200, reply(CLEAN, tokens=(1_000_000, 100_000)), 0)]
    res = a.run_agent(review_request(tmp_path))
    assert res.cost == pytest.approx(4.5) and res.cost_basis == "cash"
    free = adapter(stub)
    assert not free.capabilities.cost_reporting and free.expected_cost_basis() is None


# ------------------------------------------------------------------ context
def make_changed_repo(tmp_path: Path) -> Path:
    root = tmp_path / "ctx"
    root.mkdir()
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "src" / "a.py").write_text("A" * 300 + "\n")
    (root / "tests" / "test_a.py").write_text("T" * 300 + "\n")
    (root / "bin.dat").write_bytes(b"\0\1\2")
    return root


def diff_prompt(*files: str, filler: int = 0) -> str:
    diff = "".join(f"--- a/{f}\n+++ b/{f}\n@@ -1 +1 @@\n+changed\n" for f in files) + "+" * filler
    return f"Review.\n\n## Candidate diff\n\n```diff\n{diff}\n```"


def test_context_adds_changed_files_source_before_tests(tmp_path):
    root = make_changed_repo(tmp_path)
    files = ("tests/test_a.py", "src/a.py", "bin.dat", "gone.py")
    out = build_review_prompt(diff_prompt(*files), root, 100_000, files)
    assert out.index("### src/a.py") < out.index("### tests/test_a.py")
    assert "bin.dat\n```" not in out.split("## Full current content")[1] and "gone.py\n```" not in out
    assert "Truncation note" not in out


def test_context_budget_truncates_with_a_note_and_keeps_priority(tmp_path):
    root = make_changed_repo(tmp_path)
    prompt = diff_prompt("src/a.py", "tests/test_a.py")
    out = build_review_prompt(prompt, root, len(prompt) + 500, ("src/a.py", "tests/test_a.py"))  # room for src only
    assert "### src/a.py" in out and "Truncation note" in out and "tests/test_a.py" in out.split("Truncation note")[1]
    assert "A" * 300 in out and "T" * 300 not in out
    out = build_review_prompt(diff_prompt("src/a.py", filler=5000), root, 1500, ("src/a.py",))
    assert "the diff was cut" in out and "### src/a.py" not in out.split("Truncation note")[0]
    assert len(out.split("## Truncation note")[0]) <= 1500 + 200


def test_context_never_leaves_the_repository(tmp_path):
    root = make_changed_repo(tmp_path)
    (tmp_path / "secret.txt").write_text("TOPSECRET")
    out = build_review_prompt(diff_prompt("../secret.txt"), root, 100_000, ("../secret.txt",))
    assert "TOPSECRET" not in out


def test_context_never_follows_paths_named_in_the_prompt(tmp_path):
    # A builder writes the diff; a forged `+++ b/` header must not pull another file into the request.
    root = make_changed_repo(tmp_path)
    (root / ".env").write_text("API_KEY=TOPSECRET")
    out = build_review_prompt(diff_prompt("src/a.py", ".env"), root, 100_000, ("src/a.py",))
    assert "TOPSECRET" not in out and "### src/a.py" in out
    assert "### " not in build_review_prompt(diff_prompt(".env"), root, 100_000)  # no paths given: no content


# ------------------------------------------------------------------ refusal and config
def test_builder_launch_is_refused(stub, tmp_path):
    a = adapter(stub)
    with pytest.raises(CapabilityError, match="reviews only"):
        a.run_agent(AgentRequest("r", "t", "brunel", "build it", tmp_path))
    assert not stub.requests
    caps = a.capabilities
    assert caps.launch_agent and caps.structured_results and caps.identity == "launched"
    assert any("review only" in x for x in caps.limitations)


@pytest.mark.parametrize("cfg, match", [
    ({"base_url": "ftp://x"}, "http://"),
    ({"review_model": ""}, "review_model"),
    ({"api_key_env": "sk-abc123"}, "NAME"),
    ({"cost_per_mtok": {"input": -1, "output": 0}}, "cost_per_mtok"),
    ({"max_context_chars": 5}, "max_context_chars"),
    ({"argv": ["x"]}, "unknown setting"),
])
def test_bad_config_is_refused(cfg, match):
    base = {"name": "openai-compatible", "base_url": "http://localhost:1/v1", "review_model": "m"}
    with pytest.raises(CapabilityError, match=match):
        make_adapter({**base, **cfg})


def test_registered_as_builtin_adapters():
    names = {i.name: i for i in plugins.list_adapters()}
    for n in ("openai-compatible", "anthropic"):
        assert names[n].source == "built-in" and names[n].available and names[n].read_only == "no tools"


def test_evidence_cache_identity_uses_review_model(stub):
    from daedalus.verification.evidence_cache import adapter_identity

    a = adapter(stub)
    assert adapter_identity(a) == f"openai-compatible:qwen3-coder@{a.base_url}"


# ------------------------------------------------------------------ doctor
def doctor_repo(tmp_path: Path, adapter_cfg: dict) -> Path:
    root = make_repo(tmp_path / "d", {"mode": "review", "adapter": adapter_cfg})
    return root


def findings(root: Path) -> dict[str, doctor.Finding]:
    return {f.name: f for f in doctor.diagnose(root)}


def test_doctor_local_adapter_needs_no_key_and_reports_diff_only(tmp_path):
    root = doctor_repo(tmp_path, {"name": "openai-compatible", "base_url": "http://localhost:11434/v1",
                                  "review_model": "qwen3-coder"})
    f = findings(root)
    assert f["launching adapter for reviews"].ok
    assert f["review context"].warn and "diff" in f["review context"].detail


def test_doctor_remote_adapter_needs_its_key(tmp_path, monkeypatch):
    cfg = {"name": "anthropic", "review_model": "claude-sonnet-4-5", "api_key_env": "DAEDALUS_TEST_KEY"}
    root = doctor_repo(tmp_path, cfg)
    monkeypatch.delenv("DAEDALUS_TEST_KEY", raising=False)
    bad = findings(root)["launching adapter for reviews"]
    assert not bad.ok and "DAEDALUS_TEST_KEY" in bad.detail
    monkeypatch.setenv("DAEDALUS_TEST_KEY", "x")
    assert findings(root)["launching adapter for reviews"].ok


def test_doctor_remote_adapter_without_key_name_and_invalid_config(tmp_path):
    root = doctor_repo(tmp_path, {"name": "openai-compatible", "review_model": "gpt-x"})
    assert "api_key_env" in findings(root)["launching adapter for reviews"].detail
    root = make_repo(tmp_path / "bad", {"mode": "review", "adapter": {"name": "anthropic", "base_url": "ftp://x"}})
    bad = findings(root)["launching adapter for reviews"]
    assert not bad.ok and "base_url" in bad.detail
    root = make_repo(tmp_path / "bad2", {"mode": "review", "adapter": {"name": "anthropic", "api_key_env": "K"}})
    assert "review_model" in findings(root)["launching adapter for reviews"].detail


# ------------------------------------------------------------------ end to end through the gate
def test_review_through_launch_review_is_recorded(stub, tmp_path):
    root = make_repo(tmp_path / "e2e", {"mode": "review"})
    a = Ariadne(root)
    try:
        run_id = a.start({"objective": "change lib"}, actor=HUMAN)
        (root / "src" / "lib.py").write_text("x = 2\n", encoding="utf-8")
        (root / "app.txt").write_text("ok\n", encoding="utf-8")
        stub.script = [(200, reply(FOUND), 0)]
        rid = launch_review(a, run_id, "aristotle", adapter(stub))
        assert rid
        state = a.state(run_id)
        assert [r.perspective for r in state.reviews] == ["aristotle"]
        assert [f.title for f in state.open_findings] == ["Off by one"]
        prompt = stub.requests[0]["body"]["messages"][-1]["content"]
        assert "+x = 2" in prompt and "### src/lib.py" in prompt  # the diff, then the changed file in full
        # the files added as context are exactly the candidate's changed paths, handed over by the gate
        assert sorted(x.split("\n", 1)[0] for x in prompt.split("### ")[1:]) == ["app.txt", "src/lib.py"]
        assert state.sessions and state.sessions[-1]["role"] == "aristotle"
        # malformed output: recorded as a failed review, never an approval
        stub.script = [(200, reply("LGTM"), 0)]
        assert launch_review(a, run_id, "mozi", adapter(stub)) is None
        assert a.state(run_id).failed_reviews
    finally:
        a.close()


def test_config_roundtrips_from_yaml(tmp_path):
    text = yaml.safe_load("""
adapter:
  name: openai-compatible
  base_url: http://localhost:11434/v1
  review_model: qwen3-coder
  api_key_env: OPENAI_API_KEY
  cost_per_mtok: {input: 0, output: 0}
  max_context_chars: 200000
""")
    a = make_adapter(text["adapter"])
    assert a.capabilities.cost_reporting and a.expected_cost_basis() is None and a.local
