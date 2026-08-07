# radar-stats

Companion backend for **easyRADAR** — polls an ultrafeeder instance's live aircraft data and tracks achievements, XP, and records (rare aircraft, ranges, countries, airlines, altitude, message volume, day/night patterns, anniversaries) in a local SQLite database. Serves them as JSON under `/api/` for the frontend to render.

Not meant to run standalone outside the easyRADAR stack — see the main repo for full setup instructions.

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `SITE_LAT` / `SITE_LON` | Berlin city center | Your receiver's coordinates, used for range/direction/day-night calculations |
| `STATION_NAME` | *(empty)* | Shown in stats output |
| `TZ` | container default | Used for day/night and daily-record boundaries |

## License

MIT, see [LICENSE](LICENSE).
