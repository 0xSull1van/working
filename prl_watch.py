#!/usr/bin/env python3
import argparse
import csv
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib import parse, request


API_BASE = "https://pearl.alphapool.tech/api"
TELEGRAM_API = "https://api.telegram.org"
TOTAL_SUPPLY_PRL = 2_100_000_000
EMISSION_HALF_HEIGHT = 650_226
DEFAULT_STATE_FILE = ".prl-watch-state.json"
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_POLL_INTERVAL_SECONDS = 30
DEFAULT_RESTART_DELAY_SECONDS = 15
DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS = 30
DEFAULT_POOL_URL = "stratum+tcp://us2.alphapool.tech:5566"
DEFAULT_SOLO_POOL_URL = "stratum+tcp://us2.alphapool.tech:5567"
DEFAULT_DOTENV_FILE = ".env"
MAX_TRACKED_BLOCKS = 512
MAX_TRACKED_WORKERS = 2048
SCRIPT_DIR = Path(__file__).resolve().parent
MINER_ENV_KEYS_TO_DROP = {
    "PRL_ADDRESS",
    "PRL_MINER_BINARY",
    "PRL_WORKER",
    "PRL_POOL",
    "PRL_DEVICES",
    "PRL_FORCE_BACKEND",
    "PRL_PASSWORD",
    "PRL_STATUS_INTERVAL",
    "PRL_POLL_INTERVAL",
    "PRL_STATE_FILE",
    "PRL_MAX_TEMP_C",
    "PRL_MAX_POWER_W",
    "PRL_GPU_REPORT_INTERVAL",
    "PRL_GPU_CHECK_INTERVAL",
    "PRL_NOTIFY_CONTRIBUTED",
    "PRL_RESTART_DELAY",
    "TG_BOT_TOKEN",
    "TG_CHAT_ID",
    "TG_STATUS_INTERVAL_MINUTES",
    "PRL_NOTIFY_WORKERS",
    "PRL_NOTIFY_OFFLINE",
    "PRL_PRICE_USD",
    "PRL_MINER_FEE_PERCENT",
}
DOTENV_KEYS = set(MINER_ENV_KEYS_TO_DROP)
SAFE_MINER_ENV_KEYS = {
    "PATH",
    "HOME",
    "LANG",
    "TERM",
    "SHELL",
    "USER",
    "LOGNAME",
    "TMP",
    "TEMP",
    "TMPDIR",
    "LD_LIBRARY_PATH",
    "LIBRARY_PATH",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "SystemRoot",
    "WINDIR",
    "PATHEXT",
    "ComSpec",
}
SAFE_MINER_ENV_PREFIXES = (
    "LC_",
    "CUDA_",
    "NVIDIA_",
)
GPU_QUERY_FIELDS = [
    "index",
    "name",
    "temperature.gpu",
    "power.draw",
    "power.limit",
    "utilization.gpu",
    "fan.speed",
]


UNIT_TO_HPS = {
    "H/S": 1.0,
    "KH/S": 1e3,
    "MH/S": 1e6,
    "GH/S": 1e9,
    "TH/S": 1e12,
    "PH/S": 1e15,
    "EH/S": 1e18,
}


def env_or(value: str | None, env_name: str) -> str | None:
    return value if value is not None else os.environ.get(env_name)


def default_worker_name() -> str:
    hostname = socket.gethostname().strip().lower()
    if not hostname:
        return "miner-node"
    return hostname.replace(" ", "-")


def parse_bool(value: Any) -> bool:
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"unsupported boolean value: {value!r}")


def strip_optional_quotes(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(f"dotenv unreadable: {path}: {exc}") from exc

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            raise RuntimeError(f"invalid dotenv line {line_number} in {path}: {raw_line!r}")

        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise RuntimeError(f"invalid dotenv key on line {line_number} in {path}")
        if key not in DOTENV_KEYS:
            continue
        os.environ.setdefault(key, strip_optional_quotes(raw_value))


def build_miner_process_env() -> dict[str, str]:
    child_env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in MINER_ENV_KEYS_TO_DROP:
            continue
        if key in SAFE_MINER_ENV_KEYS or key.startswith(SAFE_MINER_ENV_PREFIXES):
            child_env[key] = value
    return child_env


def read_json_url(url: str, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> Any:
    req = request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "prl-watch/1.0",
        },
    )
    with request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def post_form(url: str, data: dict[str, Any], timeout: int = DEFAULT_TIMEOUT_SECONDS) -> Any:
    encoded = parse.urlencode(data).encode("utf-8")
    req = request.Request(
        url,
        data=encoded,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "prl-watch/1.0",
        },
        method="POST",
    )
    with request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def send_telegram(bot_token: str, chat_id: str, text: str) -> None:
    url = f"{TELEGRAM_API}/bot{bot_token}/sendMessage"
    response = post_form(url, {"chat_id": chat_id, "text": text})
    if not response.get("ok"):
        raise RuntimeError(f"telegram send failed: {response}")


def get_telegram_updates(
    bot_token: str,
    offset: int | None = None,
    timeout: int = DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    url = f"{TELEGRAM_API}/bot{bot_token}/getUpdates?{parse.urlencode(params)}"
    response = read_json_url(url, timeout=timeout + 5)
    if not response.get("ok"):
        raise RuntimeError(f"telegram getUpdates failed: {response}")
    return list(response.get("result", []))


def parse_hashrate_to_hps(text: str) -> float:
    parts = str(text).strip().upper().split()
    if len(parts) != 2:
        raise ValueError(f"unsupported hashrate format: {text!r}")
    value = float(parts[0].replace(",", ""))
    unit = parts[1]
    if unit not in UNIT_TO_HPS:
        raise ValueError(f"unsupported hashrate unit: {unit!r}")
    return value * UNIT_TO_HPS[unit]


def fmt_pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def fmt_prl(value: float) -> str:
    return f"{value:,.8f} PRL"


def fmt_usd(value: float) -> str:
    return f"${value:,.2f}"


def parse_optional_float(text: str) -> float | None:
    value = str(text).strip()
    if not value or value.upper() in {"N/A", "[N/A]", "NOT SUPPORTED", "[NOT SUPPORTED]"}:
        return None
    return float(value)


def parse_unix_timestamp(value: Any) -> str:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return "n/a"
    if timestamp <= 0:
        return "never"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


def block_status(block: dict[str, Any]) -> str:
    if block.get("orphaned"):
        return "orphaned"
    if block.get("paid_out"):
        return "paid"
    if block.get("confirmed"):
        return "confirmed"
    return "pending"


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"finder_blocks": [], "contributed_blocks": [], "seen_workers": {}}

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"state file unreadable: {path}: {exc}") from exc

    state: dict[str, Any] = {
        "finder_blocks": list(raw.get("finder_blocks", [])),
        "contributed_blocks": list(raw.get("contributed_blocks", [])),
        "seen_workers": dict(raw.get("seen_workers", {})),
    }
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def remember(state: dict[str, Any], bucket: str, value: str) -> bool:
    items = state.setdefault(bucket, [])
    if value in items:
        return False
    items.append(value)
    if len(items) > MAX_TRACKED_BLOCKS:
        del items[:-MAX_TRACKED_BLOCKS]
    return True


def get_pool_stats() -> dict[str, Any]:
    return read_json_url(f"{API_BASE}/stats")


def get_miner_stats(address: str) -> dict[str, Any]:
    encoded = parse.quote(address, safe="")
    return read_json_url(f"{API_BASE}/miner/{encoded}")


def get_worker_name(worker: dict[str, Any]) -> str | None:
    for key in ("worker", "name", "id"):
        value = worker.get(key)
        if value:
            return str(value)
    return None


def try_parse_hashrate_to_hps(text: Any) -> float | None:
    if text in (None, ""):
        return None
    try:
        return parse_hashrate_to_hps(str(text))
    except ValueError:
        return None


