# NVDA restart workflow

To restart NVDA during local testing:

1. Require an explicit user request or an agreed restart test.
2. Prefer the bridge's dedicated restart API when it is available. A restart request must be explicitly authorized by the user, must be scheduled so the initiating HTTP response can complete before NVDA shuts down, and must not be implemented by routing a lifecycle gesture through the generic `gesture` action.

   When the dedicated API is unavailable, use the bundled `skill` client's external restart workflow. In the current setup, Caps Lock is the NVDA modifier:

   ```powershell
   python skill/scripts/nvda_http_bridge.py restart --nvda-key capslock --wait-seconds 30
   ```

3. Treat either restart workflow as successful only after the client observes a changed NVDA process ID or process start time. A lower Bridge `uptimeMs` is supporting evidence only; an accepted HTTP response or successful hotkey delivery alone is not proof of restart.

The external workflow sends the custom `NVDA+Shift+Q` shortcut from the client process and polls NVDA HTTP Bridge health. A dedicated HTTP restart endpoint is permitted when it remains loopback-only, enforces Host/browser-origin and secure-context checks, completes its response before shutdown handoff, and verifies a new process identity. The Bridge intentionally has no token authentication, so any local process can call it. Never trigger restart through the bridge's generic `gesture` action.
