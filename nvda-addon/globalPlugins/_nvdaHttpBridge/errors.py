"""Structured errors shared by the service and HTTP layers."""


class BridgeError(Exception):
	status = 500
	code = "internalError"
	default_message = "The request could not be completed"

	def __init__(self, message=None, details=None):
		super().__init__(message or self.default_message)
		self.message = message or self.default_message
		self.details = details

	def as_dict(self):
		error = {"code": self.code, "message": self.message}
		if self.details:
			error["details"] = self.details
		return {"error": error}


class BadRequest(BridgeError):
	status = 400
	code = "badRequest"
	default_message = "The request is invalid"


class Forbidden(BridgeError):
	status = 403
	code = "forbidden"
	default_message = "The operation is not allowed in the current session"


class NotFound(BridgeError):
	status = 404
	code = "notFound"
	default_message = "The requested resource was not found"


class StaleObject(BridgeError):
	status = 409
	code = "staleObject"
	default_message = "The NVDA object is no longer available"


class Conflict(BridgeError):
	status = 409
	code = "conflict"
	default_message = "The request conflicts with the current bridge state"


class StaleState(Conflict):
	code = "staleState"
	default_message = "The resource changed since it was read"


class TextPositionUnavailable(Conflict):
	code = "textPositionUnavailable"
	default_message = "The current NVDA object does not expose the requested text position"


class RestartBlocked(Conflict):
	code = "restartBlocked"
	default_message = "NVDA cannot be safely restarted in the current UI state"


class RestartAlreadyScheduled(Conflict):
	code = "restartAlreadyScheduled"
	default_message = "An NVDA restart is already scheduled"


class PartialFailure(BridgeError):
	status = 500
	code = "partialFailure"
	default_message = "The operation failed and persistence could not be proven atomic"


class ValidationError(BridgeError):
	status = 422
	code = "validationError"
	default_message = "One or more parameters are invalid"


class ExportRequired(ValidationError):
	code = "exportRequired"
	default_message = "The requested limits require the asynchronous export API"


class TooManyRequests(BridgeError):
	status = 429
	code = "tooManyRequests"
	default_message = "The bridge is at its concurrency limit"


class MainThreadTimeout(BridgeError):
	status = 504
	code = "mainThreadTimeout"
	default_message = "NVDA did not complete the operation before its deadline"


class ServiceUnavailable(BridgeError):
	status = 503
	code = "serviceUnavailable"
	default_message = "The bridge is shutting down or temporarily unavailable"


class SecureContext(Forbidden):
	code = "secureContext"
	default_message = "Data and actions are unavailable while Windows is locked or on a secure desktop"


class UnsafeAction(Conflict):
	code = "unsafeAction"
	default_message = "This action is intentionally unavailable over HTTP"


class GestureNotBound(Conflict):
	code = "gestureNotBound"
	default_message = "The gesture is not bound in the current NVDA context"