def fmt_hashrate_hps(value: float) -> str:
    if value <= 0:
        return "0 H/s"

    units = [
        ("EH/s", 1e18),
        ("PH/s", 1e15),
        ("TH/s", 1e12),
        ("GH/s", 1e9),
        ("MH/s", 1e6),
        ("KH/s", 1e3),
    ]
    for unit, threshold in units:
        if value >= threshold:
            return f"{value / threshold:,.2f} {unit}"
    return f"{value:.2f} H/s"


def fmt_count(value: Any) -> str:
    return f"{int(value):,}"


def fmt_difficulty(value: Any) -> str:
    if value in (None, ""):
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:,.0f}" if number.is_integer() else f"{number:,.2f}"


def format_pairs(rows: list[tuple[str, str]], indent: str = "") -> list[str]:
    if not rows:
        return []
    width = max(len(label) for label, _value in rows)
    return [f"{indent}{label:<{width}}  {value}" for label, value in rows]


def append_pair_section(lines: list[str], title: str, rows: list[tuple[str, str]]) -> None:
    if not rows:
        return
    if lines:
        lines.append("")
    lines.append(title)
    lines.extend(format_pairs(rows))


def append_text_section(lines: list[str], title: str, rows: list[str]) -> None:
    if not rows:
        return
    if lines:
        lines.append("")
    lines.append(title)
    lines.extend(rows)


def get_worker_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return list(payload.get("workers", []))


def get_sorted_workers(payload: dict[str, Any]) -> list[dict[str, Any]]:
    workers = get_worker_rows(payload)
    return sorted(
        workers,
        key=lambda worker: (
            not bool(worker.get("online")),
            str(get_worker_name(worker) or "").lower(),
        ),
    )


def format_worker_brief(worker: dict[str, Any], index: int) -> list[str]:
    name = get_worker_name(worker) or f"worker-{index}"
    status = "online" if worker.get("online") else "offline"
    lines = [f"{index}. {name}"]
    lines.extend(
        format_pairs(
            [
                ("status", status),
                ("live", str(worker.get("hashrate_live", "0 H/s"))),
                ("1h", str(worker.get("hashrate_1h", worker.get("hashrate1h", "0 H/s")))),
                ("24h", str(worker.get("hashrate", worker.get("hashrate_24h", "0 H/s")))),
                ("difficulty", fmt_difficulty(worker.get("difficulty"))),
                ("last share", parse_unix_timestamp(worker.get("time"))),
            ],
            indent="  ",
        )
    )
    return lines


def derive_address_metrics(payload: dict[str, Any], local_workers: set[str] | None = None) -> dict[str, Any]:
    workers = payload.get("workers", [])
    blocks = payload.get("blocks", [])
    payments = payload.get("payments", [])
    worker_names = {name for worker in workers if (name := get_worker_name(worker))}
    workers_online = sum(1 for worker in workers if worker.get("online"))
    live_hashrate_hps = sum(
        parsed for worker in workers if (parsed := try_parse_hashrate_to_hps(worker.get("hashrate_live"))) is not None
    )
    contributed_reward_prl = sum(float(block.get("my_share_grain", 0) or 0.0) / 1e8 for block in blocks)
    pending_reward_prl = sum(
        float(payment.get("amount_grain", 0) or 0.0) / 1e8
        for payment in payments
        if str(payment.get("status", "")).lower() != "paid"
    )
    latest_block = max(blocks, key=lambda block: int(block.get("height", 0) or 0), default=None)

    return {
        "workers_connected": len(worker_names) if worker_names else len(workers),
        "workers_online": workers_online,
        "workers_configured": len(local_workers or set()),
        "live_hashrate_text": fmt_hashrate_hps(live_hashrate_hps),
        "hashrate_1h": str(payload.get("estHash1h", "0 H/s")),
        "hashrate_24h": str(payload.get("estHash24h", "0 H/s")),
        "blocks_with_shares": len(blocks),
        "finder_blocks": sum(1 for block in blocks if block.get("finder")),
        "reward_entries": int(payload.get("payments_count", len(payments)) or 0),
        "shares_24h": int(payload.get("shares24h", 0) or 0),
        "paid_prl": float(payload.get("total_paid_prl", 0.0) or 0.0),
        "balance_prl": float(payload.get("balance_prl", 0.0) or 0.0),
        "total_earned_prl": float(payload.get("total_paid_prl", 0.0) or 0.0)
        + float(payload.get("balance_prl", 0.0) or 0.0),
        "pending_reward_prl": pending_reward_prl,
        "contributed_reward_prl": contributed_reward_prl,
        "last_seen": parse_unix_timestamp(payload.get("last_seen")),
        "mode": str(payload.get("mode", "n/a")),
        "latest_block_height": str(latest_block.get("height", "n/a")) if latest_block else "n/a",
        "latest_block_status": block_status(latest_block) if latest_block else "n/a",
    }


def format_status_message(
    address: str,
    payload: dict[str, Any],
    header: str,
    local_workers: set[str] | None = None,
) -> str:
    metrics = derive_address_metrics(payload, local_workers=local_workers)
    lines = [header]
    append_pair_section(
        lines,
        "Overview",
        [
            ("address", address),
            ("mode", metrics["mode"]),
            ("last seen", metrics["last_seen"]),
        ],
    )
    append_pair_section(
        lines,
        "Workers",
        [
            ("connected", fmt_count(metrics["workers_connected"])),
            ("online", fmt_count(metrics["workers_online"])),
            ("configured", fmt_count(metrics["workers_configured"])),
        ],
    )
    append_pair_section(
        lines,
        "Hashrate",
        [
            ("live", metrics["live_hashrate_text"]),
            ("1h", metrics["hashrate_1h"]),
            ("24h", metrics["hashrate_24h"]),
        ],
    )
    append_pair_section(
        lines,
        "Rewards",
        [
            ("pending balance", fmt_prl(metrics["balance_prl"])),
            ("pending entries", fmt_prl(metrics["pending_reward_prl"])),
            ("paid out", fmt_prl(metrics["paid_prl"])),
            ("total earned", fmt_prl(metrics["total_earned_prl"])),
        ],
    )
    append_pair_section(
        lines,
        "Blocks",
        [
            ("with shares", fmt_count(metrics["blocks_with_shares"])),
            ("direct finder", fmt_count(metrics["finder_blocks"])),
            ("your reward", fmt_prl(metrics["contributed_reward_prl"])),
            ("shares 24h", fmt_count(metrics["shares_24h"])),
            ("reward entries", fmt_count(metrics["reward_entries"])),
            ("latest height", metrics["latest_block_height"]),
            ("latest status", metrics["latest_block_status"]),
        ],
    )
    return "\n".join(lines)


def format_workers_message(
    address: str,
    payload: dict[str, Any],
    header: str = "PRL workers",
    local_workers: set[str] | None = None,
) -> str:
    metrics = derive_address_metrics(payload, local_workers=local_workers)
    workers = get_sorted_workers(payload)
    lines = [header]
    append_pair_section(
        lines,
        "Summary",
        [
            ("address", address),
            ("connected", fmt_count(metrics["workers_connected"])),
            ("online", fmt_count(metrics["workers_online"])),
            ("configured", fmt_count(metrics["workers_configured"])),
            ("live hashrate", metrics["live_hashrate_text"]),
        ],
    )
    worker_lines: list[str] = []
    for index, worker in enumerate(workers, start=1):
        worker_lines.extend(format_worker_brief(worker, index))
        if index != len(workers):
            worker_lines.append("")
    if not worker_lines:
        worker_lines.append("No workers reported by AlphaPool yet.")
    append_text_section(lines, "Worker list", worker_lines)
    return "\n".join(lines)


