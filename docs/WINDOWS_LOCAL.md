# Run CatalystEdge on your Windows computer (Docker Desktop)

For someone with no technical background. Every step says what you should see, what a problem looks
like, and what to do about it. Takes about 45 minutes the first time, mostly waiting.

> While it runs on your computer, the scheduler and email alerts only work while the computer is
> **on and awake**. For alerts while it's off, use [DEPLOY_FREE.md](DEPLOY_FREE.md).

## What you need
- **Docker Desktop**, installed and running (the whale icon near the clock).
- At least **8 GB of memory** and **10 GB of free disk space**.
- Your **Finnhub** key, **Tiingo** key, and your name + email for the SEC.
- Optional: a **Resend** key for email alerts (see "Email alerts" below).

---

## Step 1: Start Docker Desktop
Open **Docker Desktop** from the Start menu and wait until the bottom-left corner says **Engine running**
(green).

- **Problem:** it says "WSL needs updating" or "Virtualization is not enabled". Follow the button Docker
  shows ("Update WSL"), then restart the computer. If it mentions virtualization, it must be switched on
  in your computer's BIOS. Search the web for your computer model plus "enable virtualization".

## Step 2: Download CatalystEdge
1. Open this link in your browser. It downloads a ZIP file of the app:
   https://github.com/Eswarpavan/Claude-Code/archive/refs/heads/claude/adoring-dirac-ohiyp8.zip
2. Open your **Downloads** folder, right-click the ZIP → **Extract All…** → type `C:\` as the destination →
   **Extract**.
3. In File Explorer, go to `C:\`. You'll see a folder called `Claude-Code-claude-adoring-dirac-ohiyp8`.
   Right-click it → **Rename** → type `CatalystEdge`.

You now have `C:\CatalystEdge`, which contains files such as `docker-compose.yml`, `.env.example` and `README.md`.

## Step 3: Create your settings file (`.env`)
The `.env` file holds your keys. It lives **only in `C:\CatalystEdge` on your computer** and is never uploaded.

1. Press the **Windows key**, type `PowerShell`, and open **Windows PowerShell**.
2. Type these lines one at a time, pressing **Enter** after each:
   ```powershell
   cd C:\CatalystEdge
   copy .env.example .env
   notepad .env
   ```
3. Notepad opens `.env`. Fill in the values **right after the `=`**, with no spaces and no quotes:

   | Line in the file | What to put there | Needed? |
   |---|---|---|
   | `FINNHUB_API_KEY=` | your Finnhub key | yes |
   | `TIINGO_API_KEY=` | your Tiingo key | yes |
   | `SEC_USER_AGENT=` | your name and email, e.g. `Jane Doe jane@example.com` | yes |
   | `ALERT_EMAIL_TO=` | your email address | for email alerts |
   | `RESEND_API_KEY=` | your Resend key (starts with `re_`) | for email alerts |
   | everything else | leave as it is | no |

   Leave `APP_PASSWORD` empty on your own computer. The dashboard is only reachable from this
   computer, so no login is needed.
   The text after a `#` on a line is only a note, so you can leave it.
4. **File → Save** (Ctrl+S), then close Notepad.

- **Problem:** Notepad saved it as `.env.txt`. In PowerShell run `ren .env.txt .env`. To check, run `dir .env*`:
  you should see both `.env` and `.env.example`.
- **Never** paste your keys anywhere else (chat, email, screenshots). If a key leaks, make a new one on the
  provider's website and replace it in `.env`.

## Step 4: Start everything
In the same PowerShell window (still in `C:\CatalystEdge`):
```powershell
docker compose up -d
```
**The first time takes 15–30 minutes.** It builds the app and installs the AI libraries. You'll see lots of
lines scroll past. It's done when you get the `PS C:\CatalystEdge>` prompt back and the last lines say
`Started` or `Healthy`.

Check that everything is up:
```powershell
docker compose ps
```
You should see `api`, `worker`, `beat`, `web`, `postgres` and `redis` with **Up** (api and postgres also
say **healthy**). `migrate` is gone or **Exited (0)**. That's correct: it set up the database and stopped.

**Problems at this step:**
- `error during connect` or `The system cannot find the file specified`: Docker Desktop isn't running
  (step 1).
- `no configuration file provided`: you're not in the right folder. Run `cd C:\CatalystEdge` first.
- `port is already allocated` (3000 or 8000): another program uses that port. Close it, or restart the
  computer, then run `docker compose up -d` again.
- The build stops with `timesfm` in the error: open `.env` again (`notepad .env`), add a new line
  `INSTALL_TIMESFM=false`, save, and run `docker compose up -d --build`.
- It stops with a download or network error: check your internet connection and run
  `docker compose up -d` again. It carries on from where it stopped.
- A service keeps restarting: see why with `docker compose logs --tail 50 api` (or `worker`, `web`).
  Copy the last lines into the chat. They never contain your keys.

## Step 5: Open the dashboard
Open your browser and go to **http://localhost:3000**.

