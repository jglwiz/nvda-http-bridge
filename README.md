# NVDA HTTP Bridge

English | [简体中文](README.zh-CN.md)

NVDA HTTP Bridge is a loopback-only NVDA global plugin that exposes selected NVDA internals through a controlled, versioned HTTP API.

## Why this project exists

NVDA provides rich internal APIs, but they normally live inside the NVDA process and are available only to add-ons. **NVDA HTTP Bridge turns the parts that are useful for automation into local network APIs for coding tools such as Codex and Claude.**

This gives a coding agent enough structured context to help with:

- investigating NVDA internals, runtime failures, speech history, and logs;
- inspecting focus, accessible objects, text, and accessibility trees;
- reading or changing explicitly supported NVDA settings;
- diagnosing installed add-ons, global plugins, and drivers;
- developing, testing, and validating NVDA add-ons;
- following live focus, state, caret, and speech events during a bounded test.

The Bridge is not a general-purpose remote-control server. It does not expose `eval`, arbitrary Python execution, arbitrary module imports, or general filesystem access. It listens only on `127.0.0.1`, validates every request against a strict schema, enforces resource limits, and rejects data and action requests on the lock screen or secure desktop.

For the mapping between NVDA configuration APIs and their UI counterparts, see [docs/ui-backend-map.md](docs/ui-backend-map.md). Configuration endpoints call NVDA's own configuration objects and persistence paths; they do not simulate the Settings UI.

## Highlights

- **Agent-friendly discovery:** version and capability endpoints describe the live API and its limits.
- **Accessible context:** inspect focus, foreground, navigator, and desktop objects; read bounded text and trees.
- **Troubleshooting:** query runtime status, speech history, the NVDA log tail, add-ons, plugins, drivers, and diagnostics.
- **Configuration support:** work with selected general settings, runtime modes, speech dictionaries, symbol pronunciation, and input gestures.
- **Development workflows:** subscribe to events, export large trees, create bounded diagnostic bundles, and restart NVDA through its native lifecycle API.
- **Safe client:** the bundled standard-library CLI is suitable for Codex skills, Claude tool workflows, scripts, and CI-like local checks.
- **Defensive limits:** main-thread work is sliced and bounded; synchronous responses, exports, text access, and retained jobs all have hard caps.

## Naming

- The project name is **NVDA HTTP Bridge**; the repository directory is `nvda-http-bridge`.
- `NVDA CLI` describes only the bundled client and does not cover the NVDA plugin, HTTP API, or Codex skill, so it is not used as the project name.
- The NVDA add-on ID, entry module, implementation package, and package artifact use `nvdaHttpBridge`.

## Requirements

- Windows with NVDA 2025.3 or later
- PowerShell for the build script
- Python 3 for the bundled CLI and test suite

The current manifest reports NVDA 2026.1 as the latest tested version.

## Repository layout

```text
nvda-addon/  NVDA global plugin sources and manifest
skill/       Codex skill and safe CLI client
tests/       HTTP Bridge unit tests
docs/        Design and UI/backend mapping notes
release/     Local release artifacts (not tracked by Git)
build.ps1    NVDA add-on packaging script
```

## Quick start

### 1. Install for development

Copy these items from `nvda-addon/globalPlugins/` into NVDA's scratchpad `globalPlugins` directory:

```text
nvdaHttpBridge.py
_nvdaHttpBridge/
```

Enable **Developer Scratchpad Directory** in NVDA's Advanced settings, then restart NVDA or reload plugins during development.

For a packaged installation, build the add-on:

```powershell
.\build.ps1
```

The default artifact is written to:

```text
release/nvdaHttpBridge-0.1.0.nvda-addon
```

The build excludes `__pycache__` directories and `.pyc` files.

### 2. Check the running Bridge

Use the bundled client when possible:

```powershell
python skill/scripts/nvda_http_bridge.py health
python skill/scripts/nvda_http_bridge.py capabilities
python skill/scripts/nvda_http_bridge.py status
python skill/scripts/nvda_http_bridge.py object focus --include name,role,className,appName
```