def format_blocks_message(address: str, payload: dict[str, Any], header: str = "PRL blocks") -> str:
    metrics = derive_address_metrics(payload)
    blocks = sorted(
        list(payload.get("blocks", [])),
        key=lambda block: int(block.get("height", 0) or 0),
        reverse=True,
    )
    lines = [header]
    append_pair_section(
        lines,
        "Summary",
        [
            ("address", address),
            ("with shares", fmt_count(metrics["blocks_with_shares"])),
            ("direct finder", fmt_count(metrics["finder_blocks"])),
            ("your reward", fmt_prl(metrics["contributed_reward_prl"])),
            ("pending balance", fmt_prl(metrics["balance_prl"])),
        ],
    )
    recent_lines: list[str] = []
    for block in blocks[:5]:
        recent_lines.extend(
            format_pairs(
                [
                    ("height", str(block.get("height", "n/a"))),
                    ("status", block_status(block)),
                    ("finder", "yes" if block.get("finder") else "no"),
                    ("share", fmt_prl(float(block.get("my_share_grain", 0) or 0.0) / 1e8)),
                    ("reward", fmt_prl(float(block.get("reward_prl", 0.0) or 0.0))),
                    ("time", parse_unix_timestamp(block.get("ts"))),
                ],
                indent="  ",
            )
        )
        recent_lines.append("")
    if recent_lines and recent_lines[-1] == "":
        recent_lines.pop()
    if not recent_lines:
        recent_lines.append("No blocks with shares yet.")
    append_text_section(lines, "Recent blocks", recent_lines)
    return "\n".join(lines)


def format_devices_message(gpus: list[dict[str, Any]], header: str = "Local devices") -> str:
    lines = [header]
    append_pair_section(lines, "Summary", [("detected gpus", fmt_count(len(gpus)))])
    device_lines: list[str] = []
    for gpu in gpus:
        device_lines.extend(
            [
                f"gpu{gpu['index']}  {gpu['name']}",
                *format_pairs(
                    [
                        (
                            "temperature",
                            "n/a" if gpu["temperature_c"] is None else f"{gpu['temperature_c']:.0f}C",
                        ),
                        (
                            "power",
                            "n/a"
                            if gpu["power_draw_w"] is None
                            else f"{gpu['power_draw_w']:.0f}W / "
                            + ("n/a" if gpu["power_limit_w"] is None else f"{gpu['power_limit_w']:.0f}W"),
                        ),
                        (
                            "utilization",
                            "n/a" if gpu["utilization_pct"] is None else f"{gpu['utilization_pct']:.0f}%",
                        ),
                        ("fan", "n/a" if gpu["fan_pct"] is None else f"{gpu['fan_pct']:.0f}%"),
                    ],
                    indent="  ",
                ),
                "",
            ]
        )
    if device_lines and device_lines[-1] == "":
        device_lines.pop()
    if not device_lines:
        device_lines.append("No GPUs detected.")
    append_text_section(lines, "Device list", device_lines)
    return "\n".join(lines)


def format_worker_event_message(
    title: str,
    name: str,
    worker: dict[str, Any],
    payload: dict[str, Any],
    is_first: bool,
) -> str:
    metrics = derive_address_metrics(payload)
    lines = [title]
    rows: list[tuple[str, str]] = [
        ("worker", name),
        ("status", "online" if worker.get("online") else "offline"),
        ("live", str(worker.get("hashrate_live", "0 H/s"))),
        ("1h", str(worker.get("hashrate_1h", worker.get("hashrate1h", "0 H/s")))),
        ("difficulty", fmt_difficulty(worker.get("difficulty"))),
        ("last share", parse_unix_timestamp(worker.get("time"))),
    ]
    if is_first:
        rows.append(("note", "first time seen on this address"))
    append_pair_section(lines, "Worker", rows)
    append_pair_section(
        lines,
        "Address state",
        [
            ("workers online", fmt_count(metrics["workers_online"])),
            ("live hashrate", metrics["live_hashrate_text"]),
        ],
    )
    return "\n".join(lines)


def format_earnings_message(
    address: str,
    miner_payload: dict[str, Any],
    pool_stats: dict[str, Any] | None = None,
    price_usd: float | None = None,
    miner_fee_percent: float = 1.0,
) -> str:
    metrics = derive_address_metrics(miner_payload)
    live_hps = sum(
        parsed
        for worker in miner_payload.get("workers", [])
        if (parsed := try_parse_hashrate_to_hps(worker.get("hashrate_live"))) is not None
    )
    lines = ["PRL earnings"]
    rows: list[tuple[str, str]] = [
        ("address", address),
        ("pending balance", fmt_prl(metrics["balance_prl"])),
        ("paid out", fmt_prl(metrics["paid_prl"])),
        ("total earned", fmt_prl(metrics["total_earned_prl"])),
    ]
    if price_usd and price_usd > 0:
        rows.append(("total earned USD", fmt_usd(metrics["total_earned_prl"] * price_usd)))
    append_pair_section(lines, "Balance", rows)

    if pool_stats and live_hps > 0:
        try:
            pool_metrics = derive_live_metrics(pool_stats)
            total_fee = (pool_metrics["pool_fee_percent"] + miner_fee_percent) / 100.0
            daily_prl_gross = (live_hps / 1e12) * pool_metrics["prl_per_day_per_th"]
            daily_prl_net = daily_prl_gross * (1.0 - total_fee)
            estimate_rows: list[tuple[str, str]] = [
                ("live hashrate", metrics["live_hashrate_text"]),
                ("gross PRL/day", f"{daily_prl_gross:.4f} PRL"),
                ("net PRL/day", f"{daily_prl_net:.4f} PRL"),
                ("pool+miner fees", fmt_pct(total_fee)),
            ]
            if price_usd and price_usd > 0:
                estimate_rows.append(("net USD/day", fmt_usd(daily_prl_net * price_usd)))
            append_pair_section(lines, "Live estimate", estimate_rows)
        except (KeyError, ValueError, ZeroDivisionError) as exc:
            append_text_section(lines, "Live estimate", [f"unavailable: {exc}"])
    return "\n".join(lines)


def format_help_message() -> str:
    lines = ["PRL bot commands"]
    append_text_section(
        lines,
        "Available commands",
        [
            "/status    overall address summary",
            "/servers   per-server hashrate breakdown (alias of /workers)",
            "/workers   pool workers and hashrate per worker",
            "/earnings  pending balance, paid out, live PRL/day estimate",
            "/blocks    recent blocks, rewards, balances",
            "/devices   local GPU telemetry on the host running the bot",
            "/help      command reference",
        ],
    )
    return "\n".join(lines)


def derive_live_metrics(stats: dict[str, Any]) -> dict[str, Any]:
    chain = stats["chain"]
    coin = stats["coins"][0]
    pool = stats["pool"]

    height = int(chain["height"])
    reward_prl = float(coin["reward"])
    network_hps = parse_hashrate_to_hps(coin["network_hash"])
    pool_hps = parse_hashrate_to_hps(pool["hashrate"])
    pool_share = pool_hps / network_hps if network_hps else 0.0
    network_blocks_per_day = pool["blocks24h"] / pool_share if pool_share else 0.0
    observed_block_seconds = 86_400 / network_blocks_per_day if network_blocks_per_day else 0.0
    mined_fraction = height / (height + EMISSION_HALF_HEIGHT)
    remaining_fraction = 1.0 - mined_fraction
    emitted_supply_prl = TOTAL_SUPPLY_PRL * mined_fraction
    theoretical_first_reward_prl = TOTAL_SUPPLY_PRL / EMISSION_HALF_HEIGHT
    prl_per_day = reward_prl * network_blocks_per_day
    prl_per_day_per_th = prl_per_day / (network_hps / 1e12) if network_hps else 0.0

    return {
        "height": height,
        "difficulty": float(chain["difficulty"]),
        "reward_prl": reward_prl,
        "pool_fee_percent": float(stats.get("feePercent", 0.0)),
        "pool_hashrate_text": pool["hashrate"],
        "network_hashrate_text": coin["network_hash"],
        "pool_share": pool_share,
        "pool_blocks_24h": int(pool["blocks24h"]),
        "network_blocks_per_day": network_blocks_per_day,
        "observed_block_seconds": observed_block_seconds,
        "miners_24h": int(pool["miners24h"]),
        "workers": int(pool["workers"]),
        "mined_fraction": mined_fraction,
        "remaining_fraction": remaining_fraction,
        "emitted_supply_prl": emitted_supply_prl,
        "remaining_supply_prl": TOTAL_SUPPLY_PRL - emitted_supply_prl,
        "theoretical_first_reward_prl": theoretical_first_reward_prl,
        "reward_decay_fraction": reward_prl / theoretical_first_reward_prl,
        "prl_per_day": prl_per_day,
        "prl_per_day_per_th": prl_per_day_per_th,
    }


