# Job tracker setup (10 min, free)

1. **ntfy app**: install on phone, subscribe to a long random topic, e.g. `rk-jobs-7f3q9x2m`. (Topic name = password; keep it private.)
2. **GitHub**: create a PRIVATE repo, upload all files (keep the `.github/workflows/` folder path).
3. Repo > Settings > Secrets and variables > Actions > New secret: `NTFY_TOPIC` = your topic.
4. Actions tab > job-tracker > Run workflow:
   - mode `notify-test` -> phone should buzz.
   - mode `detect` -> prints which ATS each company uses. Paste each found board URL into `config.yaml` > `board_url`.
   - mode `run` -> first real run; sends current matches per company, then only NEW ones every hour.
5. Failures: after 3 failed checks in a row for a company you get a "Tracker broken" alert.

Edit `config.yaml` any time to change keywords, locations or companies.
