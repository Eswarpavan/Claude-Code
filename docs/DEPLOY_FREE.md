# CatalystEdge online for $0 (Oracle + Neon + Vercel)

This puts CatalystEdge on the internet so the scheduler, paper trades and email alerts keep
running **while your own computer is off**. Everything here uses free plans.

> To run it on your own Windows computer instead, see [WINDOWS_LOCAL.md](WINDOWS_LOCAL.md).
> Free-plan limits were checked on 2026-09-24; the guide was re-checked against the app on 2026-10-01
> (Upstash is no longer needed: see "Free-tier budget notes"). Websites change their buttons from time to time:
> if a button has a slightly different name, look for the closest match.

**Time needed:** about 1.5–2 hours the first time, mostly waiting.
**What you type:** everything to type is in a grey box. Copy it exactly (Ctrl+C, then right-click to
paste into the black server window).

## What goes where

| Service | What it does for CatalystEdge | Free limit that matters |
|---|---|---|
| **Oracle Cloud** (a rented computer, the "VM") | runs the API, the scheduler (Celery beat), the worker, its own Redis (shared memory for rate limits and the scheduler heartbeat) and HTTPS | Always Free: 2 CPUs + 12 GB memory (ARM) |
| **Neon** | the database (signals, paper account, outcomes) | 0.5 GB storage, 100 compute-hours a month |
| **Vercel** | the dashboard website | free "Hobby" plan, personal use only |
| **DuckDNS** | a free web address for the VM, so it can have HTTPS | free |
| **Resend** | sends the alert emails | 3,000 a month, 100 a day |

The database lives on Neon, not on the VM, so if the VM is ever rebuilt your history is safe.
The cloud schedule polls news every 15 minutes in market hours (every 5 minutes locally), which keeps
Neon under its free 100 compute-hours: roughly 55–65 are used per month (see "Free-tier budget notes").

## What you must sign up for yourself

I can't create accounts for you. You need these five (all free; Oracle asks for a card to prove
you're a real person but does not charge for Always Free resources):

1. Neon: https://neon.com
2. Resend: https://resend.com (for the emails)
3. Oracle Cloud: https://www.oracle.com/cloud/free/
4. DuckDNS: https://www.duckdns.org (sign in with GitHub or Google)
5. Vercel: https://vercel.com (sign in with your GitHub account)

(Earlier versions of this guide also used Upstash. It is no longer needed: skip it.)

**Have ready before you start:** a GitHub login (for Vercel, and optionally DuckDNS/Neon), the email
address where you want alerts, a payment card (Oracle's identity check only), Windows PowerShell (built
in), and your existing values: `FINNHUB_API_KEY`, `TIINGO_API_KEY` and `SEC_USER_AGENT` (your name and
email). Optional free keys, any time later: `FRED_API_KEY` (economic release calendar),
`SAM_GOV_API_KEY` (SAM.gov contract notices). Without them those two sources show AUTH REQUIRED on the
Sources page; everything else works.

Keep a Notepad file open while you go,
**on your own computer only**, and paste each value into it as you collect it. Delete that file
when you finish.

> The GitHub repository `Eswarpavan/Claude-Code` is **public**, so anyone can read the code. That is
> fine: no keys are in it, and the app needs your password. If you'd rather keep it private, change it
> in GitHub → the repo → Settings → General → Danger Zone → Change visibility. Private repositories
> need an extra login step on the VM that this guide does not cover.

---

## Step 1: Neon (the database), about 5 minutes

1. Go to https://neon.com → **Sign up** (GitHub or Google is quickest).
2. It asks you to create a project:
   - Project name: `catalystedge`
   - Postgres version: leave the default
   - Region: **AWS US East 1 (N. Virginia)**
   - Click **Create project**.
3. On the project dashboard click **Connect** (top right).
4. You need **two** addresses from this box:
   - With **Connection pooling ON**, copy the string. It contains `-pooler`. In Notepad, write
     `DATABASE_URL=` and paste it right after the `=`.
   - Switch **Connection pooling OFF** and copy again. In Notepad, write `DATABASE_URL_DIRECT=` and
     paste it after the `=`.
   - Both start with `postgresql://`. That is fine; the app accepts them as they are.

