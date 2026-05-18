# keyhive-proxy

Local privacy proxy for AI APIs. Your LLM clients (Cursor, Cline, Continue, etc.) connect to `http://localhost:8080` instead of AI providers directly. The proxy fetches your real provider keys from KeyHive Garden and calls providers locally — **KeyHive Garden never sees your request or response content.**

## How it works

```
Your app → localhost:8080 → keyhive-proxy → OpenAI / Anthropic / Groq / ...
                                    ↑
                        fetches your keys from KeyHive Garden
                        (keys are decrypted locally, never sent to KHG)
```

1. You add your provider API keys to your KeyHive Garden account
2. KeyHive Garden verifies the keys and builds a prioritized pool
3. keyhive-proxy fetches encrypted key bundles and decrypts them locally
4. All LLM requests are made from your machine — KHG sees only metadata (provider, model, latency, outcome), never prompt or response content

## Security guarantees

- Request and response bodies are **never** logged, stored, or sent anywhere
- Your KHG master key never appears in logs or error messages
- Provider API keys are decrypted locally in memory and never written to disk in plaintext
- Local SQLite log contains only: timestamp, provider, model, token counts, latency, outcome

---

## Installation

### Option A: pip (Python 3.11+)

```bash
pip install keyhive-proxy
```

### Option B: standalone executable

Download the latest release for your platform from the [Releases](../../releases) page:

| Platform | File |
|----------|------|
| Windows  | `keyhive-proxy-win-x64.exe` (NSIS installer) |
| macOS    | `keyhive-proxy-mac.dmg` |
| Linux    | `keyhive-proxy-linux.AppImage` |

---

## Quick start

### 1. Get your app token

