"""Bounded, iterative accessibility-tree traversal."""

from dataclasses import dataclass, field
import time

from .config import (
	ALLOWED_FIELDS,
	DEFAULT_FIELDS,
	DEFAULT_TREE_CHILDREN,
	DEFAULT_TREE_DEPTH,
	DEFAULT_TREE_NODES,
	DEFAULT_TREE_TIMEOUT_MS,
	ROOT_NAMES,
	SYNC_MAX_CHILDREN,
	SYNC_MAX_DEPTH,
	SYNC_MAX_NODES,
	SYNC_MAX_TIMEOUT_MS,
	TREE_FORMATS,
)
from .errors import ExportRequired, ValidationError
from .serialization import error_info, serialize_object


@dataclass(frozen=True)
class TreeOptions:
	root: str = "focus"
	depth: int | None = DEFAULT_TREE_DEPTH
	max_children: int | None = DEFAULT_TREE_CHILDREN
	max_nodes: int | None = DEFAULT_TREE_NODES
	timeout_ms: int = DEFAULT_TREE_TIMEOUT_MS
	include: tuple = DEFAULT_FIELDS
	format: str = "nested"


def _single(params, name, default=None):
	values = params.get(name)
	if values is None:
		return default
	if not isinstance(values, (list, tuple)):
		return values
	if len(values) != 1:
		raise ValidationError("Parameter '%s' must be supplied once" % name)
	return values[0]


def _integer(value, name, minimum, allow_none=False):
	if value is None and allow_none:
		return None
	try:
		result = int(value)
	except (TypeError, ValueError):
		raise ValidationError("Parameter '%s' must be an integer" % name)
	if result < minimum:
		raise ValidationError("Parameter '%s' must be at least %s" % (name, minimum))
	return result


def _fields(value):
	if value is None:
		return DEFAULT_FIELDS
	if isinstance(value, (list, tuple)):
		values = value
	else:
		values = str(value).split(",")
	result = tuple(dict.fromkeys(item.strip() for item in values if item and item.strip()))
	unknown = sorted(set(result) - set(ALLOWED_FIELDS))
	if unknown:
		raise ValidationError("Unknown object fields", details={"fields": unknown})
	if not result:
		raise ValidationError("At least one object field must be requested")
	return result


def parse_sync_options(params):
	allowed = {"root", "depth", "maxChildren", "maxNodes", "timeoutMs", "include", "format"}
	unknown = sorted(set(params) - allowed)
	if unknown:
		raise ValidationError("Unknown query parameters", details={"parameters": unknown})
	root = _single(params, "root", "focus")
	if root not in ROOT_NAMES:
		raise ValidationError("Unknown root object", details={"root": root})
	options = TreeOptions(
		root=root,
		depth=_integer(_single(params, "depth", DEFAULT_TREE_DEPTH), "depth", 0),
		max_children=_integer(_single(params, "maxChildren", DEFAULT_TREE_CHILDREN), "maxChildren", 0),
		max_nodes=_integer(_single(params, "maxNodes", DEFAULT_TREE_NODES), "maxNodes", 1),
		timeout_ms=_integer(_single(params, "timeoutMs", DEFAULT_TREE_TIMEOUT_MS), "timeoutMs", 1),
		include=_fields(_single(params, "include", None)),
		format=_single(params, "format", "nested"),
	)
	if options.format not in TREE_FORMATS:
		raise ValidationError("Unknown tree format", details={"format": options.format})
	exceeded = {}
	for name, value, limit in (
		("depth", options.depth, SYNC_MAX_DEPTH),
		("maxChildren", options.max_children, SYNC_MAX_CHILDREN),
		("maxNodes", options.max_nodes, SYNC_MAX_NODES),
		("timeoutMs", options.timeout_ms, SYNC_MAX_TIMEOUT_MS),
	):
		if value > limit:
			exceeded[name] = {"requested": value, "maximum": limit}
	if exceeded:
		raise ExportRequired(details={"limits": exceeded, "endpoint": "/v1/tree/exports"})
	return options


def parse_export_options(data):
	if not isinstance(data, dict):
		raise ValidationError("The export body must be a JSON object")
	allowed = {"root", "depth", "maxChildren", "maxNodes", "include", "format"}
	unknown = sorted(set(data) - allowed)
	if unknown:
		raise ValidationError("Unknown export parameters", details={"parameters": unknown})
	root = data.get("root", "focus")
	if root not in ROOT_NAMES:
		raise ValidationError("Unknown root object", details={"root": root})
	format_name = data.get("format", "flat")
	if format_name not in TREE_FORMATS:
		raise ValidationError("Unknown tree format", details={"format": format_name})
	return TreeOptions(
		root=root,
		depth=_integer(data.get("depth", None), "depth", 0, allow_none=True),
		max_children=_integer(data.get("maxChildren", None), "maxChildren", 0, allow_none=True),
		max_nodes=_integer(data.get("maxNodes", None), "maxNodes", 1, allow_none=True),
		timeout_ms=DEFAULT_TREE_TIMEOUT_MS,
		include=_fields(data.get("include")),
		format=format_name,
	)


