# Deployment

The bot runs on the VPS `66.116.251.242` (hostname `adity.app`) as a
systemd service, and is kept alive by an OpenShip scheduled job.

## Layout on the VPS

| Path | What it is |
| --- | --- |
| `/root/ontop` | git clone of this repo (`main` tracks `origin/main`) |
| `/root/ontop/.env` | `DISCORD_TOKEN` + `GOOGLE_APPLICATION_CREDENTIALS` (mode 600) |
| `/root/ontop/key.json` | Google service account key (mode 600) |
| `/root/ontop/.venv` | Python 3.12 virtualenv with the pinned dependencies |
| `/root/ontop/*.json`, `*.csv` | live registration state, git-ignored |
| `/usr/local/bin/ontop-watchdog` | health check + restart, run by OpenShip every 5 min |
| `/usr/local/bin/ontop-update` | pulls new commits and restarts, run by OpenShip every 5 min |
| `/var/log/ontop-watchdog.log` | watchdog log |
| `/etc/systemd/system/ontop-bot.service` | the service unit |

## The service

```
systemctl status ontop-bot
systemctl restart ontop-bot
journalctl -u ontop-bot -f
```

It restarts itself on any crash (`Restart=always`, no start rate limit), so
it comes back on its own after a bad deploy or a random exception.

## OpenShip jobs

Two scheduled jobs on the same server, both notifying the
`Ontop Ops (Discord)` channel when they intervene:

| Job | Schedule | What it does |
| --- | --- | --- |
| `ontop-bot watchdog` | every 5 min | restarts the bot if the unit is down, or if the process has lost its Discord gateway connection |
| `ontop-bot auto-update` | every 5 min | fast-forwards to `origin/main`, reinstalls dependencies, restarts, and rolls back if the bot does not come back healthy |

The watchdog exits `42` when it intervenes, which OpenShip reports as a
failed run and notifies about. A healthy run exits `0` and is silent.

## Updating

Push to `main`. Within five minutes the auto-update job picks it up,
restarts the bot, and rolls back automatically if the new code does not
come up healthy.

To update immediately:

```
ssh adity '/usr/local/bin/ontop-update'
```

## Notes

* Python 3.12 is required: `halwa.py` uses `asyncio.Runner()`, which only
  exists from 3.11 onwards. It is installed from the deadsnakes PPA.
* `requirements.txt` must stay UTF-8. It was UTF-16, which pip cannot read.
* `psutil` and `pytz` are direct dependencies and must stay pinned.
* Only one instance may run per bot token: Discord's gateway allows a
  single session per token, so do not run the bot locally while the VPS
  copy is up.
