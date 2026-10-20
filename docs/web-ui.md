# Web UI

[Back to the README](../README.md)

Posteryard has a web UI on the same port as the webhook. Open `http://SERVER-IP:8000` in a browser. On Unraid, the port is `8098`, or choose **WebUI** in the container's menu.

## Sign in

Sign in with your `WEBHOOK_SECRET`. To use another password, set `UI_PASSWORD`, in the compose file or under **Settings**. A new password signs everyone else out.

After 10 wrong passwords, sign-in pauses for 5 minutes. A sign-in lasts 30 days.

## Pages

Search for a title from the bar at the top of every page. Press `/` to jump to it.

### Dashboard

What Posteryard is doing now, what waits in the queue, when the next sweep and the daily full pass run, and how many images are uploaded, in preview or failed. **Run sweep now** checks for new titles at once. **Run full pass now** checks every title. **Pause** stops sweeps, full passes and library work until you choose **Resume**. Work you start in the web UI still runs while paused.

The newest changes are listed below, with their posters.

### Library

Every managed library as a grid of posters. Filter by library, type and status, such as **Failed** or **By hand**, and sort by title or date. Choose **Select** to render or retry several titles at once.

### Title page

Open a title from the library or the search bar. It shows the poster and background Posteryard made next to the server's own, with the seasons and episodes of a show.

- **Preview without changing anything** renders the images in memory. Nothing is saved or uploaded.
- **Render now** renders and applies the images, even when nothing changed.
- **Art candidates** lists every picture from TMDB and the other sources, with the reason each was used or not. **Use this art** picks one.
- **Next art** switches to the next best picture. **Reset to automatic art** goes back.
- **Use my own image** takes a file or a web address. The title and badges are added on top.
- **Hand back to Posteryard** is for an image you changed on the server: Posteryard renders it again.

These are the same as the [commands](commands.md), and follow the same rules. With `DRY_RUN=true`, they save previews and change nothing on the server.

### Previews

The images saved in `data/previews` while `DRY_RUN` is on, newest first. Open one full size and step through them with the arrow keys. **Clear previews** deletes the files.

### Activity

- **Changes**: every image Posteryard made, newest first.
- **Queue**: the titles waiting, in the order they run.
- **Jobs**: the work you started in the web UI, and how it ended.
- **Log**: the service log, live. Filter it by level and copy it into a bug report.

### Problems

Titles that failed, with the error, and images that were changed by hand on the server, which Posteryard leaves alone. **Retry** tries a title again; **Retry all** tries every failed title. Open alerts are listed too.

### Live TV

The posters made for the Live TV guide, with `LIVE_TV=true`. See [Live TV](how-it-works.md#live-tv).

### Tools

- **Preview any TMDB title**: render a movie or show from TMDB, even one that is not in your library.
- **Send a test alert** to every address in `NOTIFY_URLS`.
- **Webhook for new titles**: the webhook address for Plex, Jellyfin or Emby, ready to copy.
- **Restore every image**: gives every uploaded image back to the server's own, before you remove Posteryard. Pause first. See [Upgrade, back up, uninstall](maintenance.md).

### Settings

Every setting from the [README](../README.md#settings), in groups, with a search box. Each one says where its value comes from: the web UI, the compose file, or the default.

- **Save** keeps the changes in `data/settings.json`. They override the same lines in the compose file.
- **Use environment value** drops a value saved in the web UI, so the compose file's value applies again.
- The **Test** buttons for the media server, TMDB, fanart.tv, Maintainerr and alerts try the connection with the values on the page, before you save.
- **Restart now** restarts Posteryard to apply saved settings. The container keeps running.
- **Go live** turns `DRY_RUN` off from the web UI.

Keys, tokens and passwords are never shown again after they are saved. Leave one empty to keep it.

`DATA_DIR`, `LISTEN_PORT` and `WEB_UI` can only be set in the compose file: a wrong value saved in the web UI could lock you out of it.

To start over, stop the container and delete `data/settings.json`.

## Setup mode

When the settings do not load, for example on a first start without a TMDB key, Posteryard does not exit. It starts only the web UI, on `LISTEN_PORT`, and the log says why. Open the web UI, fill in what is missing under **Settings**, save, and choose **Restart and start Posteryard**.

If neither `UI_PASSWORD` nor `WEBHOOK_SECRET` is set, the first visitor chooses the password. Do this right away, before anyone else on your network does.

The health check fails in setup mode, so Docker shows the container as unhealthy until the settings load.

With `WEB_UI=false`, a setting that does not load stops the container, as before.

## Security

- Anyone with the password can change the settings and every image on your server. Use a long random password, such as the output of `openssl rand -hex 16`.
- The web UI uses plain HTTP. Keep it on your own network. To reach it from outside, put it behind a reverse proxy with HTTPS, such as Nginx Proxy Manager, Caddy or Traefik, or use a VPN such as Tailscale or WireGuard.
- `data/settings.json` holds the keys you saved in the web UI. Only the container's user can read it.
- `WEB_UI=false` turns the web UI off. The webhook and `/healthz` keep working.

## Try it without a server

The demo runs the real service and web UI with a made-up library and art drawn on your computer. It needs no media server and no TMDB key. From a copy of the repository:

```sh
PYTHONPATH=. uv run python tools/webui_demo.py
```

Open `http://localhost:8000` and sign in with `demo`. Add `--live` to "upload" to the made-up server instead of saving previews, and `--port` or `--data` to change the port or the data folder.