Or call the read-only discovery endpoints directly:

```powershell
curl.exe http://127.0.0.1:19281/health
curl.exe http://127.0.0.1:19281/v1/version
curl.exe http://127.0.0.1:19281/v1/capabilities
curl.exe http://127.0.0.1:19281/v1/status
curl.exe http://127.0.0.1:19281/v1/objects/focus
```

`GET /v1/capabilities` is the authoritative source for the limits, fields, actions, event types, and optional behavior supported by the running version.

## Using the Bridge with coding agents

The `skill/` directory contains a Codex-compatible skill, a dependency-free Python client, and an API reference. A coding agent can use the client as a narrow tool boundary instead of constructing requests or importing NVDA internals itself.

A typical diagnostic workflow is:

```powershell
# Confirm identity and discover the live contract.
python skill/scripts/nvda_http_bridge.py health
python skill/scripts/nvda_http_bridge.py capabilities

# Read the smallest amount of context needed for the task.
python skill/scripts/nvda_http_bridge.py status
python skill/scripts/nvda_http_bridge.py object focus --include name,role,className,appName
python skill/scripts/nvda_http_bridge.py log-tail

# Inspect a bounded accessibility subtree when object context is not enough.
python skill/scripts/nvda_http_bridge.py tree --root focus
```

Codex can use the bundled skill instructions directly. Claude and other programming tools can call the same CLI or local HTTP contract from an approved tool environment. Agents should always discover live capabilities first, request only the context they need, and require explicit user intent before mutations, actions, backups, or restarts.

See [skill/references/api.md](skill/references/api.md) for the client command matrix, error semantics, and export lifecycle.

## API overview

All business endpoints are under `/v1`. No credentials are required.

```text
GET   /health
GET   /v1/version
GET   /v1/capabilities

GET   /v1/status
GET   /v1/modes
PATCH /v1/modes

GET   /v1/objects/{focus|foreground|navigator|desktop}
GET   /v1/objects/by-id/{objectId}
GET   /v1/tree

GET   /v1/text/caret
GET   /v1/text/selection
GET   /v1/text/object/{objectId}

GET   /v1/speech
GET   /v1/log
GET   /v1/events

GET   /v1/addons
GET   /v1/global-plugins
GET   /v1/drivers
GET   /v1/diagnostics

GET   /v1/settings/categories
GET   /v1/settings/general
PATCH /v1/settings/general

GET   /v1/speech-dictionaries
GET   /v1/speech-dictionaries/{default|voice|temp}
POST  /v1/speech-dictionaries/{id}/validate
PUT   /v1/speech-dictionaries/{id}

GET   /v1/symbol-dictionaries/{locale|current}
PUT   /v1/symbol-dictionaries/{locale}

GET   /v1/gestures
PATCH /v1/gestures

POST  /v1/actions/{action}
POST  /v1/lifecycle/restart
```

Tree exports, diagnostic exports, and full backups use asynchronous job endpoints. Query `/v1/capabilities` for their exact paths and current quotas.

## Runtime status and modes

```text
GET   /v1/status
GET   /v1/modes
PATCH /v1/modes
```

`status` summarizes the current profile, application, synthesizer, braille display, and modes. Each mode reports whether it is `available` and `writable`. Currently, only input help, the current application's sleep mode, and the current document's browse mode are writable. Screen curtain is status-only.

Mode changes are session state and are not persisted by this endpoint. A PATCH must include the latest `baseRevision` returned by GET.

## Bounded text access

```text
GET  /v1/text/caret?maxChars=4096
GET  /v1/text/selection?maxChars=4096
GET  /v1/text/object/{objectId}?offset=0&maxChars=4096
POST /v1/actions/set-caret
POST /v1/actions/set-selection
```

Object text is paged in NVDA character units. The default page size is 4,096 characters and the per-request maximum is 32,768. Caret and selection mutations require the `objectId`, `generation`, and `revision` from a fresh read. If focus, document, or text state changes, the server returns `409 staleObject` or `staleState`; reacquire the object instead of retrying blindly.

