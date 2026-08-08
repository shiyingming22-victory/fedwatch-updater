# fedwatch-updater

Automatically computes CME FedWatch-style FOMC probabilities (from CME
30-Day Fed Funds futures settlements + FRED EFFR) and publishes
`fedwatch.json` to this repository every weekday morning (UTC 08:30).

The local Centaur pipeline pulls the file through the jsdelivr CDN:

```
https://cdn.jsdelivr.net/gh/<owner>/<repo>@main/fedwatch.json
```

`consensus_high` = max single outcome probability >= 80%.
