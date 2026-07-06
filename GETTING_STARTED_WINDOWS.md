# Getting started on Windows (beginner-friendly)

A step-by-step guide to run the Kalshi smoke test and paper trader on Windows, assuming
you've never used a terminal. Do one phase at a time. If anything errors, copy the red
text and send it back — errors are normal and fixable.

---

## Phase 1 — Install Python (once)

1. Go to <https://www.python.org/downloads/> and click the big **Download Python** button.
2. Run the installer. **IMPORTANT:** on the first screen, check the box
   **“Add python.exe to PATH”** at the bottom before clicking **Install Now**. (This one
   checkbox saves a lot of pain.)
3. When it finishes, click **Close**.

## Phase 2 — Install Git (once)

Git is how you download the code.

1. Go to <https://git-scm.com/download/win> — the download starts automatically.
2. Run the installer and click **Next** through all the screens (the defaults are fine),
   then **Install**, then **Finish**.

## Phase 3 — Open PowerShell

1. Click the **Start** menu, type **PowerShell**, and click **Windows PowerShell**.
2. A dark window opens with a blinking cursor. This is the terminal. You type a command
   and press **Enter** to run it.

Quick check that Python installed correctly — type this and press Enter:

```powershell
python --version
```

You should see something like `Python 3.12.x`. If instead it opens the Microsoft Store
or errors, restart your computer (so the PATH change takes effect) and try again.

## Phase 4 — Download the code

Copy-paste each line and press Enter after it. This puts the project in your user folder.

```powershell
cd $HOME
git clone https://github.com/dannypecu-star/tradebot.git
cd tradebot
git checkout claude/automated-trading-strategies-b8hj9n
```

`cd tradebot` moves you *into* the project folder. From now on, run commands from here.

## Phase 5 — Set up the project (once)

Create an isolated Python environment and install the libraries the project needs:

```powershell
python -m venv .venv
```

Now “activate” it. Windows blocks this by default, so run these two lines together:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
.\.venv\Scripts\Activate.ps1
```

Your prompt should now start with `(.venv)`. That means the environment is on. Install
the libraries:

```powershell
pip install -r requirements.txt
```

This downloads a few packages and takes a minute. Warnings in yellow are fine; watch for
errors in red.

> Every new time you open PowerShell to use this project, you only need:
> ```powershell
> cd $HOME\tradebot
> .\.venv\Scripts\Activate.ps1
> ```

## Phase 6 — Point it at your Kalshi key

You already have two things from Kalshi: an **API Key ID** (a long code) and a
**private key file** you downloaded (a file whose contents start with
`-----BEGIN ... PRIVATE KEY-----`).

1. Find that key file in File Explorer. **Hold Shift, right-click it, choose
   “Copy as path.”** That copies its full location (in quotes).
2. In PowerShell, set your two credentials for this session. Replace the placeholders —
   paste the copied path for the second one (keep the quotes):

```powershell
$env:KALSHI_KEY_ID="paste-your-key-id-here"
$env:KALSHI_PRIVATE_KEY_PATH="C:\Users\You\Downloads\your_key_file"
```

(These last only until you close the window — that's fine and safe. You'll re-enter them
each session, or we can automate it later.)

## Phase 7 — Run the smoke test

```powershell
python scripts\kalshi_smoke_test.py
```

**What you want to see:** three checks, each ending in `[PASS]`, and near the bottom your
**demo balance**. If step 3 (the signed request) passes, your key and signing are
correct and you're ready to paper trade.

**If you see `[FAIL]`:** copy the whole output and send it back. Common fixes:
- `MISSING` credentials → the `$env:` lines in Phase 6 didn't run in this same window.
- `401` on step 3 → the Key ID and the key file don't match, or the file path is wrong.
- a network error → your network is blocking Kalshi; try a different network.

## Phase 8 — First paper trade (dry run, sends nothing)

The smoke test prints some real market tickers. Pick one or two, make a JSON file with
your own fair-probability guesses, then dry-run:

```powershell
'{"PASTE-A-REAL-TICKER": 0.60}' | Out-File -Encoding utf8 my_probs.json
python scripts\kalshi_paper_trade.py --probs my_probs.json
```

This only **logs** the trades it would make — it sends nothing. When you're happy with
what it shows, add `--live` to place orders on the **demo** sandbox (fake money):

```powershell
python scripts\kalshi_paper_trade.py --probs my_probs.json --live
```

That's the milestone: real market plumbing, real orders, zero real money.
