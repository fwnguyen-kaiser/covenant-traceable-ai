class ValuationError(ValueError):
    """Base class for domain-level valuation errors."""

class InsufficientPeersError(ValuationError): ...
class PeriodMismatchError(ValuationError): ...
class NonPositiveTargetEbitdaError(ValuationError): ...

class MissingFieldError(ValueError):
    def __init__(self, ticker: str, missing: list[str]):
        super().__init__(f"{ticker}: missing {missing}")
        self.ticker = ticker
        self.missing = missing
