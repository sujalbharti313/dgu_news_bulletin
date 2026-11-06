"""Errors of the intelligence engine. `safe_message` never contains credentials or source text."""


class IntelligenceConfigError(RuntimeError):
    def __init__(self, safe_message: str):
        super().__init__(safe_message)
        self.safe_message = safe_message