def print_live_stats(metrics: dict[str, Any]) -> None:
    print(f"Chain height:             {metrics['height']}")
    print(f"Current difficulty:       {metrics['difficulty']:.8f}")
    print(f"Current block reward:     {fmt_prl(metrics['reward_prl'])}")
    print(f"Pool hashrate:            {metrics['pool_hashrate_text']}")
    print(f"Network hashrate:         {metrics['network_hashrate_text']}")
    print(f"Pool share of network:    {fmt_pct(metrics['pool_share'])}")
    print(f"Pool blocks (24h):        {metrics['pool_blocks_24h']}")
    print(f"Observed network blocks:  {metrics['network_blocks_per_day']:.2f} / day")
    print(f"Observed block interval:  {metrics['observed_block_seconds']:.2f} sec")
    print(f"Active miners (24h):      {metrics['miners_24h']}")
    print(f"Active workers:           {metrics['workers']}")
    print(f"Estimated mined supply:   {fmt_pct(metrics['mined_fraction'])}")
    print(f"Estimated remaining:      {fmt_pct(metrics['remaining_fraction'])}")
    print(f"Estimated emitted supply: {metrics['emitted_supply_prl']:,.2f} PRL")
    print(f"Estimated remaining PRL:  {metrics['remaining_supply_prl']:,.2f} PRL")
    print(f"Reward vs genesis:        {fmt_pct(metrics['reward_decay_fraction'])}")
    print(f"Observed emission/day:    {metrics['prl_per_day']:,.2f} PRL")
    print(f"Observed PRL/day/TH:      {metrics['prl_per_day_per_th']:.6f} PRL")


def print_profit_estimate(
    metrics: dict[str, Any],
    hashrate_ths: float,
    price_usd: float,
    power_watts: float | None,
    electricity_usd_kwh: float | None,
    extra_fee_percent: float,
) -> None:
    total_fee_fraction = (metrics["pool_fee_percent"] + extra_fee_percent) / 100.0
    gross_prl_day = hashrate_ths * metrics["prl_per_day_per_th"]
    net_prl_day = gross_prl_day * (1.0 - total_fee_fraction)
    gross_usd_day = gross_prl_day * price_usd
    net_usd_day = net_prl_day * price_usd

    print("")
    print("Estimate")
    print(f"Hashrate:                 {hashrate_ths:.2f} TH/s")
    print(f"PRL price:                {fmt_usd(price_usd)}")
    print(f"Pool + miner fees:        {fmt_pct(total_fee_fraction)}")
    print(f"Gross PRL/day:            {gross_prl_day:.6f} PRL")
    print(f"Net PRL/day:              {net_prl_day:.6f} PRL")
    print(f"Gross USD/day:            {fmt_usd(gross_usd_day)}")
    print(f"Net USD/day before power: {fmt_usd(net_usd_day)}")

    if power_watts is not None and electricity_usd_kwh is not None:
        power_cost_day = (power_watts / 1000.0) * 24.0 * electricity_usd_kwh
        print(f"Power draw:               {power_watts:.0f} W")
        print(f"Electricity:              {fmt_usd(electricity_usd_kwh)} / kWh")
        print(f"Power cost/day:           {fmt_usd(power_cost_day)}")
        print(f"Net USD/day after power:  {fmt_usd(net_usd_day - power_cost_day)}")


def tee_stream(stream: Any, prefix: str) -> None:
    try:
        for line in iter(stream.readline, ""):
            text = line.rstrip("\n")
            if text:
                print(f"[{prefix}] {text}")
    finally:
        stream.close()


def query_local_gpus() -> list[dict[str, Any]]:
    cmd = [
        "nvidia-smi",
        f"--query-gpu={','.join(GPU_QUERY_FIELDS)}",
        "--format=csv,noheader,nounits",
    ]
    result = subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    gpus: list[dict[str, Any]] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = [part.strip() for part in next(csv.reader([line], skipinitialspace=True))]
        if len(parts) != len(GPU_QUERY_FIELDS):
            raise RuntimeError(f"unexpected nvidia-smi output line: {raw_line!r}")
        gpus.append(
            {
                "index": int(parts[0]),
                "name": parts[1],
                "temperature_c": parse_optional_float(parts[2]),
                "power_draw_w": parse_optional_float(parts[3]),
                "power_limit_w": parse_optional_float(parts[4]),
                "utilization_pct": parse_optional_float(parts[5]),
                "fan_pct": parse_optional_float(parts[6]),
            }
        )
    return gpus


def format_gpu_snapshot(gpu: dict[str, Any]) -> str:
    def fmt_metric(value: float | None, suffix: str) -> str:
        return "n/a" if value is None else f"{value:.0f}{suffix}"

    return (
        f"gpu{gpu['index']} {gpu['name']} | "
        f"temp={fmt_metric(gpu['temperature_c'], 'C')} | "
        f"power={fmt_metric(gpu['power_draw_w'], 'W')}"
        f"/{fmt_metric(gpu['power_limit_w'], 'W')} | "
        f"util={fmt_metric(gpu['utilization_pct'], '%')} | "
        f"fan={fmt_metric(gpu['fan_pct'], '%')}"
    )


def get_gpu_violations(
    gpus: list[dict[str, Any]],
    max_temp_c: float | None,
    max_power_w: float | None,
) -> list[str]:
    violations: list[str] = []
    for gpu in gpus:
        if max_temp_c is not None and gpu["temperature_c"] is not None and gpu["temperature_c"] > max_temp_c:
            violations.append(
                f"gpu{gpu['index']} temperature {gpu['temperature_c']:.0f}C > {max_temp_c:.0f}C"
            )
        if max_power_w is not None and gpu["power_draw_w"] is not None and gpu["power_draw_w"] > max_power_w:
            violations.append(
                f"gpu{gpu['index']} power {gpu['power_draw_w']:.0f}W > {max_power_w:.0f}W"
            )
    return violations


def maybe_report_or_guard_gpus(
    args: argparse.Namespace,
    next_gpu_report_at: float,
) -> tuple[float, list[str]]:
    needs_gpu_query = (
        args.max_temp_c is not None
        or args.max_power_w is not None
        or args.gpu_report_interval > 0
    )
    if not needs_gpu_query:
        return next_gpu_report_at, []

    gpus = query_local_gpus()
    now = time.monotonic()
    if args.gpu_report_interval > 0 and now >= next_gpu_report_at:
        for gpu in gpus:
            print(f"[gpu] {format_gpu_snapshot(gpu)}")
        next_gpu_report_at = now + args.gpu_report_interval

    violations = get_gpu_violations(gpus, args.max_temp_c, args.max_power_w)
    return next_gpu_report_at, violations


def build_miner_command(args: argparse.Namespace) -> list[str]:
    cmd = [
        args.miner_binary,
        "--pool",
        args.pool,
        "--address",
        args.address,
        "--worker",
        args.worker,
    ]
    if args.password:
        cmd += ["--password", args.password]
    if args.devices:
        cmd += ["--devices", args.devices]
    if args.status_interval is not None:
        cmd += ["--status-interval", str(args.status_interval)]
    if args.force_backend:
        cmd += ["--force-backend", args.force_backend]
    for extra_arg in args.extra_arg or []:
        cmd.append(extra_arg)
    return cmd


def sanitize_command_for_log(cmd: list[str]) -> str:
    sanitized: list[str] = []
    redact_next = False
    for item in cmd:
        if redact_next:
            sanitized.append("***")
            redact_next = False
            continue
        sanitized.append(item)
        if item == "--password":
            redact_next = True
    return " ".join(sanitized)


