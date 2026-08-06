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
- Runs on Windows or Linux with no separate database server.

## Requirements

- Python 3.11 or newer from [python.org](https://www.python.org/downloads/).
- [SteamCMD](https://developer.valvesoftware.com/wiki/SteamCMD).
- A Steam account that can access the games and Workshop items you configure.
- Enough free disk space for SteamCMD's Workshop cache and generated ZIP files.

The Python dependencies are small: Flask provides the web application and
Waitress runs the production HTTP server.

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
username = "your-steam-login"
password = "your-steam-password"
guard_code = ""
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

### Steam account

| Setting | Purpose |
| --- | --- |
| `username` | Steam login name used by SteamCMD. |
| `password` | Steam password used by SteamCMD. |
| `guard_code` | Optional Steam Guard code. Leave blank unless SteamCMD requires one. |

Steam credentials are stored as plain text in `config.toml`. That file is
ignored by Git and must never be committed, uploaded, or shared. A separate
Steam account dedicated to this service is strongly recommended.

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
Expiring a ZIP only removes the ZIP itself.

### Worker

| Setting | Purpose |
| --- | --- |
| `max_queue_size` | Maximum number of waiting jobs. |
| `max_archive_gb` | Maximum allowed source size before ZIP creation is stopped. |
| `poll_seconds` | How often the worker checks for another job. |

## Adding or disabling games

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
```

Restart the application after changing the configuration.

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

- Confirm the login name and password in `config.toml`.
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
- Use a dedicated Steam account with only the access the service needs.
- Back up configuration privately, never inside the public repository.
