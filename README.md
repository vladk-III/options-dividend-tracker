# 🌾 Options & Dividend Farm Dashboard — Render.com deploy

A mobile-friendly Dash app that tracks options premium income (with full
wheel-strategy cost-basis accounting) and dividend income (auto-synced from
Yahoo Finance since your purchase date), with a rotatable 3D farm — trees,
fields, and a barn — that grows as your income grows.

## 1. Push this folder to a GitHub repo

This is the repo Render will build from (your *code* repo). It's separate
from the data repo in step 3 below (your *data* repo).

```
git init
git add .
git commit -m "Options & dividend farm dashboard"
git branch -M main
git remote add origin https://github.com/your-username/your-code-repo.git
git push -u origin main
```

## 2. Create the Render web service

- Go to render.com → New → **Web Service** → connect the GitHub repo from
  step 1.
- If Render finds `render.yaml` in the repo, it offers a **Blueprint**
  deploy that fills in the build/start commands automatically — pick that.
- Otherwise, set these manually:
  - **Environment**: Python 3
  - **Build command**: `pip install -r requirements.txt`
  - **Start command**: `gunicorn app:server -b 0.0.0.0:$PORT`
  - **Plan**: Free

## 3. Create a *separate* GitHub repo for your data

- github.com → New repository → name it, e.g. `options-dividend-tracker` →
  Create. No files needed — the app creates `data/options_trades.csv`,
  `data/seed_lots.csv`, `data/holdings.csv`, and `data/dividends_cache.csv`
  automatically the first time you save.

## 4. Generate a GitHub personal access token

- github.com/settings/tokens → "Generate new token (classic)" → check the
  **`repo`** scope → Generate → copy it (you won't see it again).

## 5. Add the token as a Render environment variable

- On your Render service's page → **Environment** → **Add Environment
  Variable** → key `GITHUB_TOKEN`, value = the token you just generated →
  Save. Render redeploys automatically.
- This keeps the token out of your code and out of GitHub entirely.

## 6. Point the app at your data repo

- In `app.py`, set:
  ```python
  GITHUB_OWNER = "your-github-username"
  GITHUB_REPO = "options-dividend-tracker"
  ```
- Commit and push — Render redeploys on every push to the connected branch.

## 7. Open your app

Render gives you a URL like `https://options-dividend-farm.onrender.com`.
Bookmark it or add it to your phone's home screen for an app-like feel.

## Notes on the free tier

- Render's free web services spin down after ~15 minutes of inactivity.
  The first request after that takes about a minute to wake back up —
  normal for a personal-use tool like this, not something to fix.
- No credit card is required for the free web-service tier.
- Your GitHub token and data never touch Render's storage beyond running
  the app — all your actual trade/dividend data lives in your own GitHub
  data repo.
