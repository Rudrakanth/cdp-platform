"""Domain errors. The API maps these to HTTP status codes."""


class CDPError(Exception):
    status = 400


class ValidationError(CDPError):
    status = 422


class NotFound(CDPError):
    status = 404


class Conflict(CDPError):
    status = 409