**Problem?** If you don't see a Connect button, open the project, then **Dashboard → Connection
details**.

## Step 2: Resend (emails), about 3 minutes

1. Go to https://resend.com → **Sign up** with the email address where you want alerts.
2. Left menu **API Keys** → **Create API Key** → name `catalystedge`, permission **Sending access** →
   **Add**. Copy the key (starts with `re_`) straight away; it is shown only once.
   In Notepad: `RESEND_API_KEY=` then paste.
3. In Notepad also write `ALERT_EMAIL_TO=` followed by **the same email you signed up to Resend with**.
   Without your own domain, Resend only delivers to that address, which is all you need.
   Leave `EMAIL_FROM` empty; the app then uses Resend's test sender.

## Step 3: Oracle Cloud (the always-on computer), about 30–45 minutes

### 3a. Create the account
1. Go to https://www.oracle.com/cloud/free/ → **Start for free**.
2. Fill in the form. For **Home Region**, choose **US East (Ashburn)**. This cannot be changed later.
3. Add the card for verification and finish. Account setup can take up to 15 minutes; wait for
   the "Your account is ready" email.
4. Recommended: **upgrade to Pay As You Go** (☰ menu → **Billing & Cost Management** →
   **Upgrade and Manage Payment** → Pay As You Go). You still pay $0 if you only use Always Free
   resources. It protects your server from being shut down for looking "idle", and makes "out of
   capacity" errors rarer. Then set a safety alarm: ☰ → **Billing & Cost Management** → **Budgets** →
   **Create Budget**, amount `1`, alert at 100%, your email.

### 3b. Create the server
1. ☰ menu → **Compute** → **Instances** → **Create instance**.
2. Name: `catalystedge`.
3. **Image and shape** → **Edit**:
   - **Change image** → **Canonical Ubuntu** → version **24.04** → **Select image**.
   - **Change shape** → **Ampere** → tick **VM.Standard.A1.Flex** → set **OCPUs = 2** and
     **Memory = 12 GB** → **Select shape**. It must say "Always Free-eligible".
4. **Networking**: leave the defaults (create a new virtual cloud network and a public subnet),
   and make sure **Automatically assign public IPv4 address** is on.
5. **Add SSH keys** → **Generate a key pair for me** → click **Download private key**. Keep this file
   safe. It is the key to your server. It lands in your Downloads folder with a name like
   `ssh-key-2026-09-25.key`.
6. Click **Create**. After 1–2 minutes the status turns green: **Running**. Copy the
   **Public IP address** (for example `129.80.12.34`) into Notepad.

**Problem: "Out of capacity for shape VM.Standard.A1.Flex".** Oracle has run out of free ARM
machines in that zone. Try a different **Availability domain** (AD-2 or AD-3) in the Placement
section, or try again in a few hours. Upgrading to Pay As You Go (3a step 4) usually helps.

### 3c. Open the web ports (80 and 443)
1. On the instance page, click the **subnet** link (under Primary VNIC, or on the **Networking** tab).
2. Click the **Security List** named "Default Security List for …".
3. **Add Ingress Rules**:
   - Source CIDR: `0.0.0.0/0`
   - IP Protocol: **TCP**
   - Destination Port Range: `80,443`
   - Click **Add Ingress Rules**.

### 3d. Give the server a web address (DuckDNS)
1. Go to https://www.duckdns.org and sign in.
2. Type a name in **sub domain** (for example `catalystedge-yourname`) → **add domain**.
3. In the **current ip** box next to it, paste your server's Public IP → **update ip**.
4. Your address is now `catalystedge-yourname.duckdns.org`. In Notepad:
   `PUBLIC_HOSTNAME=catalystedge-yourname.duckdns.org`.

