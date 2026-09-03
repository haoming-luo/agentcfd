class AgentCFDError(Exception):
    """Base error for a scientifically meaningful AgentCFD failure."""


class ModelValidationError(AgentCFDError):
    """The model is incomplete or internally inconsistent."""


class UnsupportedCaseError(AgentCFDError):
    """The requested physics lies outside a provider's declared capability."""


class ProviderUnavailableError(AgentCFDError):
    """A selected numerical provider is not installed or cannot be executed."""
