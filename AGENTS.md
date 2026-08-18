# NVDA local testing

Scratchpad deployment and every NVDA restart required to activate or validate in-scope local Bridge changes are pre-authorized. Do not request separate confirmation. This authorization does not cover unrelated operating-system changes.

## Restart

1. Use the bundled client, which prefers the dedicated lifecycle API and falls back to the external `NVDA+Shift+Q` workflow. Caps Lock is the NVDA modifier:

   ```powershell
   python skill/scripts/nvda_http_bridge.py restart --nvda-key capslock --wait-seconds 30
   ```

2. The dedicated endpoint must remain loopback-only, enforce Host/browser-origin and secure-context checks, complete its HTTP response before shutdown, and call NVDA's lifecycle backend. The generic `gesture` action may send NVDA's native restart gesture; this permission does not cover arbitrary gestures.
3. Treat restart as successful only after observing a changed `nvdaProcessId` or `nvdaStartTime`. An accepted response, successful hotkey delivery, or lower Bridge uptime is not sufficient.
4. After activating changed Bridge sources, run the repository's relevant non-mutating post-restart checks and inspect the NVDA log for related import, initialization, and runtime errors.

The Bridge intentionally uses `auth.mode=none`; browser and remote-network restrictions remain required, while untrusted local processes are outside this threat model.
