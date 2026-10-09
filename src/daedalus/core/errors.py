"""Error types. Every refusal by the control plane raises one of these."""


class DaedalusError(Exception):
    """Base class for all control-plane refusals."""


class IllegalTransition(DaedalusError):
    """A state transition not present in the legal transition table."""


class ContractError(DaedalusError):
    """A task contract failed validation or an amendment was not permitted."""


class PolicyError(DaedalusError):
    """A policy failed validation, or the loaded policy does not match the run."""


class AuthorizationError(DaedalusError):
    """An actor attempted something it has no authority to do."""


class CapabilityError(DaedalusError):
    """An adapter lacks a capability the run requires (fail closed)."""


class BudgetExhausted(DaedalusError):
    """A budget limit would be exceeded by the requested operation."""


class IntegrationError(DaedalusError):
    """A worker proposal cannot be integrated into the authoritative candidate."""


class AuditIntegrityError(DaedalusError):
    """The append-only audit log failed hash-chain verification."""