def notify_if_configured(args: argparse.Namespace, message: str) -> None:
    if not args.telegram_bot_token or not args.telegram_chat_id:
        return
    send_telegram(args.telegram_bot_token, args.telegram_chat_id, message)


def safe_notify(args: argparse.Namespace, message: str) -> None:
    try:
        notify_if_configured(args, message)
    except Exception as exc:  # noqa: BLE001
        print(f"notify error: {exc}", file=sys.stderr)


def seed_seen_blocks(
    state: dict[str, Any],
    payload: dict[str, Any],
    notify_contributed: bool,
) -> None:
    for block in payload.get("blocks", []):
        block_hash = block.get("hash")
        if not block_hash:
            continue
        if block.get("finder"):
            remember(state, "finder_blocks", block_hash)
        if notify_contributed:
            remember(state, "contributed_blocks", block_hash)


def seed_seen_workers(state: dict[str, Any], payload: dict[str, Any]) -> None:
    seen = state.setdefault("seen_workers", {})
    now_ts = int(time.time())
    for worker in payload.get("workers", []):
        name = get_worker_name(worker)
        if not name or name in seen:
            continue
        seen[name] = {
            "first_seen_ts": now_ts,
            "last_online": bool(worker.get("online")),
            "last_seen_ts": now_ts,
        }
    _prune_seen_workers(seen)


def _prune_seen_workers(seen: dict[str, Any]) -> None:
    if len(seen) <= MAX_TRACKED_WORKERS:
        return
    sorted_items = sorted(seen.items(), key=lambda item: item[1].get("last_seen_ts", 0))
    for name, _info in sorted_items[: len(sorted_items) - MAX_TRACKED_WORKERS]:
        del seen[name]


def detect_worker_events(
    state: dict[str, Any],
    payload: dict[str, Any],
    notify_workers: bool,
    notify_offline: bool,
) -> list[str]:
    seen = state.setdefault("seen_workers", {})
    events: list[str] = []
    now_ts = int(time.time())
    for worker in payload.get("workers", []):
        name = get_worker_name(worker)
        if not name:
            continue
        is_online = bool(worker.get("online"))
        prev = seen.get(name)
        if prev is None:
            seen[name] = {
                "first_seen_ts": now_ts,
                "last_online": is_online,
                "last_seen_ts": now_ts,
            }
            if notify_workers:
                events.append(format_worker_event_message(
                    "PRL worker joined", name, worker, payload, is_first=True,
                ))
            continue
        prev["last_seen_ts"] = now_ts
        if prev.get("last_online") != is_online:
            if is_online and notify_workers:
                events.append(format_worker_event_message(
                    "PRL worker back online", name, worker, payload, is_first=False,
                ))
            elif (not is_online) and notify_offline:
                events.append(format_worker_event_message(
                    "PRL worker offline", name, worker, payload, is_first=False,
                ))
            prev["last_online"] = is_online
    _prune_seen_workers(seen)
    return events


def format_block_message(
    address: str,
    block: dict[str, Any],
    direct_finder: bool,
    payload: dict[str, Any],
    local_workers: set[str] | None = None,
) -> str:
    metrics = derive_address_metrics(payload, local_workers=local_workers)
    lines = ["PRL block event"]
    append_pair_section(
        lines,
        "Event",
        [
            ("type", "direct finder block" if direct_finder else "contributed pool block"),
            ("address", address),
            ("height", str(block.get("height", "n/a"))),
            ("hash", str(block.get("hash", "n/a"))),
            ("reward", fmt_prl(float(block.get("reward_prl", 0.0) or 0.0))),
            ("your share", fmt_prl(float(block.get("my_share_grain", 0) or 0.0) / 1e8)),
            ("mode", "solo" if block.get("is_solo") else "pool"),
            ("status", block_status(block)),
        ],
    )
    append_pair_section(
        lines,
        "Address state",
        [
            ("workers online", fmt_count(metrics["workers_online"])),
            ("workers configured", fmt_count(metrics["workers_configured"])),
            ("live hashrate", metrics["live_hashrate_text"]),
            ("1h", metrics["hashrate_1h"]),
            ("24h", metrics["hashrate_24h"]),
            ("pending balance", fmt_prl(metrics["balance_prl"])),
            ("total earned", fmt_prl(metrics["total_earned_prl"])),
        ],
    )
    return "\n".join(lines)


def format_runtime_event_message(title: str, rows: list[tuple[str, str]]) -> str:
    lines = [title]
    append_pair_section(lines, "Details", rows)
    return "\n".join(lines)


def send_status_update(
    args: argparse.Namespace,
    payload: dict[str, Any],
    header: str,
    local_workers: set[str] | None = None,
) -> None:
    safe_notify(
        args,
        format_status_message(
            args.address,
            payload,
            header=header,
            local_workers=local_workers,
        ),
    )


def print_address_message(message: str) -> None:
    print(message)


def run_status(args: argparse.Namespace) -> int:
    payload = get_miner_stats(args.address)
    local_workers = {args.worker} if getattr(args, "worker", None) else None
    print_address_message(
        format_status_message(args.address, payload, header="PRL status", local_workers=local_workers)
    )
    return 0


def run_workers(args: argparse.Namespace) -> int:
    payload = get_miner_stats(args.address)
    local_workers = {args.worker} if getattr(args, "worker", None) else None
    print_address_message(
        format_workers_message(args.address, payload, header="PRL workers", local_workers=local_workers)
    )
    return 0


def run_blocks(args: argparse.Namespace) -> int:
    payload = get_miner_stats(args.address)
    print_address_message(format_blocks_message(args.address, payload, header="PRL blocks"))
    return 0


def run_devices(_args: argparse.Namespace) -> int:
    gpus = query_local_gpus()
    print_address_message(format_devices_message(gpus))
    return 0


