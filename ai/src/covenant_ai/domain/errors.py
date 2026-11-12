class ValuationError(ValueError):
    """Base cho lỗi nghiệp vụ của valuation."""

class InsufficientPeersError(ValuationError): ...
class PeriodMismatchError(ValuationError): ...

class MissingFieldError(ValueError):
    def __init__(self, ticker: str, missing: list[str]):
        super().__init__(f"{ticker}: missing {missing}")
        self.ticker = ticker
        self.missing = missing