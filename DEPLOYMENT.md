# Deployment, Monitoring & Logging

How to run the bot on a server or cloud VM, with health checks, metrics, and logs. Read
`README.md`'s disclaimer first — **default to paper trading**, and treat "go live" as the
last step, not the first.

There are three deployment shapes; pick the one that matches how you trade:

| You trade on… | Deploy… | Where the strategy runs |
|---|---|---|
| An exchange, via this bot | **the paper/live bot** (`run_paper_trader.py`) | this Python process |
| 3Commas / Cryptohopper / MT5 (Python-driven) | **the signal dispatcher** (`dispatch_signal.py`, on a timer) | here; the platform executes |
| MetaTrader, natively | **the MQL Expert Advisor** (`mql/`) | inside the MT terminal on a VPS |

---

## 1. The paper/live bot (Docker — recommended)

The whole stack (bot + Prometheus + Grafana) comes up with one command.

```bash
# 1. Provide your config and (optional) secrets
cp config.example.yaml config.yaml          # edit strategy, symbol, timeframe
cp .env.example .env                        # optional; add exchange/platform keys

# 2. Bring up the stack
docker compose -f deploy/docker-compose.yml up -d

# 3. Check it
curl localhost:8000/healthz                 # bot health (JSON)
curl localhost:8000/metrics                 # Prometheus metrics
open http://localhost:3000                  # Grafana (admin / changeme)
docker compose -f deploy/docker-compose.yml logs -f tradebot
```

The bot container runs `run_paper_trader.py live` by default (fake money). Paper state and
audit logs persist in named volumes, so restarts resume where they left off.

### Run it on a cloud VM

Any small Linux VM works (AWS EC2 `t3.micro`, GCP `e2-small`, DigitalOcean/Hetzner
$5 droplet, Fly.io, Railway). The pattern is identical everywhere:

```bash
# on a fresh Ubuntu VM
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-plugin git
git clone <your-fork-url> tradebot && cd tradebot
cp config.example.yaml config.yaml && $EDITOR config.yaml
docker compose -f deploy/docker-compose.yml up -d
```

Lock down the firewall so only you can reach ports 3000/8000 (or don't publish them and
use an SSH tunnel: `ssh -L 3000:localhost:3000 -L 8000:localhost:8000 user@vm`).

---

## 2. The paper/live bot (systemd — no Docker)

For a bare VM without Docker:

```bash
sudo useradd -r -m -d /opt/tradebot tradebot
sudo -u tradebot git clone <your-fork-url> /opt/tradebot
cd /opt/tradebot
sudo -u tradebot python3 -m venv .venv && sudo -u tradebot .venv/bin/pip install -r requirements.txt
# put secrets in /opt/tradebot/.env  (chmod 600, owned by tradebot)

sudo cp deploy/systemd/tradebot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tradebot
journalctl -u tradebot -f
```

`systemd` restarts the bot on failure. Metrics/health are still served on `:8000`; point
an external Prometheus at it.

---

## 3. Driving a hosted platform (3Commas / Cryptohopper / MT5)

Here the strategy runs on your server but **orders execute on the platform**, which holds
your exchange keys. Run the dispatcher once per bar with a systemd timer:

```bash
sudo cp deploy/systemd/tradebot-dispatch.service /etc/systemd/system/
sudo cp deploy/systemd/tradebot-dispatch.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tradebot-dispatch.timer
systemctl list-timers tradebot-dispatch.timer     # confirm next run
```

Set the platform secrets in `/opt/tradebot/.env` (see `.env.example`). Test with a **dry
run first** — remove `--live` from the service's `ExecStart`, run it once
(`sudo systemctl start tradebot-dispatch.service`), and inspect
`journalctl -u tradebot-dispatch` to confirm the payload looks right before enabling live.

---

## 4. Native MetaTrader EA (no server needed)

For MT4/MT5, the portable path is the Expert Advisor in `mql/` running on a MetaTrader
**VPS** (most brokers offer one, or use any Windows VPS). No Python, no webhooks. Follow
`mql/README.md` to install, Strategy-Test, demo-forward-test, then attach to a live chart.

---

## Monitoring

The bot serves two endpoints (see `src/monitoring/health.py`):

- **`GET /healthz`** — JSON: `healthy`, uptime, seconds since last loop, equity, open
  position, trade/error counts. Returns HTTP 503 if the bot has gone silent past
  `max_silence_s`. Use it for container/orchestrator liveness probes.
- **`GET /metrics`** — Prometheus exposition format:
  `tradebot_up`, `tradebot_equity`, `tradebot_open_position`, `tradebot_trades_total`,
  `tradebot_errors_total`, `tradebot_loops_total` (all labeled by strategy + symbol).

### Suggested alerts (Prometheus/Alertmanager)

```yaml
groups:
  - name: tradebot
    rules:
      - alert: TradebotDown
        expr: tradebot_up == 0
        for: 2m
        annotations: { summary: "Trading bot is unhealthy or not scraping" }
      - alert: TradebotErrorsSpiking
        expr: increase(tradebot_errors_total[10m]) > 5
        for: 0m
        annotations: { summary: "Bot logging repeated errors" }
      - alert: TradebotEquityDrawdown
        expr: tradebot_equity < 8000       # set to your initial_cash * (1 - max drawdown)
        for: 5m
        annotations: { summary: "Equity below drawdown floor — investigate" }
```

Grafana: add Prometheus (`http://prometheus:9090`) as a data source and chart
`tradebot_equity` and `tradebot_open_position` over time.

---

## Logging

- **Console** — human-readable during development; set `monitoring.json_console: true` in
  `config.yaml` for JSON on stdout so a container log shipper (Loki, CloudWatch, ELK) can
  parse it.
- **`logs/paper.log`** — rolling JSON application log (one event per line).
- **`logs/paper_trades.jsonl`** — the **audit trail**: every decision and fill as a JSON
  line (`time`, `price`, `signal`, `in_position`, `equity`, and the `fill` when one
  happens). This is your source of truth for reconstructing what the bot did and when.

Ship these wherever you centralize logs. In Docker, both files are on the `tradebot_logs`
volume; with systemd, application logs also go to `journalctl -u tradebot`.

---

## Path to live (do not skip)

1. Green backtest across regimes — `python scripts/run_strategy_comparison.py`.
2. Walk-forward validation — `python scripts/validate.py --folds 6`.
3. Weeks of paper trading — `run_paper_trader.py live` — with monitoring you actually watch.
4. Only then, live, with the smallest size your venue allows, and the safety flags in
   `config.yaml` (`live.enabled`, `live.mode: live`, `live.confirm_live: true`) plus keys in
   the environment. The bot refuses to send real orders until all three flags are set.