### 3e. Connect to the server from Windows
1. Press the Windows key, type **PowerShell**, and open **Windows PowerShell**.
2. Type this, replacing the file name and IP with yours, then press Enter:
   ```powershell
   ssh -i $HOME\Downloads\ssh-key-2026-09-25.key ubuntu@129.80.12.34
   ```
3. It asks "Are you sure you want to continue connecting?". Type `yes` and press Enter.
4. You're in when the prompt looks like `ubuntu@catalystedge:~$`. Everything below is typed here.

**Problem: "UNPROTECTED PRIVATE KEY FILE" or "bad permissions".** Windows lets too many users read
the key. Run these three lines in PowerShell (use your file name), then try step 2 again:
```powershell
icacls $HOME\Downloads\ssh-key-2026-09-25.key /inheritance:r
icacls $HOME\Downloads\ssh-key-2026-09-25.key /grant:r "$($env:USERNAME):(R)"
icacls $HOME\Downloads\ssh-key-2026-09-25.key /remove "Authenticated Users" "BUILTIN\Users" "Everyone"
```
**Problem: "Connection timed out".** Check the IP address, and that the instance says Running.

### 3f. Install Docker and download CatalystEdge (on the server)
Paste these lines one at a time and press Enter after each. The first can take a few minutes.
```bash
sudo apt-get update && sudo apt-get install -y iptables-persistent git
```
(If a pink screen asks about saving rules, press Enter for **Yes** both times.)
```bash
sudo iptables -I INPUT 6 -p tcp --dport 80 -j ACCEPT && sudo iptables -I INPUT 6 -p tcp --dport 443 -j ACCEPT && sudo netfilter-persistent save
```
```bash
curl -fsSL https://get.docker.com | sudo sh && sudo usermod -aG docker ubuntu
```
```bash
exit
```
Connect again with the same `ssh …` line from 3e (this makes the Docker permission take effect), then:
```bash
git clone -b claude/adoring-dirac-ohiyp8 https://github.com/Eswarpavan/Claude-Code.git catalystedge && cd catalystedge
```

### 3g. Create the settings file on the server
1. Make a long random password-signing secret and copy what it prints into Notepad as `APP_SECRET=`:
   ```bash
   openssl rand -hex 32
   ```
2. Choose a **login password** for the dashboard (at least 12 characters, one you don't use
   elsewhere). In Notepad: `APP_PASSWORD=` then your password. **This is required.** In the cloud
   profile every page stays locked (error 503) until it is set.
3. Start from the example file and open the editor:
   ```bash
   cp .env.cloud.example .env && chmod 600 .env && nano .env
   ```
4. In the editor, use the arrow keys to move. Fill in these lines by pasting your Notepad values
   after the `=` (right-click pastes):

   | Line | Value |
   |---|---|
   | `FINNHUB_API_KEY=` | your Finnhub key |
   | `TIINGO_API_KEY=` | your Tiingo key |
   | `SEC_USER_AGENT=` | your name and email, e.g. `Jane Doe jane@example.com` |
   | `ALERT_EMAIL_TO=` | your email (step 2) |
   | `RESEND_API_KEY=` | the `re_…` key (step 2) |
   | `APP_PASSWORD=` | your dashboard password |
   | `APP_SECRET=` | the random secret |
   | `PUBLIC_HOSTNAME=` | `catalystedge-yourname.duckdns.org` |
   | `DATABASE_URL=` | the Neon **pooled** address (step 1) |
   | `DATABASE_URL_DIRECT=` | the Neon **direct** address (step 1) |
   | `FRED_API_KEY=`, `SAM_GOV_API_KEY=` | optional; leave empty if you don't have them yet |
   | `CORS_ORIGINS=` | leave it for now; you'll fill it in step 5 |

5. Save and close: press **Ctrl+O**, then **Enter**, then **Ctrl+X**.

### 3h. Start it
```bash
docker compose -f docker-compose.cloud.yml up -d --build
```
The first build takes **15–30 minutes** (it installs the AI libraries). When the prompt comes back:
```bash
docker compose -f docker-compose.cloud.yml ps
```
You should see `api`, `worker`, `beat`, `redis` and `caddy` all **Up** (api says **healthy** after a
minute). `migrate` shows **Exited (0)**. That's correct: it set up the database tables and stopped.

