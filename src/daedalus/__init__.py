"""Daedalus — a harness-agnostic engineering control plane.

Agents propose; the control plane enforces. The core owns contracts, lifecycle
state, risk policy, evidence validation, authorization and the derived
acceptance decision. Harness adapters supply agent execution only.
"""

# The single source of truth for the version: pyproject.toml reads it (`[tool.setuptools.dynamic]`).
__version__ = "0.2.0"
