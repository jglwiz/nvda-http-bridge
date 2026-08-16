---
name: nvda
description: Inspect and control a locally running NVDA screen reader through NVDA HTTP Bridge, restart NVDA through its dedicated lifecycle API with a legacy external-hotkey fallback, and create a complete portable backup with current configuration. Use when the user asks about current NVDA focus, foreground or navigator objects, accessibility trees, NVDA events, speech or log history, bounded or full tree exports, speaking or canceling speech, focusing an object, safe gestures or default actions, restarting or backing up NVDA, HTTP bridge health, or an NVDA 504 mainThreadTimeout whose stack is stopped in WTSCurrentSessionInfoEx. Do not use for generic NVDA source-code work that does not require a running local NVDA instance.
---

# NVDA

Use the bundled client instead of assembling HTTP requests by hand. Resolve this skill's directory from the skill path, then run:

```powershell
python <skill-directory>/scripts/nvda_http_bridge.py health
```

## Workflow

1. Run `health`. If the bridge is absent, report that NVDA HTTP Bridge is not running; do not start or install software unless the user asked.
2. Run `capabilities` before relying on limits or optional behavior. Treat the live response as authoritative.
3. Choose the smallest operation that answers the request:
   - Current object: `object focus`, `object foreground`, or `object navigator`.
   - Accessibility subtree: `tree` with defaults first; add explicit bounds only when needed.
   - Large or user-unbounded result: `export-run --output <path>` rather than expanding synchronous limits.
   - Sensitive history: `speech-history` or `log-tail`.
   - Live changes: bounded `events --types <types> --duration <seconds>`.
   - Restart: `restart` only after an explicit request, then report before/after process identity and uptime.
   - Complete backup: `backup` only after an explicit request. Treat `--output` as the target folder; let NVDA HTTP Bridge create its new `nvda` child (default `./nvda`), poll completion, then delete only the HTTP job.
   - Mutation: an action command only after confirming the user's intent.
4. Summarize metadata such as node count, elapsed time, truncation reasons, and error codes. Do not dump large trees or sensitive text unless requested.

Read [references/api.md](references/api.md) when choosing parameters, interpreting errors, managing exports, or diagnosing startup freezes.

## Safety rules

- Keep the base URL on `http://127.0.0.1`; the client rejects other hosts. The Bridge has no token authentication, so any local process can call it; never proxy or expose the port.
- Keep ordinary tree queries bounded. Start with the server defaults and never work around `422 exportRequired` with repeated synchronous calls.
- Use `null` export limits only when the user explicitly requests an unrestricted dimension. Pass `--allow-unbounded`; cancel or delete the server job when finished.
- Treat screen text, speech history, logs, and exported trees as sensitive. Store downloads only at a user-approved path.
- Run `speak`, `cancel-speech`, `gesture`, `focus-object`, `default-action`, and `restart` only for an explicit user request or an agreed test. Never attempt NVDA quit, plugin reload, or arbitrary Python execution.
- Restart through the client. It uses the dedicated lifecycle endpoint when live capabilities declare it, sends the POST only once, and uses external `NVDA+Shift+Q` only for an older Bridge without that capability. Never route a lifecycle command through the generic `gesture` action. Treat dropped or incomplete responses during shutdown as temporary unavailability and keep polling. Success requires a changed NVDA PID or start time; lower Bridge uptime is not sufficient.
- Create backups only through the asynchronous NVDA HTTP Bridge backup API by using the client's `backup` workflow. Send the chosen target folder as `targetPath`; normalize it and create a new `nvda` child, including missing target parents. Refuse an existing child, exclude any legacy credential file left by older Bridge versions, and preserve the completed backup when deleting or expiring its HTTP job.
- Reacquire an object after UI changes or a `409 staleObject`; object IDs are short-lived and generation-scoped.
- Stop on `403 secureContext`. Do not bypass lock-screen or secure-desktop checks.
- On `504 mainThreadTimeout`, inspect `health`. Do not increase timeouts or start a large tree. Follow the WTS diagnostic in the reference; changing Windows service state or restarting NVDA requires user approval.
- Do not automatically retry mutating requests after a transport timeout because their completion may be uncertain.

## Common commands

```powershell
# Inspect focus without expensive fields.
python <skill-directory>/scripts/nvda_http_bridge.py object focus --include name,role,className,appName

# Use the safe server defaults: depth 3, 20 children, 200 nodes, 500 ms.
python <skill-directory>/scripts/nvda_http_bridge.py tree --root focus

# Request a larger but still bounded flat tree.
python <skill-directory>/scripts/nvda_http_bridge.py tree --root foreground --depth 6 --max-children 100 --max-nodes 800 --timeout-ms 2500 --format flat --include name,role,states,className

# Explicitly unbind user-level dimensions; emergency caps still apply.
python <skill-directory>/scripts/nvda_http_bridge.py export-run --root desktop --depth null --max-children null --max-nodes null --allow-unbounded --output <approved-path>

# Explicitly requested restart; dedicated HTTP endpoint preferred, Caps Lock fallback for an old Bridge.
python <skill-directory>/scripts/nvda_http_bridge.py restart --nvda-key capslock --wait-seconds 30

# Complete portable backup plus current configuration; defaults to ./nvda.
python <skill-directory>/scripts/nvda_http_bridge.py backup
```

The client emits one JSON result and exits nonzero for HTTP or client errors. Check `httpStatus`, `data.error.code`, and the process exit code before continuing.
