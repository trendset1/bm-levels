# bm-levels (GitHub hosting)

Same `gen_levels.py` you already use, run every 15 minutes by GitHub Actions.
Bookmap Cloud Notes reads the CSV from a public raw URL. Nothing needs to stay
open on your PC.

No API keys. Data comes from Deribit's public market API. The repo must stay
**public** — Cloud Notes cannot send a GitHub login, so a private raw URL will
fail.

## Setup (web UI)

1. Get a free GitHub account at https://github.com/signup if you don't have one.
2. Click **New repository**. Name it `bm-levels`, set it to **Public**. A README
   is optional. Click **Create repository**.
3. Unzip `bm-levels-cloud.zip` on your Desktop.
4. In the new repo, click **Add file → Upload files**. Drag in everything from
   the unzipped folder, **including the `.github` folder itself** (not only the
   file inside it), so the path stays `.github/workflows/levels.yml`. Click
   **Commit changes**.
5. Repo **Settings → Actions → General → Workflow permissions → Read and write
   permissions → Save**.
6. Open the **Actions** tab, click **Update levels**, then **Run workflow**.
   The first run takes about a minute. When it is green, the two CSV files are
   in the repo.

Cloud Notes URLs (replace `YOURUSER` with your GitHub username):

```
https://raw.githubusercontent.com/YOURUSER/bm-levels/main/bm_levels.csv
```

Optional second column, for pWH / pWL sound alerts:

```
https://raw.githubusercontent.com/YOURUSER/bm-levels/main/bm_levels_alerts.csv
```

Set the Cloud Notes download interval to **5 minutes**. After that you can close
the local `serve_levels.py` window.

## Setup (GitHub CLI, if you already use `gh`)

Unzip, then in PowerShell 7, one command at a time:

```
Expand-Archive $HOME\Desktop\bm-levels-cloud.zip -DestinationPath $HOME\Desktop\bm-levels-cloud -Force
```

```
cd $HOME\Desktop\bm-levels-cloud
```

```
gh auth login
```

```
gh repo create bm-levels --public --source=. --remote=origin --push
```

Then do step 5 above (Actions write permission) and start one run:

```
gh workflow run levels.yml
```

## What to expect

- The job is scheduled every 15 minutes (`*/15 * * * *`). GitHub cron is
  best-effort and can start several minutes late.
- `raw.githubusercontent.com` caches for about **5 minutes**. That matches a
  5-minute Cloud Notes interval, so GitHub Pages is not needed (Pages is usually
  cached at least as long, and is more setup).
- End to end, notes are typically 15–30 minutes fresh (cron + job + raw cache +
  Bookmap's own interval). A delayed cron can stretch that.
- Public repos are the right plan here (Actions minutes stay free). The levels
  are built from public data.
- GitHub disables scheduled workflows on **public** repos after **60 days with
  no repository activity**. A scheduled run by itself does not count. This
  workflow commits to `main` when the CSVs or the OI snapshot file change
  (the snapshot updates about hourly), and those commits should keep it alive.
  If a workflow ever shows as disabled, open **Actions**, click **Update
  levels**, and **Enable workflow**, then run it once.

Defaults match your local generator (no extra flags). To change them later, edit
the `python gen_levels.py --out out` line in `.github/workflows/levels.yml`.