def extract_telegram_message(update: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("message", "edited_message"):
        message = update.get(key)
        if isinstance(message, dict):
            return message
    return None


def normalize_telegram_command(text: str) -> str:
    command = text.strip().split(maxsplit=1)[0].lower()
    if "@" in command:
        command = command.split("@", 1)[0]
    return command


def build_telegram_command_response(args: argparse.Namespace, text: str) -> str:
    command = normalize_telegram_command(text)
    local_workers = {args.worker} if getattr(args, "worker", None) else None

    if command in {"/help", "/start"}:
        return format_help_message()
    if command == "/devices":
        return format_devices_message(query_local_gpus())
    if command in {"/status", "/stats"}:
        payload = get_miner_stats(args.address)
        return format_status_message(args.address, payload, header="PRL status", local_workers=local_workers)
    if command == "/workers":
        payload = get_miner_stats(args.address)
        return format_workers_message(args.address, payload, header="PRL workers", local_workers=local_workers)
    if command == "/blocks":
        payload = get_miner_stats(args.address)
        return format_blocks_message(args.address, payload, header="PRL blocks")
    if command in {"/servers", "/rigs"}:
        payload = get_miner_stats(args.address)
        return format_workers_message(args.address, payload, header="PRL servers", local_workers=local_workers)
    if command in {"/earnings", "/profit", "/balance"}:
        payload = get_miner_stats(args.address)
        pool_stats = None
        try:
            pool_stats = get_pool_stats()
        except Exception:
            pool_stats = None
        price = getattr(args, "price_usd", None)
        miner_fee = getattr(args, "miner_fee_percent", 1.0)
        return format_earnings_message(
            args.address,
            payload,
            pool_stats=pool_stats,
            price_usd=price,
            miner_fee_percent=miner_fee,
        )
    return format_help_message()


def run_bot(args: argparse.Namespace) -> int:
    stop_event = threading.Event()

    def stop_handler(signum: int, _frame: Any) -> None:
        print(f"received signal {signum}, shutting down...")
        stop_event.set()

    signal.signal(signal.SIGINT, stop_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop_handler)

    offset: int | None = None
    safe_notify(args, format_help_message())
    print(f"telegram bot command loop started for chat {args.telegram_chat_id}")

    while not stop_event.is_set():
        try:
            updates = get_telegram_updates(
                args.telegram_bot_token,
                offset=offset,
                timeout=args.telegram_poll_timeout,
            )
            for update in updates:
                update_id = int(update.get("update_id", 0) or 0)
                message = extract_telegram_message(update)
                if message is None:
                    offset = update_id + 1
                    continue
                chat = message.get("chat", {})
                if str(chat.get("id")) != str(args.telegram_chat_id):
                    offset = update_id + 1
                    continue
                text = str(message.get("text") or "").strip()
                if not text.startswith("/"):
                    offset = update_id + 1
                    continue
                try:
                    reply = build_telegram_command_response(args, text)
                except Exception as exc:  # noqa: BLE001
                    reply = format_runtime_event_message(
                        "PRL bot command failed",
                        [("command", text), ("error", str(exc))],
                    )
                send_telegram(args.telegram_bot_token, args.telegram_chat_id, reply)
                offset = update_id + 1
        except Exception as exc:  # noqa: BLE001
            print(f"telegram bot error: {exc}", file=sys.stderr)
            stop_event.wait(3)

    return 0


def build_bot_process_env(args: argparse.Namespace) -> dict[str, str]:
    child_env = os.environ.copy()
    child_env["PRL_ADDRESS"] = args.address
    child_env["TG_BOT_TOKEN"] = args.telegram_bot_token
    child_env["TG_CHAT_ID"] = args.telegram_chat_id
    if getattr(args, "worker", None):
        child_env["PRL_WORKER"] = args.worker
    if getattr(args, "price_usd", None) is not None:
        child_env["PRL_PRICE_USD"] = str(args.price_usd)
    if getattr(args, "miner_fee_percent", None) is not None:
        child_env["PRL_MINER_FEE_PERCENT"] = str(args.miner_fee_percent)
    return child_env


def poll_and_notify(
    args: argparse.Namespace,
    state: dict[str, list[str]],
    state_path: Path,
    seeded: bool,
) -> tuple[bool, dict[str, Any]]:
    payload = get_miner_stats(args.address)
    local_workers = {args.worker} if getattr(args, "worker", None) else None

    if not seeded:
        seed_seen_blocks(state, payload, args.notify_contributed)
        seed_seen_workers(state, payload)
        save_state(state_path, state)
        return True, payload

    changed = False
    blocks = list(payload.get("blocks", []))
    blocks.reverse()

    for block in blocks:
        block_hash = block.get("hash")
        if not block_hash:
            continue

        if block.get("finder") and block_hash not in state["finder_blocks"]:
            notify_if_configured(
                args,
                format_block_message(
                    args.address,
                    block,
                    direct_finder=True,
                    payload=payload,
                    local_workers=local_workers,
                ),
            )
            remember(state, "finder_blocks", block_hash)
            if args.notify_contributed:
                remember(state, "contributed_blocks", block_hash)
            changed = True
        elif args.notify_contributed and block_hash not in state["contributed_blocks"]:
            notify_if_configured(
                args,
                format_block_message(
                    args.address,
                    block,
                    direct_finder=False,
                    payload=payload,
                    local_workers=local_workers,
                ),
            )
            remember(state, "contributed_blocks", block_hash)
            changed = True

    worker_events = detect_worker_events(
        state,
        payload,
        notify_workers=getattr(args, "notify_workers", True),
        notify_offline=getattr(args, "notify_offline", True),
    )
    for event in worker_events:
        notify_if_configured(args, event)
        changed = True

    if changed:
        save_state(state_path, state)

    return True, payload


def run_monitor(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser()
    state = load_state(state_path)
    stop_event = threading.Event()

    def stop_handler(signum: int, _frame: Any) -> None:
        print(f"received signal {signum}, shutting down...")
        stop_event.set()

    signal.signal(signal.SIGINT, stop_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop_handler)

    seeded = bool(state["finder_blocks"] or state["contributed_blocks"])
    startup_status_sent = False
    next_status_at = time.monotonic() + args.telegram_status_interval_minutes * 60
    print(f"monitoring address {args.address}")
    print(f"state file: {state_path}")
    if not seeded:
        print("first run: current blocks will be seeded as seen, no historical alerts")

    while not stop_event.is_set():
        try:
            seeded, payload = poll_and_notify(args, state, state_path, seeded)
            if not startup_status_sent:
                send_status_update(args, payload, header="PRL monitor started")
                startup_status_sent = True
            if args.telegram_status_interval_minutes > 0 and time.monotonic() >= next_status_at:
                send_status_update(args, payload, header="PRL status update")
                next_status_at = time.monotonic() + args.telegram_status_interval_minutes * 60
        except Exception as exc:  # noqa: BLE001
            print(f"monitor error: {exc}", file=sys.stderr)
        stop_event.wait(args.poll_interval)

    return 0


def run_miner(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser()
    state = load_state(state_path)
    stop_event = threading.Event()

    def stop_handler(signum: int, _frame: Any) -> None:
        print(f"received signal {signum}, shutting down...")
        stop_event.set()

    signal.signal(signal.SIGINT, stop_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop_handler)

    seeded = bool(state["finder_blocks"] or state["contributed_blocks"])
    startup_status_sent = False
    cmd = build_miner_command(args)
    gpu_query_enabled = (
        args.max_temp_c is not None
        or args.max_power_w is not None
        or args.gpu_report_interval > 0
    )
    gpu_check_interval = max(1, args.gpu_check_interval)
    print("miner command:")
    print(sanitize_command_for_log(cmd))

    if not seeded:
        print("first run: current blocks will be seeded as seen, no historical alerts")

    next_gpu_report_at = time.monotonic()

    while not stop_event.is_set():
        print("starting miner...")
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=build_miner_process_env(),
            )
        except OSError as exc:
            print(f"miner start failed: {exc}", file=sys.stderr)
            safe_notify(
                args,
                format_runtime_event_message(
                    "PRL miner start failed",
                    [
                        ("address", args.address),
                        ("worker", args.worker),
                        ("error", str(exc)),
                    ],
                ),
            )
            if args.no_restart:
                return 1
            stop_event.wait(args.restart_delay)
            continue

        next_pool_poll_at = time.monotonic()
        next_gpu_check_at = time.monotonic()
        next_status_at = time.monotonic() + args.telegram_status_interval_minutes * 60

        reader = None
        if process.stdout is not None:
            reader = threading.Thread(
                target=tee_stream,
                args=(process.stdout, "miner"),
                daemon=True,
            )
            reader.start()

        while not stop_event.is_set():
            now = time.monotonic()
            if now >= next_pool_poll_at:
                next_pool_poll_at = now + args.poll_interval
                try:
                    seeded, payload = poll_and_notify(args, state, state_path, seeded)
                    if not startup_status_sent:
                        send_status_update(
                            args,
                            payload,
                            header=f"PRL miner started ({args.worker})",
                            local_workers={args.worker},
                        )
                        startup_status_sent = True
                    if args.telegram_status_interval_minutes > 0 and now >= next_status_at:
                        send_status_update(
                            args,
                            payload,
                            header=f"PRL status update ({args.worker})",
                            local_workers={args.worker},
                        )
                        next_status_at = now + args.telegram_status_interval_minutes * 60
                except Exception as exc:  # noqa: BLE001
                    print(f"monitor error: {exc}", file=sys.stderr)

            if gpu_query_enabled and now >= next_gpu_check_at:
                next_gpu_check_at = now + gpu_check_interval
                try:
                    next_gpu_report_at, violations = maybe_report_or_guard_gpus(args, next_gpu_report_at)
                except Exception as exc:  # noqa: BLE001
                    print(f"gpu monitor error: {exc}", file=sys.stderr)
                    safe_notify(
                        args,
                        format_runtime_event_message(
                            "PRL gpu monitor failed",
                            [
                                ("address", args.address),
                                ("worker", args.worker),
                                ("error", str(exc)),
                            ],
                        ),
                    )
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    return 2

                if violations:
                    message = "; ".join(violations)
                    print(message, file=sys.stderr)
                    safe_notify(
                        args,
                        format_runtime_event_message(
                            "PRL gpu safety stop",
                            [
                                ("address", args.address),
                                ("worker", args.worker),
                                ("reason", message),
                            ],
                        ),
                    )
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    return 2

            exit_code = process.poll()
            if exit_code is not None:
                print(f"miner exited with code {exit_code}")
                safe_notify(
                    args,
                    format_runtime_event_message(
                        "PRL miner exited",
                        [
                            ("address", args.address),
                            ("worker", args.worker),
                            ("exit code", str(exit_code)),
                        ],
                    ),
                )
                break

            wait_candidates = [max(0.0, next_pool_poll_at - time.monotonic())]
            if gpu_query_enabled:
                wait_candidates.append(max(0.0, next_gpu_check_at - time.monotonic()))
            wait_seconds = min(wait_candidates)
            stop_event.wait(wait_seconds if wait_seconds > 0 else 0.2)

        if stop_event.is_set():
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
            break

        if reader is not None:
            reader.join(timeout=2)

        if args.no_restart:
            return process.returncode or 0

        print(f"restarting in {args.restart_delay} seconds...")
        stop_event.wait(args.restart_delay)

    return 0


def run_hub(args: argparse.Namespace) -> int:
    bot_cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "bot",
        "--telegram-poll-timeout",
        str(args.telegram_poll_timeout),
    ]
    print("bot command:")
    print(" ".join(bot_cmd))

    try:
        bot_process = subprocess.Popen(
            bot_cmd,
            env=build_bot_process_env(args),
        )
    except OSError as exc:
        print(f"bot start failed: {exc}", file=sys.stderr)
        return 1

    try:
        return run_monitor(args)
    finally:
        if bot_process.poll() is None:
            bot_process.terminate()
            try:
                bot_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                bot_process.kill()


def run_serve(args: argparse.Namespace) -> int:
    bot_cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "bot",
        "--telegram-poll-timeout",
        str(args.telegram_poll_timeout),
    ]
    print("bot command:")
    print(" ".join(bot_cmd))

    try:
        bot_process = subprocess.Popen(
            bot_cmd,
            env=build_bot_process_env(args),
        )
    except OSError as exc:
        print(f"bot start failed: {exc}", file=sys.stderr)
        return 1

    try:
        return run_miner(args)
    finally:
        if bot_process.poll() is None:
            bot_process.terminate()
            try:
                bot_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                bot_process.kill()


