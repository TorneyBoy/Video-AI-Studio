"""Shared authoring errors; safe to return through the private API handler."""


class EditingError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 409, details: dict | None = None):
        super().__init__(message)
        self.code, self.status_code, self.details = code, status_code, details or {}
