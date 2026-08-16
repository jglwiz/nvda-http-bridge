# NVDA HTTP reference

## Client commands

Run `python <skill-directory>/scripts/nvda_http_bridge.py --help` for the complete argument list.

| Command | Purpose | Authentication |
|---|---|---|
| `health`, `version`, `capabilities` | Diagnose and discover the live contract | None |
| `object ROOT`, `object-id ID`, `tree` | Read current objects or a bounded tree | None |
| `speech-history`, `log-tail` | Read sensitive bounded history | None |
| `events` | Collect a duration- and count-bounded SSE event stream | None |
| `export-create/status/download/cancel`, `export-run` | Manage an asynchronous NDJSON tree export | None |
| `speak`, `cancel-speech`, `gesture`, `focus-object`, `default-action` | Perform whitelisted actions | None |
| `restart` | Use the dedicated lifecycle endpoint and verify new process identity; external hotkey only for an old Bridge | None |
| `backup-create/status/cancel`, `backup` | Create and manage a complete backup | None |
| `settings-categories/get/set` | Read or patch the allowlisted General settings | None |
| `speech-dictionaries`, `speech-dictionary-get/validate/put` | Manage NVDA speech dictionaries | None |
| `symbols-get/put` | Manage locale symbol pronunciation overrides | None |
| `gestures-get/patch` | Read current-context commands or change user gesture bindings | None |

The client only connects to `http://127.0.0.1:<port>` and sends no credentials. Any local process can call the Bridge, so never proxy or expose its port.

## Live HTTP contract

- Health: `GET /health`
- Discovery: `GET /v1/version`, `GET /v1/capabilities`
- Objects: `GET /v1/objects/{focus|foreground|navigator|desktop}`
- Object by ID: `GET /v1/objects/by-id/{objectId}`
- Synchronous tree: `GET /v1/tree`
- Exports: `POST /v1/tree/exports`, then `GET`/`DELETE /v1/tree/exports/{jobId}` and `GET .../{jobId}/data`
- Sensitive history: `GET /v1/speech`, `GET /v1/log`
- Actions: `POST /v1/actions/{speak|cancel-speech|gesture|focus|default-action}`
- Backups: `POST /v1/backups`, then `GET`/`DELETE /v1/backups/{jobId}`
- General settings: `GET /v1/settings/categories`, `GET`/`PATCH /v1/settings/general`
- Speech dictionaries: `GET /v1/speech-dictionaries`, `GET`/`PUT /v1/speech-dictionaries/{id}`, `POST .../{id}/validate`
- Symbol pronunciation: `GET`/`PUT /v1/symbol-dictionaries/{locale}`
- Input gestures: `GET`/`PATCH /v1/gestures`
- Lifecycle: `POST /v1/lifecycle/restart` with `{}`

`POST /v1/lifecycle/restart` is process-boundary asynchronous: it requires no credentials and accepts only an empty object, returns `202` completely, closes the request, and only then schedules native `core.restart()`. The CLI sends this POST exactly once and polls `GET /health`; connection refusal, reset, or an incomplete response is completion-unknown and never causes a retry or hotkey fallback. Success requires a changed `nvdaProcessId` or `nvdaStartTime`; lower `uptimeMs` alone can be caused by plugin reload. If live capabilities omit this endpoint, the CLI uses the configured external `NVDA+Shift+Q` compatibility fallback.

`backup --output PATH` sends normalized `PATH` as the required `targetPath`. The plugin creates `<PATH>/nvda`, including missing target parents, calls NVDA's internal portable-copy implementation with current configuration, refuses an existing child, excludes any legacy Bridge credential file, and returns the resulting directory as `backupPath`. Deleting or expiring the HTTP job preserves the completed backup.
- Events: `GET /v1/events`; the client defaults to 5 seconds and at most 50 events. Use `--last-event-id` to resume after a previously returned ID.

Query the live `capabilities` endpoint before assuming limits. Version 1.3.0 defaults are depth 3, 20 children per parent, 200 nodes, and 500 ms. Synchronous hard limits are depth 10, 200 children, 1000 nodes, 3000 ms, and 2 MiB. Exports retain emergency caps of depth 100, 10,000 children, 1,000,000 nodes, 100 MiB per job, 200 MiB total, and 300 seconds.

## Error handling

| Status/code | Meaning | Response |
|---|---|---|
| `403 forbidden` | Bad Host/origin | Keep the loopback base URL |
| `403 secureContext` | Lock screen or secure desktop | Stop sensitive work |
| `409 staleObject` | Object ID/generation expired | Re-read the current object |
| `409 staleState` | A configuration revision or gesture UI context changed | GET the resource again and re-evaluate the intended change |
| `409 unsafeAction` | Lifecycle or dangerous gesture denied | Do not work around it |
| `409 restartBlocked` | NVDA modal state prevents a safe restart | Close the modal UI and make a new explicitly authorized request |
| `409 restartAlreadyScheduled` | This Bridge process already accepted a restart | Do not resend; poll health for a new process identity |
| `422 exportRequired` | Synchronous hard limit exceeded | Use an asynchronous export |
| `429` | Concurrency or rate limit reached | Back off; do not fan out requests |
| `504 mainThreadTimeout` | NVDA main thread missed the deadline | If `completionUnknown=true`, GET the resource to reconcile; never auto-retry the write |

## Export lifecycle

Use `export-run` for normal full workflows. It polls until a terminal state, writes the NDJSON file without overwriting an existing path, and deletes the server-side job by default. `--keep-server-copy` is exceptional; completed files otherwise remain until explicit deletion or TTL.

Passing `null` removes only the user-level limit. Require both explicit `null` arguments and `--allow-unbounded`; server emergency limits, loop detection, security checks, quotas, duration, and cancellation still apply.

## WTS startup freeze

If HTTP-only health responds but focus, tree, and even `cancel-speech` return 504, inspect NVDA's main-thread stack. A stack in `winAPI.sessionTracking` calling `WTSCurrentSessionInfoEx` indicates a Windows session-query freeze rather than tree traversal.

On the validated host, NVDA froze when it started while `TermService` was stopped. Temporarily starting Remote Desktop Services before starting NVDA allowed WTS initialization; the service could then be restored to its prior Stopped/Manual state while NVDA remained usable. Treat this as a host-specific diagnostic. Ask before changing services or restarting NVDA, preserve the original service state, and never change the startup type implicitly.
