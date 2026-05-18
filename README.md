# keyhive-proxy

A local privacy proxy for [KeyHive Garden](https://keyhivegarden.com).

Your AI client apps (Cursor, Cline, etc.) point at `keyhive-proxy` instead of AI providers directly.
The proxy fetches your real API keys from KeyHive Garden and calls providers locally — KHG never
sees your request or response content.

## Quick start

```bash
pip install keyhive-proxy
keyhive-proxy start
```

On first launch, enter your KHG API key in the Settings window.
Copy the Base URL from the tray menu and paste it into your AI client.

## CLI

```
keyhive-proxy start            # start with tray icon
keyhive-proxy start --no-tray  # headless (server/Docker)
keyhive-proxy status           # check if running
keyhive-proxy logs             # tail local logs
keyhive-proxy config set --key=<KHG_API_KEY> --port=8080
```

## Privacy

- Request and response body are **never** logged, stored, or sent to KHG servers.
- KHG only receives: token ID, provider name, model name, token counts, latency, outcome.
- Your real API keys are fetched on demand and kept in memory only — never written to disk.

## Downloads

Pre-built installers for Windows, macOS, and Linux are on the
[Releases](../../releases) page.