def add_common_monitor_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--address", required=False, help="PRL address (prl1p...)")
    parser.add_argument(
        "--telegram-bot-token",
        default=None,
        help="Telegram bot token or TG_BOT_TOKEN env",
    )
    parser.add_argument(
        "--telegram-chat-id",
        default=None,
        help="Telegram chat id or TG_CHAT_ID env",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=None,
        help=f"poll interval in seconds, default {DEFAULT_POLL_INTERVAL_SECONDS}",
    )
    parser.add_argument(
        "--state-file",
        default=None,
        help=f"state file path, default {DEFAULT_STATE_FILE}",
    )
    parser.add_argument(
        "--notify-contributed",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="notify on every pool block where your address got a share, not only finder=true blocks",
    )
    parser.add_argument(
        "--telegram-status-interval-minutes",
        type=int,
        default=None,
        help="send periodic Telegram status every N minutes, 0 disables it",
    )
    parser.add_argument(
        "--notify-workers",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Telegram alerts on new/returning workers (default on)",
    )
    parser.add_argument(
        "--notify-offline",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Telegram alerts when a worker drops offline (default on)",
    )
    parser.add_argument(
        "--price-usd",
        type=float,
        default=None,
        help="PRL/USD price for /earnings estimate; env PRL_PRICE_USD",
    )
    parser.add_argument(
        "--miner-fee-percent",
        type=float,
        default=None,
        help="miner client fee percent for /earnings estimate, default 1 for alpha-miner",
    )