Then open `https://catalystedge-yourname.duckdns.org/health` in your browser. You should see text
starting with `{"status":"ok","profile":"cloud"` and `"database":"ok"`.

**Problems at this step:**
- `migrate` shows **Exited (1)**: the Neon address is wrong. Run
  `docker compose -f docker-compose.cloud.yml logs migrate`, fix `DATABASE_URL_DIRECT` with
  `nano .env`, then run the `up -d` line again.
- `/health` doesn't load or shows a certificate warning: DuckDNS isn't pointing at the server yet
  (check 3d), or ports 80/443 are closed (check 3c and the iptables line in 3f). Then run
  `docker compose -f docker-compose.cloud.yml restart caddy` and wait 2 minutes.
- `/health` says `"status":"degraded"`: the database can't be reached. Check `DATABASE_URL`.
- The build stops with an error mentioning `timesfm`: add the line `INSTALL_TIMESFM=false` to `.env`
  and run the `up -d --build` line again. TimesFM is off by default anyway.

## Step 4: Vercel (the dashboard website), about 10 minutes

1. Go to https://vercel.com → **Sign up** → **Continue with GitHub**.
2. **Add New…** → **Project** → find **Claude-Code** → **Import**.
3. On the setup screen:
   - **Root Directory** → **Edit** → choose `frontend` → **Continue**. Framework shows **Next.js**.
   - Open **Environment Variables** and add: Name `API_PUBLIC_URL`, Value
     `https://catalystedge-yourname.duckdns.org` (your address, with `https://` and no `/` at the end).
   - Click **Deploy**. It takes 2–3 minutes.
4. The code lives on the branch `claude/adoring-dirac-ohiyp8`, not on `main`, so point Vercel at it:
   Project → **Settings** → **Environments** (or **Git**) → **Production** → **Branch Tracking** →
   set it to `claude/adoring-dirac-ohiyp8` → **Save**. Then **Deployments** → the latest one → **⋯**
   → **Redeploy**. (If the branch is later merged into `main`, you can switch this back.)
5. Copy your site address, for example `https://claude-code-yourname.vercel.app`.

## Step 5: Connect the two and restart, about 2 minutes

Back in the server window (PowerShell):
```bash
nano .env
```
Set `CORS_ORIGINS=` to your Vercel address, for example `CORS_ORIGINS=https://claude-code-yourname.vercel.app`.
Press **Ctrl+O**, **Enter**, **Ctrl+X**. Then:
```bash
docker compose -f docker-compose.cloud.yml up -d
```

## Step 6: Check that it really works on the live version

Run this on the server (replace both addresses with yours). It reads your password from the server's
`.env`, so you don't type it, and it never prints it:
```bash
docker compose -f docker-compose.cloud.yml exec api python -m catalystedge verify-deployment --url https://catalystedge-yourname.duckdns.org --web https://claude-code-yourname.vercel.app --email
```
It checks, one line each, `[PASS]`/`[FAIL]`/`[WAIT]`:

