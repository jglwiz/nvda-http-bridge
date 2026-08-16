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


class Unauthorized(BridgeError):
	status = 401
	code = "unauthorized"
	default_message = "A valid local session token is required"


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