def add_run_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--miner-binary", required=False, help="path to alpha-miner binary")
    parser.add_argument(
        "--pool",
        default=None,
        help=f"stratum endpoint, default {DEFAULT_POOL_URL}",
    )
    parser.add_argument("--worker", required=False, help="worker name")
    parser.add_argument("--password", help="optional password, e.g. x;d=65536")
    parser.add_argument("--devices", help="optional device list, e.g. 0,1,2")
    parser.add_argument("--status-interval", type=int, help="alpha-miner status interval")
    parser.add_argument(
        "--force-backend",
        choices=["volta", "ampere", "ada", "hopper", "blackwell", "blackwell-native"],
        help="override backend autodetect",
    )
    parser.add_argument(
        "--extra-arg",
        action="append",
        help="append a literal extra arg to the miner command",
    )
    parser.add_argument(
        "--restart-delay",
        type=int,
        default=None,
        help=f"restart delay after crash, default {DEFAULT_RESTART_DELAY_SECONDS}",
    )
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="exit after miner process stops instead of restarting",
    )
    parser.add_argument(
        "--max-temp-c",
        type=float,
        help="stop miner if any local GPU exceeds this temperature in C",
    )
    parser.add_argument(
        "--max-power-w",
        type=float,
        help="stop miner if any local GPU exceeds this power draw in W",
    )
    parser.add_argument(
        "--gpu-report-interval",
        type=int,
        default=None,
        help="print local GPU telemetry every N seconds, 0 disables it",
    )
    parser.add_argument(
        "--gpu-check-interval",
        type=int,
        default=None,
        help="check local GPU safety every N seconds when gpu guard/reporting is enabled",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pearl/AlphaPool helper: live stats, miner runner, Telegram alerts",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    stats_parser = sub.add_parser("stats", help="print live Pearl stats and profitability")
    stats_parser.add_argument("--price-usd", type=float, default=0.36, help="PRL price in USD")
    stats_parser.add_argument("--hashrate-ths", type=float, help="your hashrate in TH/s")
    stats_parser.add_argument("--power-watts", type=float, help="your power draw in watts")
    stats_parser.add_argument(
        "--electricity-usd-kwh",
        type=float,
        help="electricity price in USD/kWh",
    )
    stats_parser.add_argument(
        "--miner-fee-percent",
        type=float,
        default=1.0,
        help="extra miner client fee percent, default 1 for alpha-miner",
    )

    monitor_parser = sub.add_parser("monitor", help="monitor an address and send Telegram alerts")
    add_common_monitor_args(monitor_parser)

    status_parser = sub.add_parser("status", help="print current address status")
    add_common_monitor_args(status_parser)
    status_parser.add_argument("--worker", help="optional local worker name for configured count")

    workers_parser = sub.add_parser("workers", help="print worker list and hashrate details")
    add_common_monitor_args(workers_parser)
    workers_parser.add_argument("--worker", help="optional local worker name for configured count")

    blocks_parser = sub.add_parser("blocks", help="print recent blocks and rewards")
    add_common_monitor_args(blocks_parser)

    devices_parser = sub.add_parser("devices", help="print local GPU telemetry")

    bot_parser = sub.add_parser("bot", help="run Telegram bot command loop")
    add_common_monitor_args(bot_parser)
    bot_parser.add_argument(
        "--worker",
        help="optional local worker name to display in status responses",
    )
    bot_parser.add_argument(
        "--telegram-poll-timeout",
        type=int,
        default=DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS,
        help=f"Telegram getUpdates timeout, default {DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS}",
    )

    hub_parser = sub.add_parser(
        "hub",
        help="run central control: address monitor + Telegram bot, no mining",
    )
    add_common_monitor_args(hub_parser)
    hub_parser.add_argument(
        "--worker",
        help="optional local worker name to highlight in status responses",
    )
    hub_parser.add_argument(
        "--telegram-poll-timeout",
        type=int,
        default=DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS,
        help=f"Telegram getUpdates timeout, default {DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS}",
    )

    serve_parser = sub.add_parser("serve", help="run miner and Telegram bot command loop together")
    add_common_monitor_args(serve_parser)
    add_run_args(serve_parser)
    serve_parser.add_argument(
        "--telegram-poll-timeout",
        type=int,
        default=DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS,
        help=f"Telegram getUpdates timeout, default {DEFAULT_TELEGRAM_POLL_TIMEOUT_SECONDS}",
    )

    run_parser = sub.add_parser("run", help="run alpha-miner and monitor the address")
    add_common_monitor_args(run_parser)
    add_run_args(run_parser)

    return parser


def normalize_args(args: argparse.Namespace) -> argparse.Namespace:
    load_dotenv(SCRIPT_DIR / DEFAULT_DOTENV_FILE)

    if hasattr(args, "address"):
        args.address = env_or(args.address, "PRL_ADDRESS")
    if hasattr(args, "telegram_bot_token"):
        args.telegram_bot_token = env_or(args.telegram_bot_token, "TG_BOT_TOKEN")
    if hasattr(args, "telegram_chat_id"):
        args.telegram_chat_id = env_or(args.telegram_chat_id, "TG_CHAT_ID")
    if hasattr(args, "poll_interval"):
        args.poll_interval = env_or(args.poll_interval, "PRL_POLL_INTERVAL")
        args.poll_interval = DEFAULT_POLL_INTERVAL_SECONDS if args.poll_interval is None else int(args.poll_interval)
    if hasattr(args, "state_file"):
        args.state_file = env_or(args.state_file, "PRL_STATE_FILE") or DEFAULT_STATE_FILE
    if hasattr(args, "notify_contributed"):
        if args.notify_contributed is None:
            args.notify_contributed = parse_bool(os.environ.get("PRL_NOTIFY_CONTRIBUTED", "false"))
    if hasattr(args, "telegram_status_interval_minutes"):
        args.telegram_status_interval_minutes = env_or(args.telegram_status_interval_minutes, "TG_STATUS_INTERVAL_MINUTES")
        args.telegram_status_interval_minutes = (
            0 if args.telegram_status_interval_minutes is None else int(args.telegram_status_interval_minutes)
        )
    if hasattr(args, "notify_workers"):
        if args.notify_workers is None:
            args.notify_workers = parse_bool(os.environ.get("PRL_NOTIFY_WORKERS", "true"))
    if hasattr(args, "notify_offline"):
        if args.notify_offline is None:
            args.notify_offline = parse_bool(os.environ.get("PRL_NOTIFY_OFFLINE", "true"))
    if hasattr(args, "price_usd"):
        args.price_usd = env_or(args.price_usd, "PRL_PRICE_USD")
        if args.price_usd is not None:
            args.price_usd = float(args.price_usd)
    if hasattr(args, "miner_fee_percent"):
        if args.miner_fee_percent is None:
            args.miner_fee_percent = float(os.environ.get("PRL_MINER_FEE_PERCENT", "1.0"))
    if hasattr(args, "miner_binary"):
        args.miner_binary = env_or(args.miner_binary, "PRL_MINER_BINARY")
        if args.miner_binary:
            args.miner_binary = str(Path(args.miner_binary).expanduser())
    if hasattr(args, "pool"):
        args.pool = env_or(args.pool, "PRL_POOL") or DEFAULT_POOL_URL
    if hasattr(args, "worker"):
        args.worker = env_or(args.worker, "PRL_WORKER")
        if args.worker is None and getattr(args, "command", None) in {"run", "serve"}:
            args.worker = default_worker_name()
    if hasattr(args, "password"):
        args.password = env_or(args.password, "PRL_PASSWORD")
    if hasattr(args, "devices"):
        args.devices = env_or(args.devices, "PRL_DEVICES")
    if hasattr(args, "status_interval"):
        args.status_interval = env_or(args.status_interval, "PRL_STATUS_INTERVAL")
        if args.status_interval is not None:
            args.status_interval = int(args.status_interval)
    if hasattr(args, "force_backend"):
        args.force_backend = env_or(args.force_backend, "PRL_FORCE_BACKEND")
    if hasattr(args, "restart_delay"):
        args.restart_delay = env_or(args.restart_delay, "PRL_RESTART_DELAY")
        args.restart_delay = DEFAULT_RESTART_DELAY_SECONDS if args.restart_delay is None else int(args.restart_delay)
    if hasattr(args, "max_temp_c"):
        args.max_temp_c = env_or(args.max_temp_c, "PRL_MAX_TEMP_C")
        if args.max_temp_c is not None:
            args.max_temp_c = float(args.max_temp_c)
    if hasattr(args, "max_power_w"):
        args.max_power_w = env_or(args.max_power_w, "PRL_MAX_POWER_W")
        if args.max_power_w is not None:
            args.max_power_w = float(args.max_power_w)
    if hasattr(args, "gpu_report_interval"):
        args.gpu_report_interval = env_or(args.gpu_report_interval, "PRL_GPU_REPORT_INTERVAL")
        args.gpu_report_interval = 0 if args.gpu_report_interval is None else int(args.gpu_report_interval)
    if hasattr(args, "gpu_check_interval"):
        args.gpu_check_interval = env_or(args.gpu_check_interval, "PRL_GPU_CHECK_INTERVAL")
        args.gpu_check_interval = 5 if args.gpu_check_interval is None else int(args.gpu_check_interval)
    if hasattr(args, "telegram_poll_timeout") and args.telegram_poll_timeout is not None:
        args.telegram_poll_timeout = int(args.telegram_poll_timeout)
    return args


def validate_address_args(args: argparse.Namespace) -> None:
    if not args.address:
        raise SystemExit("--address is required (or set PRL_ADDRESS)")


def validate_optional_telegram_args(args: argparse.Namespace) -> None:
    if bool(args.telegram_bot_token) != bool(args.telegram_chat_id):
        raise SystemExit("set both --telegram-bot-token and --telegram-chat-id, or neither")


def validate_monitor_args(args: argparse.Namespace) -> None:
    validate_address_args(args)
    validate_optional_telegram_args(args)


def validate_bot_args(args: argparse.Namespace) -> None:
    validate_address_args(args)
    validate_optional_telegram_args(args)
    if not args.telegram_bot_token or not args.telegram_chat_id:
        raise SystemExit("bot requires --telegram-bot-token and --telegram-chat-id")


def validate_run_args(args: argparse.Namespace) -> None:
    if not args.miner_binary:
        raise SystemExit("--miner-binary is required (or set PRL_MINER_BINARY)")
    if not args.worker:
        raise SystemExit("--worker is required (or set PRL_WORKER)")


def main() -> int:
    parser = build_parser()
    args = normalize_args(parser.parse_args())

    if args.command == "stats":
        metrics = derive_live_metrics(get_pool_stats())
        print_live_stats(metrics)
        if args.hashrate_ths is not None:
            print_profit_estimate(
                metrics,
                hashrate_ths=args.hashrate_ths,
                price_usd=args.price_usd,
                power_watts=args.power_watts,
                electricity_usd_kwh=args.electricity_usd_kwh,
                extra_fee_percent=args.miner_fee_percent,
            )
        return 0

    if args.command == "monitor":
        validate_monitor_args(args)
        return run_monitor(args)

    if args.command == "status":
        validate_address_args(args)
        validate_optional_telegram_args(args)
        return run_status(args)

    if args.command == "workers":
        validate_address_args(args)
        validate_optional_telegram_args(args)
        return run_workers(args)

    if args.command == "blocks":
        validate_address_args(args)
        validate_optional_telegram_args(args)
        return run_blocks(args)

    if args.command == "devices":
        return run_devices(args)

    if args.command == "bot":
        validate_bot_args(args)
        return run_bot(args)

    if args.command == "hub":
        validate_bot_args(args)
        return run_hub(args)

    if args.command == "serve":
        validate_bot_args(args)
        validate_run_args(args)
        return run_serve(args)

    if args.command == "run":
        validate_monitor_args(args)
        validate_run_args(args)
        return run_miner(args)

    raise SystemExit(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
