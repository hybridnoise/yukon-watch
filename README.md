# yukon-watch

A tiny free watcher for new Yukon (yukon.org) / Eigen Labs challenge launches.

Every 15 minutes GitHub Actions runs `watch.py`, which checks:

- yukon.org homepage card links and the "Showing N of M" count
- `/api/challenges` names and `/api/benchmarks` track names + statuses (never scores)
- the full list of Layr-Labs public repo names
- "Validate submission" PRs, looking for repos not seen before
- Layr-Labs PublicEvent / repository CreateEvent

It saves fingerprints in `state.json` and POSTs a short JSON change note to a Grok Bot
webhook only when something differs. Midnight to 8am Eastern, changes are held until 8am.
If one source fails two checks in a row it sends one coverage alert, and one note when it recovers.

Repo secrets needed: `YUKON_WEBHOOK_URL` and `YUKON_WEBHOOK_KEY`.
The repo is public so scheduled runs are free. It contains no secrets.
