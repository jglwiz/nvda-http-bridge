"""Small shared helpers for versioned configuration resources."""

import hashlib
import json

from .errors import BadRequest, StaleState, ValidationError


def revision(value):
	payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
	return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def require_object(body, name="request body"):
	if not isinstance(body, dict):
		raise BadRequest("The %s must be a JSON object" % name)
	return body


def reject_unknown(mapping, allowed, name="fields"):
	unknown = sorted(set(mapping) - set(allowed))
	if unknown:
		raise ValidationError("Unknown %s" % name, details={"unknown": unknown})


def require_revision(body, current):
	base = body.get("baseRevision")
	if not isinstance(base, str) or not base:
		raise ValidationError("baseRevision must be a non-empty string")
	if base != current:
		raise StaleState(details={"expectedRevision": current, "providedRevision": base})


def mutation_result(resource, changed, current, persisted, restart_required=False, warnings=None):
	return {
		"status": "ok",
		"resource": resource,
		"changed": bool(changed),
		"revision": current["revision"],
		"effective": True,
		"persisted": bool(persisted),
		"restartRequired": bool(restart_required),
		"warnings": list(warnings or ()),
		"state": current,
	}
