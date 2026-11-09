# Workshop Downloader

A small self-hosted web queue for downloading Steam Workshop items with
SteamCMD and packing them into shareable ZIP files.

Choose a configured game, paste a Steam Workshop link, and the server handles
the download, archive creation, and temporary download link.

> This is an unofficial project and is not affiliated with Valve or Steam.
> Only download and share content you are allowed to access and redistribute.

## What it does

- Accepts Steam Community Workshop item links.
- Checks that the Workshop item belongs to the selected game when Steam
  metadata is available.
- Processes downloads one at a time through SteamCMD.
- Packs downloaded files into ZIP archives.
- Shows download progress and errors in a browser-based queue.
- Creates temporary download links and removes expired archives automatically.
- Supports an admin mode for viewing full errors and deleting queue entries.
- Lets the community share Steam accounts. Each account is validated with
  SteamCMD, re-checked every 15 minutes, and flagged when it breaks.
- Includes an admin dashboard showing which accounts work and which do not.
- Runs on Windows or Linux with no separate database server.

## Requirements

- Python 3.11 or newer from [python.org](https://www.python.org/downloads/).
- [SteamCMD](https://developer.valvesoftware.com/wiki/SteamCMD).
- One or more Steam accounts that can access the games and Workshop items.
- Enough free disk space for SteamCMD's Workshop cache and generated ZIP files.

The Python dependencies are small: Flask provides the web application,
Waitress runs the production HTTP server and `cryptography` encrypts stored
Steam credentials.

## Quick setup on Windows

### 1. Install Python

Install Python 3.11 or newer. During installation, enable **Add Python to
PATH**. You can check the installation in PowerShell:

```powershell
py -3 --version
```

### 2. Install SteamCMD

If `SteamCMD/steamcmd.exe` is not already included, download the
[Windows SteamCMD archive](https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip)
and extract it so the project looks like this:

```text
WorkshopDownloader/
├── SteamCMD/
│   └── steamcmd.exe
├── workshop_porter/
├── config.example.toml
├── main.py
└── start_windows.bat
```

SteamCMD will download its remaining runtime files the first time it starts.

### 3. Create the configuration

Run `start_windows.bat` once. If `config.toml` does not exist, the launcher
creates it from `config.example.toml` and asks you to edit it.

You can also create it manually:

```powershell
Copy-Item config.example.toml config.toml
```

At minimum, change these values:

```toml
[server]
host = "127.0.0.1"
port = 8765
public_base_url = ""

[security]
admin_token = "choose-a-long-private-password"
admin_password = "choose-a-different-admin-password"

[steam]
web_api_key = "your-steam-web-api-key"

[[steam.accounts]]
name = "primary"
username = "your-steam-login"
password = "your-steam-password"
guard_code = ""
steam_id = "your-64-bit-steam-id"
```

Keep `host = "127.0.0.1"` when the application is only used on this computer
or placed behind a reverse proxy. Use `0.0.0.0` only when it must accept direct
connections from other devices.

### 4. Start the server

Run:

```text
start_windows.bat
```

The launcher installs or updates the Python packages and starts the application
at:

```text
http://127.0.0.1:8765
```

Keep the terminal window open. Press `Ctrl+C` to stop the server.

## Quick setup on Linux

Install Python 3.11 or newer and SteamCMD using the instructions for your Linux
distribution. Confirm both are available:

```bash
python3 --version
steamcmd +quit
```

Then open the project directory and run:

```bash
cp config.example.toml config.toml
nano config.toml
python3 -m pip install --user -r requirements.txt
python3 main.py
```

The default Linux configuration looks for `steamcmd` on `PATH`:

```toml
[steam.linux]
steamcmd_path = "steamcmd"
```

When available, Linux also prefers the current user's Steam Workshop cache at
`~/.local/share/Steam/steamapps/workshop/content` for download detection and
cleanup. Set `[steam.linux].content_root` explicitly if your SteamCMD cache
lives somewhere else.

The application listens on port `8765` unless you change `[server].port`.

## Configuration reference

### Server

| Setting | Purpose |
| --- | --- |
| `host` | Address Waitress listens on. Use `127.0.0.1` locally or behind a proxy. |
| `port` | HTTP port. The default is `8765`. |
| `public_base_url` | Public HTTPS address used when creating download links, such as `https://mods.example.com`. Leave empty for local use. |
| `threads` | Waitress worker threads. Defaults to `16`. |
| `connection_limit` | Maximum open Waitress connections. Defaults to `200`. |
| `channel_timeout` | Seconds before Waitress closes an inactive connection. Defaults to `120`. |
| `max_downloads_per_ip` | Maximum simultaneous direct ZIP transfers per client IP. Defaults to `2`; there is no per-ZIP or global transfer limit. |
| `trusted_proxy_hops` | Number of trusted reverse proxies in front of Waitress. Use `1` for one local Nginx proxy and `0` for direct access. A nonzero value requires a loopback `host`. |
| `x_accel_redirect_prefix` | Optional Nginx internal URL used to offload ZIP transfers. Leave empty unless the matching Nginx location is configured. |

### Security

| Setting | Purpose |
| --- | --- |
| `admin_token` | Required in the page's **Password** field before jobs can be created or listed. |
| `admin_password` | Unlocks admin mode, including full error details and delete buttons. Use a different value from `admin_token`. |

To enter admin mode, open the following URL once:

```text
https://your-domain.example/?admin=YOUR_ADMIN_PASSWORD
```

The application replaces that URL with the normal home page and stores an
HTTP-only admin cookie for eight hours. Do not share the admin URL or leave it
in screenshots and browser history.

### Steam accounts and automatic Workshop catalogue

| Setting | Purpose |
| --- | --- |
| `web_api_key` | Optional Steam Web API key used to scan each account's owned games. Create it at `steamcommunity.com/dev/apikey`. |
| `[[steam.accounts]]` | Optional. Each block is imported once into the account pool on startup. |
| `name` | Public display name for the imported account. |
| `username` / `password` | SteamCMD login for this account. |
| `guard_code` | Optional Steam Guard code. Leave blank unless SteamCMD requires one. |
| `steam_id` | Optional 64-bit SteamID. It is normally read from SteamCMD's login output. |

Steam accounts now live in the database (see **Community account pool**
below). On startup, every `[[steam.accounts]]` entry (or the single
`[steam]` username/password) is imported into the pool once. After the accounts
show up in `/admin`, remove their passwords from `config.toml`.

With `web_api_key` set, every account check also reads the account's owned
games through the Steam Web API. The Store API then tells which of them
advertise **Steam Workshop**, and those titles are added to the game selector
automatically. Store answers are cached, so each game is looked up once. The
account's Steam **Game details** privacy setting must be **Public** for this
(**Profile → Edit Profile → Privacy Settings**). Accounts with a private
library still work for the configured `[[games]]`, and for Workshop games that
were already found in other libraries.

## Community account pool

Every download uses an account from a shared pool that owns the selected game.
Users add accounts under **Share a Steam account** on the main page.

1. A user enters a public display name, a Steam login and password, and
   optionally an emailed Steam Guard code. The site password is required.
2. The account is stored encrypted with status **validating**. The worker logs
   in with SteamCMD between downloads, runs `app_status` for the known games,
   and scans the library when a Web API key is set. The browser shows the
   result when the check finishes.
3. An account that logs in and owns at least one supported game becomes
   **working**. A wrong password, a Steam Guard block, a disabled account or
   owning none of the games marks it **broken**.
4. Working accounts are re-checked every `check_interval_minutes` (default
   15). Rate limits and Steam outages are retried after 5 minutes, and an
   account is only flagged broken after 3 of these failures in a row. Broken
   accounts are retried every `broken_recheck_minutes` (default 6 hours).
5. Downloads rotate between working accounts that own the game, using the
   least recently used account first. If a login fails during a download, the
   next account is tried and the failed one is queued for a re-check.

Public pages show only display names, status, games and check times. Logins
and passwords are never returned to visitors. A login that is already working
or waiting for validation cannot be submitted again, so nobody can overwrite
its password. A **broken** account can be resubmitted with a corrected
password.

A Steam Guard code is used once and then discarded. Accounts protected by
mobile-app approval usually cannot be used: SteamCMD has to be stopped after
`login_timeout_seconds`, and the account is flagged broken.

### Admin dashboard

After entering admin mode with `/?admin=YOUR_ADMIN_PASSWORD`, open `/admin`.
The dashboard shows every account with its Steam login (never the password),
SteamID and library visibility, status, failure reason, last SteamCMD output,
owned games, and its last, next and most recent use times. It also shows how
many working accounts cover each game. Accounts can be re-checked immediately,
disabled, enabled again or deleted.

### Credential encryption

Logins and passwords are encrypted with a key from
`[security].credentials_key`. If that setting is empty, the key is generated in
`data/credentials.key`. Back up the key privately. Without it, stored accounts
cannot be decrypted and must be submitted again.

Each check is a real Steam login. Many accounts at a 15-minute interval mean
many logins from one IP address, and Steam may answer with rate limits. If the
dashboard shows repeated `Steam rate limit reached` retries, raise
`check_interval_minutes`.

### Storage

| Setting | Purpose |
| --- | --- |
| `data_dir` | Parent directory for application data. |
| `database_path` | SQLite database containing queue jobs. |
| `archive_dir` | Directory containing completed ZIP archives. |
| `file_ttl_hours` | Hours before a ZIP link expires and its archive is deleted. |

After a ZIP is created successfully, the downloaded workshop content under the
configured SteamCMD Workshop cache is deleted automatically. The worker also
removes older cached workshop content once it is older than `file_ttl_hours`.
Before every download, it also clears that game's cached Workshop content,
temporary Workshop download data, and Workshop manifest. This prevents an
interrupted or partially deleted download from breaking the next job. It does
not remove the game's installation or cache for other games.
Expiring a ZIP only removes the ZIP itself.

### Worker

| Setting | Purpose |
| --- | --- |
| `max_queue_size` | Maximum number of waiting jobs. |
| `max_archive_gb` | Maximum allowed source size before ZIP creation is stopped. |
| `poll_seconds` | How often the worker checks for another job. |

## Adding or disabling games

With the automatic catalogue configured, you normally do not need this section:
the list is built at service startup. `[[games]]` remains useful as a fallback
when catalogue scanning is unavailable, or for restricting the manually offered
list.

Each game is an individual `[[games]]` entry:

```toml
[[games]]
name = "XCOM 2"
app_id = 268500
enabled = true
```

The `app_id` is the game's Steam application ID. It appears in the game's Steam
Store URL:

```text
https://store.steampowered.com/app/268500/XCOM_2/
                                      ^^^^^^
```

Restart the application after editing the game list. To temporarily hide a
game without deleting its entry, use:

```toml
enabled = false
```

Only regular Steam Community Workshop links are accepted, for example:

```text
https://steamcommunity.com/sharedfiles/filedetails/?id=1234567890
```

## Using the downloader

1. Open the web page.
2. Enter the configured `admin_token` in the **Password** field.
3. Choose the game that owns the Workshop item.
4. Paste the Workshop item URL.
5. Select **Add to queue**.
6. Wait for the job to move through `queued`, `downloading`, `packing`, and
   `ready`.
7. Download the ZIP or copy its temporary link.

If the same item is already queued or being processed, the application reuses
the existing job instead of downloading it twice.

## Running with systemd

The included service example expects the project at `/opt/WorkShopDownloader`
and a Linux system user named `workshop`.

```bash
sudo mkdir -p /opt/WorkShopDownloader
sudo cp -a /path/to/WorkshopDownloader/. /opt/WorkShopDownloader/
sudo useradd --system --create-home --shell /usr/sbin/nologin workshop
sudo chown -R workshop:workshop /opt/WorkShopDownloader
sudo install -d -o workshop -g workshop -m 0750 /var/log/workshop-downloader
sudo -u workshop cp /opt/WorkShopDownloader/config.example.toml /opt/WorkShopDownloader/config.toml
sudoedit /opt/WorkShopDownloader/config.toml
sudo -u workshop python3 -m pip install --user -r /opt/WorkShopDownloader/requirements.txt
sudo cp /opt/WorkShopDownloader/workshop-downloader.service.example /etc/systemd/system/workshop-downloader.service
sudo systemctl daemon-reload
sudo systemctl enable --now workshop-downloader
```

Check its status:

```bash
sudo systemctl status workshop-downloader
```

Follow its logs:

```bash
sudo tail -F /var/log/workshop-downloader/workshop-downloader.log
```

The service example writes application and SteamCMD diagnostics to
`/var/log/workshop-downloader/workshop-downloader.log`.

If you use a different project path or Linux user, update
`workshop-downloader.service.example` before installing it.

## Reverse proxy and public hosting

For a public installation, put the application behind an HTTPS reverse proxy
such as CloudPanel, Nginx, Caddy, or Apache.

Point the proxy to:

```text
http://127.0.0.1:8765
```

Then set the external address in `config.toml`:

```toml
[server]
host = "127.0.0.1"
port = 8765
public_base_url = "https://mods.example.com"
max_downloads_per_ip = 2
trusted_proxy_hops = 1
```

`trusted_proxy_hops = 1` trusts the client IP and HTTPS scheme added by that
one proxy. For security, it is accepted only while Waitress is bound to a
loopback address.

Restart the application after changing the configuration.

### Offload large ZIP downloads to Nginx

Waitress should not stream multi-gigabyte ZIP files on a busy public server.
Nginx can validate the public request through the application and then transfer
the file itself. Add an internal location to the Nginx server block:

```nginx
location /_protected_archives/ {
    internal;
    alias /opt/WorkShopDownloader/data/archives/;
    sendfile on;
}
```

The `alias` must be the absolute path configured as `[storage].archive_dir`, and
both the location and alias must end in `/`. Then enable the matching prefix:

```toml
[server]
host = "127.0.0.1"
port = 8765
public_base_url = "https://mods.example.com"
threads = 16
connection_limit = 200
channel_timeout = 120
max_downloads_per_ip = 2
trusted_proxy_hops = 1
x_accel_redirect_prefix = "/_protected_archives"
```

The internal location cannot be opened directly. The application still checks
the job status and secret download token before returning the internal redirect.

For a public service, also limit the number of downloads per client. Define the
shared zone once in Nginx's `http` context:

```nginx
limit_conn_zone $binary_remote_addr zone=workshop_downloads:10m;
```

Then add this inside the internal archive location:

```nginx
limit_conn workshop_downloads 2;
```

Reload Nginx and restart the application after validating both configurations.
If Nginx offload is not enabled, the application falls back to direct transfers
and limits each client IP to two simultaneous transfers. There is intentionally
no per-ZIP or global download limit.

## Files and data

```text
config.toml                 Private local configuration and credentials
config.example.toml         Configuration template; replace every credential
data/jobs.sqlite3           Queue database
data/archives/              Temporary ZIP downloads
SteamCMD/steamapps/         SteamCMD Workshop cache
workshop_porter/            Application source
```

The `config.toml`, `data/`, caches, and generated ZIP files are ignored by Git.

## Troubleshooting

### SteamCMD was not found

On Windows, confirm this file exists:

```text
SteamCMD/steamcmd.exe
```

On Linux, run `steamcmd +quit`. If that command is unavailable, either install
SteamCMD on `PATH` or put its full path in `[steam.linux].steamcmd_path`.

### Steam login fails

- Open `/admin` and expand **SteamCMD output** for the affected account.
- Start SteamCMD manually once and complete any Steam Guard or account prompts.
- Update `guard_code` if SteamCMD explicitly asks for a current code.
- Confirm the account owns or can access the selected game.

### The item belongs to another game

The selected game must match the Workshop item's Steam application ID. Choose
the correct game or add its `app_id` to `config.toml`.

### SteamCMD finishes without files

Not every Workshop item can be downloaded through SteamCMD. Check that the item
still exists, is public, supports the selected game, and is accessible to the
configured Steam account.

### Public download links use the wrong address

Set `[server].public_base_url` to the complete public HTTPS address, without a
path. For example:

```toml
public_base_url = "https://mods.example.com"
```

### Disk usage keeps growing

Completed jobs delete their downloaded Workshop content after the ZIP is created.
The worker also prunes older SteamCMD Workshop cache entries based on
`file_ttl_hours`. If disk usage still grows, check for failed jobs, unfinished
manual SteamCMD downloads, or archives waiting for their TTL to expire.

## Security checklist

Before making the service public:

- Replace every example password and token.
- Use different values for `admin_token` and `admin_password`.
- Confirm `config.toml` is not tracked by Git.
- Use HTTPS through a reverse proxy.
- Keep port `8765` private when a reverse proxy is used.
- Use Nginx offload and a per-client connection limit for public ZIP downloads.
- Use a dedicated Steam account with only the access the service needs.
- Back up configuration privately, never inside the public repository.
