# Reviewers on any model (no agent CLI)

Daedalus can run its independent reviews over HTTP, so you do not need `claude` or any other
agent CLI installed. Two built-in adapters:

| `adapter.name` | Speaks | Use it for |
| --- | --- | --- |
| `openai-compatible` | `POST {base_url}/chat/completions` | OpenAI, OpenRouter, Together, Groq, vLLM, LM Studio, Ollama |
| `anthropic` | `POST {base_url}/messages` | The Anthropic Messages API |

They review only. A builder launch is refused with a clear error; keep your agent (or a CLI
adapter) for building. They use the standard library only, honour `HTTPS_PROXY` and the system CA
bundle, never follow redirects (so a key cannot be sent to another host), and put no key in any
log or error.

## Local setup with Ollama

```sh
ollama pull qwen3-coder
# .daedalus.yml
adapter: {name: openai-compatible, base_url: "http://localhost:11434/v1", review_model: qwen3-coder}
```

Then `mode: review` and `daedalus doctor`. No key is needed for localhost.

## Hosted setup

The key is never written to `.daedalus.yml`. `api_key_env` names the environment variable that
holds it.

```yaml
# OpenAI
adapter: {name: openai-compatible, base_url: "https://api.openai.com/v1",
          review_model: gpt-5, api_key_env: OPENAI_API_KEY}

# OpenRouter (any model it serves)
adapter: {name: openai-compatible, base_url: "https://openrouter.ai/api/v1",
          review_model: "anthropic/claude-sonnet-4.5", api_key_env: OPENROUTER_API_KEY}

# Anthropic
adapter: {name: anthropic, review_model: claude-sonnet-4-5, api_key_env: ANTHROPIC_API_KEY}
```

## All settings

```yaml
adapter:
  name: openai-compatible          # or: anthropic
  base_url: http://localhost:11434/v1   # default: the vendor's public URL
  review_model: qwen3-coder        # required; also keys the evidence cache
  api_key_env: OPENAI_API_KEY      # env var NAME, never the key; optional for localhost
  cost_per_mtok: {input: 0, output: 0}  # dollars per million tokens; optional
  max_context_chars: 200000        # bound on the prompt plus file context
  max_tokens: 8192                 # output cap (the Messages API requires one)
```

With non-zero `cost_per_mtok`, each review is charged as cash, so the run needs a `max_cash`
budget (`daedalus doctor` checks this). A local endpoint (`localhost`, a loopback address) with no
prices is recorded as a free API-equivalent estimate. A hosted endpoint with no prices is treated as
cash with an unknown cost: the cash budget applies, a review is refused while `max_cash` is 0, and one
that does run leaves the run "cash unknown" so it cannot be accepted. Set `cost_per_mtok` for any
billed key; `daedalus doctor` warns. Token counts are always recorded. `base_url` must not contain
credentials (`user:pass@`), a query string or a fragment; the key belongs in `api_key_env`. A 429 is retried once after `Retry-After` (at most 10 seconds). A timeout, an HTTP
error, an empty reply or output with no valid JSON block is a failed review, never an approval.

## What the reviewer sees, and what it cannot

An HTTP model has no tools, so it cannot read your repository. Daedalus gives it:

1. the review prompt with the candidate diff against the base revision;
2. the full current content of each changed file, source before tests;
3. a truncation note if `max_context_chars` cut anything. The diff has priority, then changed
   source, then changed tests. Symlinks among the changed paths are never followed. If the review
instructions alone leave less than about 500 characters for the diff, the review fails with a
"max_context_chars is too small" error instead of sending an empty diff.

It judges from that and nothing else: it cannot see callers, other modules, or run anything. The
trusted checks still run as usual; the reviewer only reads their results. `daedalus doctor` reports
this limit.

## Trust and limits

- Small local models are weaker reviewers. They miss subtle bugs and sometimes fail to produce the
  JSON block; those reviews fail closed and are retried, not approved.
- Use a local model for the medium tier, where a second pair of eyes on the diff is the goal. For
  the high tier (auth, migrations, test integrity) use a stronger model, or an agent adapter whose
  reviewer can open files.
- Your code is sent to the API you configure. For a hosted API, check your provider's terms.