## Bounded accessibility trees

Start with the safe defaults:

```powershell
python skill/scripts/nvda_http_bridge.py tree --root focus
```

Or request an explicitly bounded, flat result:

```powershell
python skill/scripts/nvda_http_bridge.py tree --root foreground `
  --depth 6 --max-children 100 --max-nodes 800 --timeout-ms 2500 `
  --format flat --include name,role,states,className
```

The response reports its generation, applied limits, node count, elapsed time, truncation state, and truncation reasons. Reasons can include `depthLimit`, `childLimit`, `nodeLimit`, `timeLimit`, `sizeLimit`, and `cycleDetected`.

Version 0.1.0 defaults to depth 3, 20 children per parent, 200 nodes, and a 500 ms soft time budget. Synchronous hard limits are depth 10, 200 children per parent, 1,000 nodes, 3 seconds, and 2 MiB of JSON. Query live capabilities instead of hard-coding these values.

If the requested scope exceeds synchronous limits, the server returns `422 exportRequired`. Use the asynchronous tree export workflow instead of splitting a large traversal into repeated synchronous calls:

```powershell
python skill/scripts/nvda_http_bridge.py export-run `
  --root foreground --depth null --max-children null --max-nodes null `
  --allow-unbounded --output .\tree.ndjson
```

Exports read NVDA objects in main-thread batches and stream NDJSON to a temporary file. Emergency depth, child, node, size, duration, retention, and aggregate-storage caps still apply.

## Settings and dictionaries

Configuration resources use NVDA's own backend objects and save paths:

```powershell
python skill/scripts/nvda_http_bridge.py settings-get
python skill/scripts/nvda_http_bridge.py settings-set --body-file settings-change.json
python skill/scripts/nvda_http_bridge.py speech-dictionary-get default
python skill/scripts/nvda_http_bridge.py symbols-get current
python skill/scripts/nvda_http_bridge.py gestures-get --filter time
```

Mutating requests must include the latest `baseRevision` from GET. Structured mutations use JSON body files. The server does not retry a mutation whose completion becomes uncertain. If a main-thread timeout happens after execution may have started, the response includes `completionUnknown=true`; the CLI reads the resource again and reports the observed state under `reconciliation`.

General settings do not automatically save every NVDA configuration value, and a language change only reports that a restart is required. Clearing a non-empty speech dictionary as a whole and resetting all gestures are intentionally unsupported because their UI equivalents require confirmation.

## Inventory and diagnostics

The following endpoints are read-only:

```text
GET /v1/addons
GET /v1/global-plugins
GET /v1/drivers
GET /v1/diagnostics
```

Diagnostic exports are asynchronous and accept only an empty JSON object. Each ZIP is capped at 5 MiB and contains structured inventories plus at most 2 MiB from the tail of the NVDA log. The caller cannot choose an arbitrary server-side file path. A secure-context change immediately revokes downloads and removes temporary results.

## Events and controlled actions

The SSE stream can report focus, foreground, name, value, state, caret, and speech events:

```powershell
python skill/scripts/nvda_http_bridge.py events --types gainFocus,speech --duration 5
```

It supports event-type filters and `Last-Event-ID` resumption. Plugin reloads and buffer overflows produce a `reset` event.

Explicitly supported actions are:

```text
POST /v1/actions/speak
POST /v1/actions/cancel-speech
POST /v1/actions/gesture
POST /v1/actions/focus
POST /v1/actions/default-action
POST /v1/actions/set-caret
POST /v1/actions/set-selection
```

The gesture action resolves commands in NVDA's current focus context and does not pass an unhandled key through to the foreground application. A syntactically valid but currently unbound gesture returns `409 gestureNotBound`. Read focus and current gestures again before deciding what to do; do not automatically retry an action.

The general action dispatcher rejects restart, quit, plugin reload, and equivalent lifecycle gestures. NVDA restart is available only through the dedicated lifecycle endpoint.

