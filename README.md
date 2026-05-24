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

## Run miner + monitor

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
- `--force-backend ampere`
- `--extra-arg=--debug-logs`
- `--no-restart`
- `--max-temp-c 78`
- `--max-power-w 200`
- `--gpu-report-interval 60`
- `--gpu-check-interval 5`

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
