"""Failure classes. Each maps to one runtime state and none can be turned into a success."""


class Refused(Exception):
    """The request, approval, registry, or queue input is not acceptable. Nothing was executed or claimed."""


class Unavailable(Exception):
    """A pinned prerequisite is missing, so no measurement could be taken. Runtime state: unavailable."""


class MeasurementFailed(Exception):
    """A build or case run failed, timed out, exceeded a bound, or was unstable. Runtime state: failed."""


class ResultLost(Exception):
    """A verifier result could not be written or did not read back identically."""
