# Daedalus documentation

Start with the [README](../README.md) for the pitch and the 60-second demo.

## Using Daedalus

- [concepts.md](concepts.md): the words Daedalus uses (run, tier, floor, evidence, review, ...)
- [troubleshooting.md](troubleshooting.md): the ten most common problems and their fixes
- [gate-workflow.md](gate-workflow.md): set it up once and work under the gate; the commands you touch
- [configuration.md](configuration.md): modes, risk floors, `.daedalus.yml`, budgets and what the gate guarantees
- [reviews.md](reviews.md): review economics: one thorough pass, delta re-reviews, follow-up rounds
- [evidence-cache.md](evidence-cache.md): reuse proven checks and approving reviews on an identical tree
- [ci.md](ci.md): the gate on every pull request, and the GitHub Action
- [report.md](report.md): `daedalus report`, a shareable verdict page for a run (Markdown, HTML, JSON)
- [adapters.md](adapters.md): reviewers on any model: OpenAI-compatible APIs, Ollama, Anthropic
- [factory.md](factory.md): the factory line, work orders and what it guarantees
- [coordination.md](coordination.md): agents coordinating over the shared bus: identity, context, receipts

## How it works

- [architecture.md](architecture.md): layers and responsibilities
- [state-machine.md](state-machine.md): states, transitions, disposition precedence
- [security-model.md](security-model.md): trust boundaries and known limits

## Extending

- [extending.md](extending.md): adding a harness integration or an adapter (entry points)
- [harnesses/](harnesses/claude-code.md): per-harness notes: [Claude Code](harnesses/claude-code.md), [Codex](harnesses/codex.md), [OpenCode](harnesses/opencode.md), [Pi](harnesses/pi.md), [Hermes](harnesses/hermes.md)
- [adapter-contract.md](adapter-contract.md): the harness adapter contract

## Project

- [history/PLAN.md](history/PLAN.md): the original build plan (history, not current behaviour)
- [CONTRIBUTING.md](../CONTRIBUTING.md), [SECURITY.md](../SECURITY.md), [CHANGELOG.md](../CHANGELOG.md)
