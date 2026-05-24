# PRL Watch

`prl_watch.py` is a small helper for Pearl/AlphaPool.

What it does:

- fetches live PRL stats from AlphaPool
- estimates how much of supply is already emitted
- estimates gross/net daily returns from your hashrate
- monitors a PRL address through AlphaPool API
- sends Telegram alerts when your address gets a `finder=true` block
- can launch `alpha-miner` and auto-restart it on failure
- can stop mining if local GPU exceeds temperature or power limits

What it does not do:

- it does not implement a custom Pearl miner
- it does not reverse-engineer the closed AlphaPool stratum server
- it does not auto-download and auto-execute closed-source binaries for you

## Why this approach

AlphaPool explicitly says its pool server is internal and the integration spec for custom miners is not public yet.
That makes a fresh miner implementation unrealistic without reverse engineering.

So the safe/minimal route is:

1. run an existing miner
2. monitor your address via the documented pool API
3. notify Telegram when your address actually finds a block on the pool

## Safety notes

- Pearl protocol repo is public: `https://github.com/pearl-research-labs/pearl`
- AlphaPool `alpha-miner` source is private. Treat it as a trust tradeoff.
- Verify the published checksum before running the binary.
- Use a dedicated mining wallet and do not store meaningful funds on the same machine.
- For pool mining, your direct "winning block" event is exposed via `finder=true` in `GET /api/miner/<address>`.

## Requirements

- Python 3.10+
- Linux or WSL2 if you want to run `alpha-miner`
- optional Telegram bot token + chat id

No external Python packages are required.

## Verify alpha-miner first

Example for Linux / WSL2:

```bash
curl -L -o alpha-miner https://github.com/AlphaMine-Tech/alpha-miner/releases/latest/download/alpha-miner
chmod +x alpha-miner

curl -L https://github.com/AlphaMine-Tech/alpha-miner/releases/latest/download/SHA256SUMS | sha256sum -c
```

If checksum verification fails, stop there.

## Live stats

Print current network/pool stats:

```bash
python prl_watch.py stats
```

Estimate profitability for a specific rig:

```bash
python prl_watch.py stats \
  --hashrate-ths 150 \
  --price-usd 0.36 \
  --power-watts 350 \
  --electricity-usd-kwh 0.12
```

By default the estimate includes:

- pool fee from AlphaPool API
- extra miner fee `1%` for `alpha-miner`

If you use another client, override it:

```bash
python prl_watch.py stats --hashrate-ths 150 --miner-fee-percent 0
```

## Monitor only

If your miner is already running somewhere else, just monitor the address:

```bash
python prl_watch.py monitor \
  --address prl1pYOURADDRESS \
  --telegram-bot-token 123456:ABCDEF \
  --telegram-chat-id 123456789
```

Default behavior on first run:

- current blocks are seeded as already seen
- old historical blocks are not replayed into Telegram
- only new future events will alert

To also notify on any pool block where your address got a share, not only direct finder blocks:

```bash
python prl_watch.py monitor \
  --address prl1pYOURADDRESS \
  --telegram-bot-token 123456:ABCDEF \
  --telegram-chat-id 123456789 \
  --notify-contributed
```

## Run miner + monitor (single rig)

Example:

```bash
python prl_watch.py run \
  --miner-binary ./alpha-miner \
  --address prl1pYOURADDRESS \
  --worker rig01 \
  --pool stratum+tcp://us2.alphapool.tech:5566 \
  --password 'x;d=65536' \
  --telegram-bot-token 123456:ABCDEF \
  --telegram-chat-id 123456789
```

Optional flags:

- `--devices 0,1,2`
- `--status-interval 60`
- `--force-backend blackwell-native` (RTX 5090) or `--force-backend ada` (RTX 4090)
- `--extra-arg=--debug-logs`
- `--no-restart`
- `--max-temp-c 78`
- `--max-power-w 450`
- `--gpu-report-interval 60`
- `--gpu-check-interval 5`
- `--notify-workers` / `--no-notify-workers` (alert when a worker joins; on by default)
- `--notify-offline` / `--no-notify-offline` (alert when a worker drops; on by default)

## Multi-server: one hub, many rigs

When you have many mining hosts, run **one** central control process and let the rigs just mine.

Architecture:

- **Hub** — a laptop / VPS / home server runs `prl_watch.py hub` (or `bot` + `monitor`). It polls the
  AlphaPool API for your address, answers Telegram commands, and fires alerts on new/offline workers,
  finder blocks, and contributed blocks. Only the hub needs `TG_BOT_TOKEN` and `TG_CHAT_ID`.
- **Rigs** — each mining host runs `prl_watch.py run` with `--worker $(hostname)` and **no** Telegram
  credentials. The rig submits shares to AlphaPool; AlphaPool reports the worker to the hub. The hub
  detects "rig joined" / "rig offline" automatically — no SSH, no message bus, no extra plumbing.

### Hub setup (your laptop or VPS)