| Check | What it proves |
|---|---|
| API reachable over HTTPS, Database (Neon), Cloud profile | the server, HTTPS and Neon work |
| Scheduler running | the worker finished a scheduled task recently (heartbeat in the server's Redis) |
| Login, Password required | your password works and visitors without it are refused |
| Refresh | a full refresh (news → events → prices → signals → portfolio) ran on the worker |
| Web app points at this API | Vercel is connected to your server |
| Test email sent by the worker | one real email went out; check your inbox **and spam folder** |

It ends with **All checks passed.**

- **`[WAIT] Scheduler running`** right after the first start is normal. News polls run at least once an
  hour (every 15 minutes on weekdays 6:00–20:00 New York time). Run the check again after the next poll.
- **`[FAIL] Scheduler running: stale`**: see `docker compose -f docker-compose.cloud.yml logs --tail 50 beat worker`.
- **`[FAIL] Test email`** with a provider error: the Resend key is wrong, or `ALERT_EMAIL_TO` isn't the
  email you signed up to Resend with.
- **`[FAIL] Web app points at this API`**: fix `API_PUBLIC_URL` in Vercel (Settings → Environment
  Variables) and redeploy.

Finally, open your Vercel address on your phone or computer, log in with your password, and click
around. **Settings → Email send log** shows the test email as **sent**, and the **Send test email**
button there repeats the email check any time.

**Problem: the dashboard says "Failed to fetch" or keeps asking you to log in.** `CORS_ORIGINS` on
the server doesn't exactly match the Vercel address (it needs `https://` and no `/` at the end).
Fix it (step 5) and restart.

---

## What to expect once it's running

- **Paper money only.** Auto-buy is OFF. Signals appear on the dashboard and in emails; nothing is bought
  unless you click Buy in the paper account. Fills happen at the **next day's open**.
- **Catalysts start OFF or "unproven"** (the backtest's verdicts are loaded into a fresh database). A
  catalyst only turns ON after 50+ live signals beat the S&P 500 on the strict bar. That takes months.
- **Confidence is UNCALIBRATED**: don't read 80 as "80% likely".
- **Sources page:** expect ACTIVE for SEC, the newswires, FDA, halts, contracts, FINRA, Fed, BLS, Tiingo
  and Finnhub. FRED and SAM.gov show AUTH REQUIRED until you add their optional keys.
- **Outcomes** for a signal are filled in after its holding period (days), so the first real numbers
  appear after a week or two.

## Keeping it running

- **Updating to a newer version** (on the server):
  ```bash
  cd ~/catalystedge && git pull && docker compose -f docker-compose.cloud.yml up -d --build
  ```
  Database changes are applied automatically by the `migrate` step. Vercel updates itself when the
  branch changes.
- **Logs:** `docker compose -f docker-compose.cloud.yml logs --tail 100 worker`.
- **If Oracle restarts the server**, everything starts again by itself (`restart: unless-stopped`).
- **Your keys** live only in `~/catalystedge/.env` on the server (readable only by you) and in Vercel's
  settings. Never paste them anywhere else. After finishing, delete the Notepad file you used.
- **External alarm (optional):** a free UptimeRobot monitor on `https://…duckdns.org/health` emails you
  if the server stops answering. The server can't report its own outage.

## Free-tier budget notes

| Service | Limit | How CatalystEdge stays inside it |
|---|---|---|
| Neon compute | 100 compute-hours a month; sleeps after 5 idle minutes | short-lived connections; cloud polls every 15 min in market hours and hourly otherwise; filings, email and other runs happen on those same minutes. Measured 2026-10-01: a news run takes ~2 min and a filings run under 1 min, so the database is awake ~7 min of every 15 in market hours: about 230 hours a month at the smallest size (0.25), i.e. **~55–65 compute-hours**. If Neon offers a compute-size setting, keep it at the smallest. |
| Neon storage | 0.5 GB | news older than 48 hours is deleted daily; events, filings and outcomes are kept. Measured growth is about 1–1.5 MB a day, so **0.5 GB lasts roughly a year**. Neon's dashboard shows the size; when it passes ~400 MB, ask for a clean-up of old events. |
| Upstash | not used | measured 2026-10-01: ~290 shared-memory commands per news run and ~390 per filings run, ~775,000 a month, more than Upstash's free 500,000. The VM's own Redis does this job instead (no limit, kept on disk across restarts). |
| Oracle | idle machines on free accounts may be reclaimed | upgrade to Pay As You Go (3a), which stays $0 |
| Vercel Hobby | personal, non-commercial use | the dashboard is a small site with no paid features |
| Resend | 100 emails a day | the app caps itself at `EMAIL_DAILY_CAP=50` |

Not running in the cloud by design: **Ollama** (the optional local AI explanations need a
computer with spare memory; see the README) and the **backtest data download** (run it locally).

*Not financial advice. CatalystEdge is a research and paper-trading tool.*
