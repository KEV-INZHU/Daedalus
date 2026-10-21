# Daedalus documentation

Start with the [README](../README.md) for the pitch and the 60-second demo.

## Using Daedalus

- [gate-workflow.md](gate-workflow.md): set it up once and work under the gate; the commands you touch
- [configuration.md](configuration.md): modes, risk floors, `.daedalus.yml`, budgets and what the gate guarantees
- [reviews.md](reviews.md): review economics: one thorough pass, delta re-reviews, follow-up rounds
- [evidence-cache.md](evidence-cache.md): reuse proven checks and approving reviews on an identical tree
- [ci.md](ci.md): the gate on every pull request, and the GitHub Action
- [factory.md](factory.md): the factory line, work orders and what it guarantees

## How it works

- [architecture.md](architecture.md): layers and responsibilities
- [state-machine.md](state-machine.md): states, transitions, disposition precedence
- [security-model.md](security-model.md): trust boundaries and known limits

## Extending

- [extending.md](extending.md): adding a harness integration or an adapter (entry points)
- [harnesses/](harnesses/codex.md): per-harness notes: [Codex](harnesses/codex.md), [OpenCode](harnesses/opencode.md), [Pi](harnesses/pi.md), [Hermes](harnesses/hermes.md)
- [adapter-contract.md](adapter-contract.md): the harness adapter contract

## Project

- [PLAN.md](PLAN.md): build plan and progress
- [CONTRIBUTING.md](../CONTRIBUTING.md), [SECURITY.md](../SECURITY.md), [CHANGELOG.md](../CHANGELOG.md)