```bash
cp .env.example .env
$EDITOR .env           # fill PRL_ADDRESS, TG_BOT_TOKEN, TG_CHAT_ID, optionally PRL_PRICE_USD

python prl_watch.py hub
```

The hub fires a Telegram message every time a new worker name first appears on the address.

### Per-rig setup

On every mining server (no Telegram credentials needed):

```bash
export PRL_ADDRESS=prl1pYOURADDRESS
python prl_watch.py run \
  --miner-binary ./alpha-miner \
  --pool stratum+tcp://us2.alphapool.tech:5566 \
  --force-backend blackwell-native
```

`--worker` defaults to the machine hostname, so each rig is named uniquely without
config. Restart-on-crash and GPU temperature/power guards are on automatically.

### Telegram commands (hub or any bot instance)

| Command | What it does |
| ------- | ------------ |
| `/status` | overall address summary (workers, hashrate, balance) |
| `/servers` | per-server hashrate breakdown (alias of `/workers`) |
| `/workers` | full worker list with live / 1h / 24h hashrate |
| `/earnings` | balance, paid out, total earned, live PRL/day estimate (and USD if `PRL_PRICE_USD` set) |
| `/blocks` | recent blocks where your address contributed, with finder flag |
| `/devices` | local GPU telemetry on the host running the bot |
| `/help` | command reference |

### Solo mining

AlphaPool supports solo through port `5567` (separate from pool's `5566`). Set
`PRL_POOL=stratum+tcp://us2.alphapool.tech:5567` on the rigs. Solo only makes sense if your aggregate
hashrate is a meaningful fraction of the network — see "Pool vs solo" below.

### Pool vs solo with N x RTX 5090

Network hashrate at writing: ~13.9 EH/s, block reward ~2705 PRL, average network block ~3.2 min.
A single RTX 5090 on alpha-miner is ~0.6 - 0.8 TH/s (extrapolated from current top miners on AlphaPool).

- < 100 GPUs -> pool. Daily PRL: hashrate_TH x 0.165 PRL x (1 - 0.06 fees). Variance is acceptable.
- 100 - 1000 GPUs -> still pool. Solo block expectancy >= 6 days at 1000 GPUs; the variance vs the 5% fee
  on pool is a bad trade unless you can absorb multi-week droughts.
- > 1000 GPUs -> solo (port 5567) becomes viable, expected block every ~6 days at 1000 GPUs and
  shrinking sublinearly. You still pay AlphaPool's 5% solo fee but keep the full reward when you win.

The economics depend on PRL/USD. Run `python prl_watch.py stats --hashrate-ths 70 --price-usd 0.36
--power-watts 40000 --electricity-usd-kwh 0.10` (100 x 5090 example) to recompute live.

## Local device test profile

For a first local test, prefer conservative limits and telemetry output:

```bash
python prl_watch.py run \
  --miner-binary ./alpha-miner \
  --address prl1pYOURADDRESS \
  --worker local-test \
  --pool stratum+tcp://eu1.alphapool.tech:5566 \
  --status-interval 60 \
  --max-temp-c 78 \
  --max-power-w 200 \
  --gpu-report-interval 60 \
  --gpu-check-interval 5
```

If your local GPU stays cool and stable, then tune the power limit upward later.

## Environment variables

You can set:

- `PRL_ADDRESS`
- `TG_BOT_TOKEN`
- `TG_CHAT_ID`

Then the command becomes shorter:

```bash
export PRL_ADDRESS=prl1pYOURADDRESS
export TG_BOT_TOKEN=123456:ABCDEF
export TG_CHAT_ID=123456789

python prl_watch.py monitor
```

## State file

The script stores seen block hashes in:

```text
.prl-watch-state.json
```

Override it with:

```bash
python prl_watch.py monitor --state-file ~/.config/prl-watch/state.json
```

## API endpoints used

The script relies on AlphaPool's public API visible in the pool frontend:

- `GET /api/stats`
- `GET /api/miner/<address>`

Useful extras for manual inspection:

- `GET /api/blocks?limit=100`
- `GET /api/miners?limit=50`
- `GET /api/charts`


## Secrets / .env

`.env` is gitignored. Do **not** commit your `TG_BOT_TOKEN` or wallet seed.

- Telegram automatically revokes bot tokens detected in public Git pushes (GitHub
  partners with messaging providers for token-scanning), so a leaked token is gone
  within minutes anyway — and you'll lose access to the bot.
- Your `PRL_ADDRESS` is fine to share publicly (it's just a deposit address); but
  pairing it with worker hostnames in a public repo doxes your fleet.

Workflow:

```bash
cp .env.example .env
$EDITOR .env
# verify .env is ignored:
git check-ignore -v .env
```

If you really want a one-file deploy onto fresh rigs, prefer a small bootstrap
that pulls `.env` from your own private object store (S3 / Backblaze / private
gist), or set the variables in the rig's systemd unit / Windows service config,
rather than committing them.
