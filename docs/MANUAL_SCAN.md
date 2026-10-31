# Manual live scan (not part of the tests)

Tests never touch the network. To check the real sources yourself:

```
./run.sh start
podman exec forge python -m app.scout.manual_scan
```

It fetches every enabled source with the default profile and prints, per source, how many listings
came back, plus notes (bad slugs, skipped sources, failures). Adzuna is skipped unless `ADZUNA_APP_ID`
and `ADZUNA_APP_KEY` are set in `.env`.

Be considerate when repeating it: Remotive asks for at most 4 requests a day, and Jobicy asks for
no more than one automated fetch per hour. Forge enforces both limits during real scans (Phase 5).