Open your [KeyHive Garden proxy dashboard](https://keyhivegarden.com/profile/proxy), create an app token. Copy the token value — it is shown only once.

### 2. Configure

```bash
keyhive-proxy config set --key=sk-khg-xxxxxxxxxxxxxxxxxxxxxx
```

Optional — change listen port (default: 8080):

```bash
keyhive-proxy config set --port=9090
```

### 3. Start

```bash
# With system tray icon (default on desktop systems)
keyhive-proxy start

# Headless / server mode
keyhive-proxy start --no-tray
```

### 4. Point your app to the proxy

Replace the provider base URL in your app settings:

| App | Setting | Value |
|-----|---------|-------|
| **Cursor** | Settings → Models → OpenAI Base URL | `http://localhost:8080/v1` |
| **Cline** (VS Code) | API Provider → Base URL | `http://localhost:8080/v1` |
| **Continue** | `~/.continue/config.json` → `apiBase` | `http://localhost:8080/v1` |
| **Open WebUI** | Admin → Connections → OpenAI URL | `http://localhost:8080/v1` |
| **LangChain** | `openai_api_base` | `http://localhost:8080/v1` |

Use your app token (`sk-khg-...`) as the API key in all these apps.

---

## CLI reference

```
keyhive-proxy start [--no-tray]   Start the proxy
keyhive-proxy stop                Print stop instructions
keyhive-proxy status              Check if proxy is running
keyhive-proxy config set          Update configuration
keyhive-proxy logs [--limit N]    Print recent request log (default: 50 rows)
```

### `keyhive-proxy start`

| Flag | Default | Description |
|------|---------|-------------|
| `--no-tray` | off | Run without system tray icon (useful for servers and CI) |

### `keyhive-proxy config set`

| Option | Description |
|--------|-------------|
| `--key TEXT` | App token from KeyHive Garden dashboard |
| `--port INT` | Local listen port (default: 8080) |
| `--url TEXT` | KeyHive Garden base URL (default: https://keyhivegarden.com) |

### `keyhive-proxy logs`

| Option | Default | Description |
|--------|---------|-------------|
| `--limit INT` | 50 | Number of recent records to show |

Sample output:
```
2026-05-18T09:14:03  openai        gpt-4o-mini                       success          341ms
2026-05-18T09:14:18  anthropic     claude-3-5-haiku-20241022         success          892ms
2026-05-18T09:15:02  groq          llama-3.3-70b-versatile           rate_limited     128ms
2026-05-18T09:15:03  groq          llama-3.1-8b-instant              success          201ms
```

---

## API endpoints

The proxy exposes an OpenAI-compatible API on `http://localhost:<port>`:

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/chat/completions` | Chat completions (streaming supported) |
| `POST` | `/v1/messages` | Anthropic-style messages (proxied as chat completions) |
| `GET`  | `/v1/models` | List available models for your token |
| `GET`  | `/health` | Health check — `{"status":"ok","slots_active":N}` |

**Authentication:** `Authorization: Bearer sk-khg-<your-app-token>`

The `model` field in your request is ignored — the proxy selects the best available model from your pool. Use `GET /v1/models` to see what's available for your token.

---

## Retry and rotation behavior

The proxy automatically handles provider failures without interrupting your session:

- **429 Too Many Requests** → rotates to next slot in pool, retries immediately
- **500 / 502 / 503 / 504** → rotates to next slot, retries (up to 4 attempts)
- **All slots exhausted** → returns `503` with `{"error": {"type": "server_error"}}`

---

## Configuration file locations

| Platform | Config path |
|----------|-------------|
| Windows  | `%APPDATA%\keyhive-proxy\config.json` |
| macOS    | `~/Library/Application Support/keyhive-proxy/config.json` |
| Linux    | `~/.config/keyhive-proxy/config.json` |

Local request log database:

| Platform | Log DB path |
|----------|-------------|
| Windows  | `%APPDATA%\keyhive-proxy\logs.db` |
| macOS    | `~/Library/Application Support/keyhive-proxy/logs.db` |
| Linux    | `~/.local/share/keyhive-proxy/logs.db` |

Default config:

```json
{
  "khg_api_key": "",
  "listen_port": 8080,
  "log_retention_days": 30,
  "autostart": false,
  "khg_base_url": "https://keyhivegarden.com"
}
```

---

## System tray

On Windows and macOS, `keyhive-proxy start` shows a tray icon with a colored status circle:

| Color | Meaning |
|-------|---------|
| Green | Running, slots available |
| Yellow | Running, degraded (some slots rate-limited) |
| Red | Error or no slots available |
| Grey | Stopped / starting |

Right-click the tray icon for:
- Copy public URL (if Cloudflare tunnel is active)
- Open Settings
- Restart / Quit

---

## Public URL (Cloudflare Tunnel)

When started, the proxy automatically creates a temporary public URL via Cloudflare Tunnel. This allows remote access to your local proxy (e.g., from a cloud IDE):

```
https://random-name.trycloudflare.com/v1
```

The public URL is shown in the tray menu and on your [KeyHive Garden proxy dashboard](https://keyhivegarden.com/profile/proxy). The URL changes each time the proxy restarts. The tunnel starts automatically alongside the proxy — if it fails, the proxy continues working on localhost.

---

## Autostart

### Via tray Settings window

Open Settings from the tray icon and check the **Start on login** checkbox.

### Registration details

- **Windows:** `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`
- **macOS:** `~/Library/LaunchAgents/com.keyhive.proxy.plist`

---

## Multiple app tokens

You can create multiple app tokens — one per client app (Cursor, Cline, etc.). Each token is isolated: it gets its own key bundle from the pool. Revoke a single token without affecting others.

Create tokens at [keyhivegarden.com/profile/proxy](https://keyhivegarden.com/profile/proxy).

---

## Troubleshooting

### Proxy not responding on localhost

```bash
keyhive-proxy status
```

If it shows "Stopped", start it:
```bash
keyhive-proxy start --no-tray
```

### 401 Unauthorized from apps

Make sure you are using your **app token** (`sk-khg-...`) as the API key in the app, not a provider key or KHG account password.

### 503 No slots available

Your KeyHive Garden pool may be empty for the requested capability. Check the [pool dashboard](https://keyhivegarden.com/admin/pool) to see key status. Make sure at least one API key is verified for the `text` capability.

### Port already in use

If port 8080 is already in use:
```bash
keyhive-proxy config set --port=9090
keyhive-proxy start
```

Then update your app's base URL to `http://localhost:9090/v1`.

### Cloudflare tunnel not starting

The proxy works without the tunnel — it only affects remote/cloud access. If running from source, the `cloudflared` binary must be present at `proxy/tray/assets/cloudflared`:

```bash
# Linux/macOS
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
     -o proxy/tray/assets/cloudflared
chmod +x proxy/tray/assets/cloudflared
```

---

## Running from source

```bash
git clone https://github.com/keyhive/keyhive-proxy
cd keyhive-proxy
pip install -e ".[dev]"
keyhive-proxy config set --key=sk-khg-xxx
keyhive-proxy start --no-tray
```

Run tests:

```bash
pytest tests/ -v
```

---

## License

MIT
