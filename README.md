# Breaking Bot: Austin fire, Reddit and news

Rebuilt for `gabriel-dev-ux` after the original `gabrielkutnews/austin-fire-monitor` became unavailable. The original breaking-bot working folder was empty, so this is a new implementation of the fire/Reddit/news monitor, using the recovered layoff monitor's operating pattern.

## Sources and alert rules

- Austin Fire's current public Open Data feed: [Real-Time Fire Incidents](https://data.austintexas.gov/stories/s/dr26-vqib). Uses active FIRE records from dataset `wpu4-x69d`. Serious structure/brush fires, rescues, aircraft emergencies, hazmat, active attacks, flooding and highest-priority traffic incidents qualify. Routine alarms, odor/smoke investigations and small grass fires are suppressed by default.
- r/Austin's public new-post Atom feed. Matching breaking-news headlines are explicitly labeled **UNVERIFIED REDDIT TIP**.
- Google News RSS for Austin-area breaking-news terms. Requires a local headline and an allowlisted publisher. Fiction, drills and retrospectives are filtered out.

All rules, publisher domains and freshness windows are in `config.json`. An official dispatch is preliminary information, not a confirmed final incident outcome. The City feed is a snapshot and can miss very brief calls; medical calls are excluded by the source. Public RSS endpoints can become unavailable; source failures are logged independently without clearing deduplication state or blocking healthy sources.

## Run and test

Python 3.11+; no third-party packages or paid APIs.

```sh
python3 -m unittest discover -v
python3 fire_monitor.py --dry-run --check-sources
python3 fire_monitor.py --seed-only
python3 fire_monitor.py
```

Dry runs never send messages, read Slack credentials, or write state. `--seed-only` checkpoints current feeds without alerting, and requires all three sources to succeed. First successful contact with each source seeds it silently. Subsequent new events are deduplicated; a routine official incident upgraded to a serious category can produce an alert. Reddit/news stories older than six hours are ignored by default. Failed deliveries remain pending and retry only the missing recipients. State uses hashed recipient fingerprints rather than publishing Slack member IDs. Expired pending alerts are discarded with a log entry instead of being sent as stale breaking news.

Use `FIRE_STATE_FILE` to override the local checkpoint location. A malformed checkpoint causes a visible error rather than a silent reset.

## GitHub Actions setup

The **Breaking Bot / Austin Fire and News** workflow provides three manual modes:

1. `check`: run tests and check real feeds without Slack or state writes.
2. `baseline`: silently save current feed fingerprints to `state.json`.
3. `monitor`: start live polling once enabled.

Before activation, save two encrypted repository secrets:

- `SLACK_BOT_TOKEN`: an existing Slack bot token with permission to message the intended recipients.
- `SLACK_USER_IDS`: comma- or whitespace-separated Slack member IDs.

Run `baseline` successfully, set the repository variable `MONITOR_ENABLED` to `true`, then dispatch `monitor`. Polling runs every five minutes in a 340-minute job, commits state every 30 minutes, and chains its successor. An hourly backstop starts a monitor only when no other run is active. Missing Slack configuration fails before entering the loop.

To stop: set `MONITOR_ENABLED` to `false` and cancel the currently active monitor run. New live jobs will be skipped. Do not commit tokens or `.env` files. A new GitHub repository does not inherit secrets from the original repository.