@dataclass
class _Frame:
	obj: object
	parent_id: str | None
	depth: int
	index: int
	phase: str = "node"
	node_id: str | None = None
	child: object | None = None
	child_index: int = 0
	child_seen: set = field(default_factory=set)
	pre_errors: dict = field(default_factory=dict)


class TreeWalker:
	def __init__(self, root, options, registry, generation, adapter=None, monotonic=None):
		self.options = options
		self.registry = registry
		self.generation = generation
		self.adapter = adapter or registry.adapter
		self._monotonic = monotonic or time.monotonic
		self._stack = [_Frame(root, None, 0, 0)] if root is not None else []
		self._seen = set()
		self.node_count = 0
		self.reasons = []
		self.done = root is None

	def _reason(self, reason):
		if reason not in self.reasons:
			self.reasons.append(reason)

	def _identity(self, obj):
		try:
			return self.adapter.identity(obj)
		except Exception:
			return id(obj)

	def _step(self):
		if not self._stack:
			self.done = True
			return None
		frame = self._stack[-1]
		if frame.phase == "node":
			if self.options.max_nodes is not None and self.node_count >= self.options.max_nodes:
				self._reason("nodeLimit")
				self._stack.clear()
				self.done = True
				return None
			identity = self._identity(frame.obj)
			if identity in self._seen:
				self._reason("cycleDetected")
				self._stack.pop()
				return None
			self._seen.add(identity)
			data = serialize_object(
				frame.obj,
				self.options.include,
				self.registry,
				self.generation,
				self.adapter,
			)
			frame.node_id = data["objectId"]
			frame.phase = "children"
			try:
				first_child = self.adapter.first_child(frame.obj)
			except Exception as error:
				first_child = None
				frame.pre_errors["firstChild"] = error_info(error)
			if self.options.depth is not None and frame.depth >= self.options.depth:
				if first_child is not None:
					self._reason("depthLimit")
				first_child = None
			frame.child = first_child
			if frame.pre_errors:
				data.setdefault("errors", {}).update(frame.pre_errors)
			record = {
				"id": frame.node_id,
				"parentId": frame.parent_id,
				"depth": frame.depth,
				"index": frame.index,
				"object": data,
			}
			self.node_count += 1
			return record

		if frame.child is None:
			self._stack.pop()
			return None
		if self.options.max_children is not None and frame.child_index >= self.options.max_children:
			self._reason("childLimit")
			self._stack.pop()
			return None
		child = frame.child
		child_identity = self._identity(child)
		if child_identity in frame.child_seen:
			self._reason("cycleDetected")
			self._stack.pop()
			return None
		frame.child_seen.add(child_identity)
		next_error = None
		try:
			frame.child = self.adapter.next_sibling(child)
		except Exception as error:
			frame.child = None
			next_error = error_info(error)
		child_index = frame.child_index
		frame.child_index += 1
		pre_errors = {"nextSibling": next_error} if next_error else {}
		self._stack.append(_Frame(child, frame.node_id, frame.depth + 1, child_index, pre_errors=pre_errors))
		return None

	def next_batch(self, max_records, budget_ms):
		start = self._monotonic()
		deadline = start + max(1, budget_ms) / 1000.0
		records = []
		while not self.done and len(records) < max_records:
			if self._monotonic() >= deadline:
				break
			record = self._step()
			if record is not None:
				records.append(record)
		return records

	def abort(self, reason):
		self._reason(reason)
		self._stack.clear()
		self.done = True

	def frontier_size(self):
		return len(self._stack)


def _nested(records):
	by_id = {}
	roots = []
	for record in records:
		node = dict(record["object"])
		node["depth"] = record["depth"]
		node["index"] = record["index"]
		node["children"] = []
		by_id[record["id"]] = node
		parent = by_id.get(record["parentId"])
		if parent is None:
			roots.append(node)
		else:
			parent["children"].append(node)
	return roots[0] if len(roots) == 1 else {"children": roots}


def tree_result(records, walker, options, generation, started, monotonic=None):
	clock = monotonic or time.monotonic
	elapsed_ms = round((clock() - started) * 1000, 2)
	result = _nested(records) if options.format == "nested" else records
	return {
		"generation": generation,
		"root": options.root,
		"limits": {
			"depth": options.depth,
			"maxChildren": options.max_children,
			"maxNodes": options.max_nodes,
			"timeoutMs": options.timeout_ms,
		},
		"nodeCount": len(records),
		"elapsedMs": elapsed_ms,
		"truncated": bool(walker.reasons),
		"truncationReasons": walker.reasons,
		"format": options.format,
		"tree": result,
	}


def collect_tree(root, options, registry, generation, adapter=None, monotonic=None):
	clock = monotonic or time.monotonic
	started = clock()
	walker = TreeWalker(root, options, registry, generation, adapter, clock)
	records = []
	deadline = started + options.timeout_ms / 1000.0
	while not walker.done:
		remaining_ms = int(max(0.0, deadline - clock()) * 1000)
		if remaining_ms <= 0:
			walker.abort("timeLimit")
			break
		remaining_nodes = options.max_nodes - len(records)
		batch = walker.next_batch(max(1, remaining_nodes), remaining_ms)
		records.extend(batch)
		if not batch and not walker.done and clock() >= deadline:
			walker.abort("timeLimit")
			break
	return tree_result(records, walker, options, generation, started, clock)