- **Problem:** "This site can't be reached". Wait a minute (the `web` service starts last) and reload. Still
  nothing? Run `docker compose ps`. `web` must say Up.
- **Problem:** the page loads but says it can't reach the API. Open **http://localhost:8000/health**. It
  should show `"status":"ok"` and `"database":"ok"`. If it doesn't load, check `docker compose logs --tail 50 api`.

## Step 6: What each page should show when everything works

On the first visit the app starts a **refresh** by itself (news → events → prices → signals →
portfolio). The progress bar at the top takes a few minutes the first time.

### Sources (Source Health)
- **Finnhub news**, **Tiingo prices** and **SEC EDGAR** show a green **ok** after the first refresh.
- **Benzinga** and **Investing.com** show **disabled**. That's intentional; they're placeholders.
- **Marketaux** and **Alpha Vantage** show **disabled** unless you added their optional keys.
- **Yahoo**, **Stooq**, **openFDA** and **ClinicalTrials.gov** should work on your computer. They were only
  blocked in the build environment.
- **Sentiment models**: **FinBERT** first shows **downloading** (about 0.5 GB, first run only), then **ready**.

**What a problem looks like:** a red **failed** badge with an error.
- `401` or `403` on Finnhub or Tiingo: the key in `.env` is wrong. Fix it, then run `docker compose up -d`.
- `hourly limit` or `budget` on Tiingo: the free plan allows 50 requests an hour. The app waits and tries
  again later by itself.
- FinBERT **failed**: the app automatically uses the labelled **word-list fallback**, so signals still
  work. Run `docker compose restart worker api` to try the download again.

### Signals
- Every card has an **UNCALIBRATED** label, and the banner at the top explains why (not enough real
  outcomes yet).
- Only signals with confidence **65 or more** appear. **80+** are highlighted.
- **Earnings beats** and **FDA approvals** don't appear. The backtest switched those rules off because
  they didn't beat the S&P 500. They're still recorded, so they can earn their way back on live results.
- Hover over a ticker to see the headlines, the reasons, the stop and the target.
- **An empty Signals page is normal** on quiet news days, or before the first end-of-day run (4:45 pm
  New York time on weekdays). It isn't an error.

**What a problem looks like:** "Showing cached data" stays up for more than 15 minutes, or a red refresh
error appears. Open **Sources** to see which source failed.

### Settings
- **Auto-buy**: the switch exists, but the text below it says **"Auto-buy is not active: …"**. That is
  correct. It stays locked until 30 paper trades have closed, or a backtest proves the scores are
  calibrated. The switch alone can't turn it on.
- **TimesFM**: **Off**. It's optional, and the backtest showed it didn't help.
- **Local AI explanations (Ollama)**: **Off** (optional; see the README).
- **Email send log**: empty at first. With the Resend key set, click **Send test email**. Within a minute
  the log shows **sent** and the email is in your inbox. Check spam too.

**What a problem looks like:** the test email shows **failed** with an error. The Resend key is wrong, or
`ALERT_EMAIL_TO` isn't the address you signed up to Resend with (without your own domain, Resend only
delivers to that address). Fix `.env`, run `docker compose up -d`, and try again.

### Backtest
- The **calibration banner** says **UNCALIBRATED**.
- **Hit rate and average return by catalyst** lists each catalyst with its status:
  - **insider buying**: on;
  - **earnings beat** and **FDA approval**: off;
  - the rest: **on · unproven**.
  These come from the real backtest of 2026-09-24, which ships with the app.
- **Walk-forward backtest** says **"No backtest yet"** on a new computer. The full numbers are in
  `docs/PHASE1_PROGRESS.md`. In short:
  - the rules' average trade was slightly better than the S&P 500 (+0.48% vs +0.42% after costs) but
    much bumpier (Sharpe 0.42 vs 1.06);
  - simply taking every positive event did a bit better than the rules;
  - picking by confidence showed no real skill.
  - The ranking model made results worse, so it stays **off**, and TimesFM didn't help.
- To produce the backtest on your own computer (optional, a few hours because of Tiingo's hourly limit),
  run the commands in the README section "Backtest, ranking model and TimesFM".
- **Live outcomes by confidence bucket** fill in 1, 3 and 10 trading days after signals appear.

## Everyday use
- **Stop:** `docker compose down` (your data is kept).
- **Start again:** open Docker Desktop, then in PowerShell: `cd C:\CatalystEdge` and `docker compose up -d`.
  It takes seconds after the first time.
- **Update to a newer version:** download the ZIP again (step 2) and extract it over `C:\CatalystEdge`
  (your `.env` is kept because the ZIP doesn't contain one). Then run `docker compose up -d --build`.
- **Delete everything, including data:** `docker compose down -v`.

## Email alerts on your computer
1. Sign up at https://resend.com with the email where you want alerts.
2. **API Keys → Create API Key** (Sending access). Copy it into `.env` as `RESEND_API_KEY=`.
3. Set `ALERT_EMAIL_TO=` to that same email. Save, and run `docker compose up -d`.
4. Settings → **Send test email**.

*Not financial advice. CatalystEdge is a research and paper-trading tool.*
