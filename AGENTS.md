# NVDA restart workflow

To restart NVDA during local testing:

1. Require an explicit user request or an agreed restart test.
2. Use the bundled `skill` client's external restart workflow. In the current setup, Insert is the NVDA modifier:

   ```powershell
   python skill/scripts/nvda_http_bridge.py restart --nvda-key insert --wait-seconds 30
   ```

3. Treat the command as successful only when it reports `status: restarted` and the returned `afterUptimeMs` is lower than `beforeUptimeMs`.

The command sends the custom `NVDA+Shift+Q` shortcut from the client process and polls NVDA HTTP Bridge health. Do not trigger restart through the bridge's `gesture` action or add an HTTP restart endpoint.