## NVDA restart

Use the synchronous client wrapper:

```powershell
python skill/scripts/nvda_http_bridge.py restart --wait-seconds 30
```

`POST /v1/lifecycle/restart` accepts only `{}`. After completely returning `202 Accepted` and closing the request, it schedules NVDA's native `core.restart()` on the main thread. A `202` response alone does not prove completion. The client reports success only after `/health` shows a changed `nvdaProcessId` or `nvdaStartTime`; a lower Bridge uptime is not enough.

For an older Bridge that does not advertise the lifecycle endpoint, the client can use an external `NVDA+Shift+Q` compatibility fallback. It never falls back after sending the dedicated POST, because a dropped connection at a process boundary has unknown completion state.

## Full portable backup

The backup workflow calls NVDA's own portable-copy implementation and includes the current user configuration:

```powershell
python skill/scripts/nvda_http_bridge.py backup --output D:\backups
```

The target is a parent directory. The Bridge creates a new `nvda` child and refuses to overwrite an existing one. Deleting or expiring the HTTP job does not delete a completed backup. A legacy `nvdaHttpBridge.token` file, if present from an older release, is excluded.

## Security model

- The server listens only on `127.0.0.1:19281` and rejects non-loopback `Host` values.
- There is intentionally no token or client authentication. Any local process can call the API.
- Never proxy, port-forward, or expose the Bridge to another machine or container network.
- Cross-site browser `Origin` and `Sec-Fetch-Site` requests are rejected before entering the NVDA main thread.
- Data and action requests are rejected on the lock screen and secure desktop. Sensitive caches and in-flight tree, diagnostic, and backup jobs are cleared or cancelled.
- Tree traversal, text reads, response sizes, retained jobs, and main-thread work are bounded.
- Individual UIA or IA2 property calls cannot be safely interrupted; budgets are checked before calls and between batches, so time limits are not hard real-time guarantees.
- Object IDs, generations, revisions, and text offsets are short-lived. Mutations require fresh state.
- The API does not provide arbitrary Python execution, imports, or general file access.

Because `auth.mode=none`, untrusted local processes are outside the threat model. The protection boundary is the loopback listener, Host and browser-origin checks, secure-context enforcement, strict schemas, and resource limits. Legacy `%APPDATA%\nvda\nvdaHttpBridge.token` files are neither read nor created.

## Development and verification

Run the unit tests and compile the plugin sources:

```powershell
python -m unittest discover -s tests -v
python -m compileall nvda-addon/globalPlugins
```

After activating changed runtime sources, verify at minimum that `/health`, `/v1/version`, and `/v1/capabilities` return HTTP 200 and that the running version matches the repository. Confirm that capabilities report `auth.mode=none`, then use bounded, non-mutating checks for the features involved in the change. Inspect the NVDA log for related import, initialization, and runtime errors.

Real-machine coverage should include supported NVDA versions and representative Win32, UIA, and Chromium/IA2 applications, as well as lock screen, secure desktop, repeated plugin reload, cancellation, backup, and stale-object cases.

## Troubleshooting main-thread timeouts

If `/health` responds but object, tree, and cancel-speech requests all return `504 mainThreadTimeout`, inspect the frozen main-thread stack in the NVDA log before increasing any timeout.

If the stack is blocked in `winAPI.sessionTracking` at `WTSCurrentSessionInfoEx`, the problem is in NVDA/Windows session-state initialization rather than tree traversal or HTTP JSON encoding. On one development machine, starting NVDA while Remote Desktop Services (`TermService`) was stopped triggered this freeze. Temporarily starting the service before NVDA allowed initialization to complete; the service could then be restored to its previous state. This is a machine-specific diagnostic finding, not a Bridge dependency. Changing Windows services requires administrator privileges and should not be automated without explicit approval.

## Project status

NVDA HTTP Bridge is currently focused on local development, diagnostics, and agent-assisted accessibility workflows. Its API is versioned, but consumers should still use live capability discovery because fields, optional endpoints, and safety limits may evolve.
