# radar-stats

Companion backend for **easyRADAR** — polls an ultrafeeder instance's live aircraft data and tracks achievements, XP, and records (rare aircraft, ranges, countries, airlines, altitude, message volume, day/night patterns, anniversaries) in a local SQLite database. Serves them as JSON under `/api/` for the frontend to render.

Not meant to run standalone outside the easyRADAR stack — see the [main repo](https://github.com/unLieb/easyradar) for full setup instructions.

Published as `ghcr.io/unlieb/easyradar-stats:latest` (`linux/amd64` + `linux/arm64`) - no need to clone this repo unless you want to modify the code.

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `SITE_LAT` / `SITE_LON` | Berlin city center | Your receiver's coordinates, used for range/direction/day-night calculations |
| `STATION_NAME` | *(empty)* | Shown in stats output |
| `TZ` | container default | Used for day/night and daily-record boundaries |

## License

MIT, see [LICENSE](LICENSE).
