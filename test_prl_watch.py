import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch
from subprocess import CompletedProcess

import prl_watch


class PrlWatchTests(unittest.TestCase):
    def sample_payload(self) -> dict[str, object]:
        return {
            "workers": [
                {
                    "name": "rig01",
                    "online": True,
                    "hashrate_live": "1.40 TH/s",
                    "hashrate_1h": "240 GH/s",
                    "hashrate": "10 GH/s",
                    "difficulty": 50000,
                    "time": 1,
                }
            ],
            "blocks": [
                {"finder": True, "height": 10, "my_share_grain": 200, "reward_prl": 15, "ts": 1},
                {"finder": False, "height": 9, "my_share_grain": 100, "reward_prl": 10, "ts": 1},
            ],
            "payments": [{"amount_grain": 300, "status": "pending"}],
            "estHash1h": "123 GH/s",
            "estHash24h": "111 GH/s",
            "payments_count": 2,
            "shares24h": 99,
            "total_paid_prl": 12.5,
            "balance_prl": 1.25,
            "mode": "PPLNS",
            "last_seen": 1,
        }

    def test_load_dotenv_supports_quotes_and_preserves_existing_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "PRL_POOL='from-file'\nTG_BOT_TOKEN=\"hello\"\nPRL_PASSWORD=''\nIGNORED_TOKEN='secret'\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"PRL_POOL": "already-set"}, clear=True):
                prl_watch.load_dotenv(env_path)
                self.assertEqual(os.environ["PRL_POOL"], "already-set")
                self.assertEqual(os.environ["TG_BOT_TOKEN"], "hello")
                self.assertEqual(os.environ["PRL_PASSWORD"], "")
                self.assertNotIn("IGNORED_TOKEN", os.environ)

    def test_parse_hashrate_to_hps(self) -> None:
        self.assertEqual(prl_watch.parse_hashrate_to_hps("1.5 TH/s"), 1.5e12)

    def test_parse_optional_float(self) -> None:
        self.assertIsNone(prl_watch.parse_optional_float("N/A"))
        self.assertEqual(prl_watch.parse_optional_float("214.5"), 214.5)

    def test_save_state_creates_parent_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "nested" / "state.json"
            state = {
                "finder_blocks": ["a"],
                "contributed_blocks": ["b"],
                "seen_workers": {"rig01": {"first_seen_ts": 1, "last_online": True, "last_seen_ts": 1}},
            }
            prl_watch.save_state(state_path, state)
            loaded = prl_watch.load_state(state_path)
            self.assertEqual(loaded, state)

    def test_detect_worker_events_notifies_join_and_offline(self) -> None:
        state = {"finder_blocks": [], "contributed_blocks": [], "seen_workers": {}}
        payload = {
            "workers": [
                {"name": "rig01", "online": True, "hashrate_live": "500 GH/s"},
                {"name": "rig02", "online": False, "hashrate_live": "0 H/s"},
            ],
            "blocks": [],
            "estHash1h": "0 H/s",
            "estHash24h": "0 H/s",
        }
        events = prl_watch.detect_worker_events(
            state, payload, notify_workers=True, notify_offline=True,
        )
        self.assertEqual(len(events), 2)
        self.assertTrue(any("rig01" in event and "joined" in event for event in events))
        self.assertTrue(any("rig02" in event for event in events))
        events_again = prl_watch.detect_worker_events(
            state, payload, notify_workers=True, notify_offline=True,
        )
        self.assertEqual(events_again, [])

    def test_detect_worker_events_flips_online_offline(self) -> None:
        state = {
            "finder_blocks": [],
            "contributed_blocks": [],
            "seen_workers": {
                "rig01": {"first_seen_ts": 1, "last_online": True, "last_seen_ts": 1},
            },
        }
        payload_offline = {
            "workers": [{"name": "rig01", "online": False, "hashrate_live": "0 H/s"}],
            "blocks": [],
            "estHash1h": "0 H/s",
            "estHash24h": "0 H/s",
        }
        events = prl_watch.detect_worker_events(
            state, payload_offline, notify_workers=True, notify_offline=True,
        )
        self.assertEqual(len(events), 1)
        self.assertIn("offline", events[0])
        payload_online = {
            "workers": [{"name": "rig01", "online": True, "hashrate_live": "500 GH/s"}],
            "blocks": [],
            "estHash1h": "0 H/s",
            "estHash24h": "0 H/s",
        }
        events = prl_watch.detect_worker_events(
            state, payload_online, notify_workers=True, notify_offline=True,
        )
        self.assertEqual(len(events), 1)
        self.assertIn("back online", events[0])

    def test_detect_worker_events_respects_disabled_flags(self) -> None:
        state = {"finder_blocks": [], "contributed_blocks": [], "seen_workers": {}}
        payload = {
            "workers": [{"name": "rig01", "online": True, "hashrate_live": "500 GH/s"}],
            "blocks": [],
            "estHash1h": "0 H/s",
            "estHash24h": "0 H/s",
        }
        events = prl_watch.detect_worker_events(
            state, payload, notify_workers=False, notify_offline=False,
        )
        self.assertEqual(events, [])
        self.assertIn("rig01", state["seen_workers"])

    def test_format_earnings_message_includes_balance_and_estimate(self) -> None:
        miner_payload = self.sample_payload()
        pool_stats = {
            "chain": {"height": 60000, "difficulty": 5000000.0},
            "coins": [{"reward": 2700.0, "network_hash": "13.91 EH/s"}],
            "pool": {
                "hashrate": "1.85 EH/s",
                "blocks24h": 113,
                "miners24h": 961,
                "workers": 11000,
            },
            "feePercent": 5.0,
        }
        message = prl_watch.format_earnings_message(
            "prl1ptest",
            miner_payload,
            pool_stats=pool_stats,
            price_usd=0.5,
            miner_fee_percent=1.0,
        )
        self.assertIn("PRL earnings", message)
        self.assertIn("pending balance", message)
        self.assertIn("paid out", message)
        self.assertIn("net PRL/day", message)
        self.assertIn("net USD/day", message)

    def test_build_telegram_command_response_routes_earnings_and_servers(self) -> None:
        args = Namespace(
            address="prl1ptest",
            worker="rig01",
            price_usd=0.5,
            miner_fee_percent=1.0,
        )
        pool_stats = {
            "chain": {"height": 60000, "difficulty": 5000000.0},
            "coins": [{"reward": 2700.0, "network_hash": "13.91 EH/s"}],
            "pool": {"hashrate": "1.85 EH/s", "blocks24h": 113, "miners24h": 961, "workers": 11000},
            "feePercent": 5.0,
        }
        with patch("prl_watch.get_miner_stats", return_value=self.sample_payload()),              patch("prl_watch.get_pool_stats", return_value=pool_stats):
            earnings_reply = prl_watch.build_telegram_command_response(args, "/earnings")
            servers_reply = prl_watch.build_telegram_command_response(args, "/servers")
        self.assertIn("PRL earnings", earnings_reply)
        self.assertIn("PRL servers", servers_reply)

    def test_sanitize_command_redacts_password(self) -> None:
        cmd = ["alpha-miner", "--password", "x;d=65536", "--worker", "rig01"]
        self.assertEqual(
            prl_watch.sanitize_command_for_log(cmd),
            "alpha-miner --password *** --worker rig01",
        )

    def test_get_gpu_violations(self) -> None:
        gpus = [
            {
                "index": 0,
                "name": "RTX",
                "temperature_c": 81.0,
                "power_draw_w": 221.0,
                "power_limit_w": 250.0,
                "utilization_pct": 99.0,
                "fan_pct": 70.0,
            }
        ]
        violations = prl_watch.get_gpu_violations(gpus, max_temp_c=78, max_power_w=200)
        self.assertEqual(len(violations), 2)

    def test_query_local_gpus_parses_csv(self) -> None:
        output = '0, NVIDIA GeForce RTX 4060 Ti, 45, 33.54, 214.50, 10, 0\n'
        with patch(
            "prl_watch.subprocess.run",
            return_value=CompletedProcess(args=["nvidia-smi"], returncode=0, stdout=output),
        ):
            gpus = prl_watch.query_local_gpus()
        self.assertEqual(gpus[0]["index"], 0)
        self.assertEqual(gpus[0]["name"], "NVIDIA GeForce RTX 4060 Ti")
        self.assertEqual(gpus[0]["power_draw_w"], 33.54)

    def test_query_local_gpus_rejects_malformed_output(self) -> None:
        with patch(
            "prl_watch.subprocess.run",
            return_value=CompletedProcess(args=["nvidia-smi"], returncode=0, stdout="broken\n"),
        ):
            with self.assertRaises(RuntimeError):
                prl_watch.query_local_gpus()

    def test_normalize_args_reads_run_settings_from_env(self) -> None:
        args = Namespace(
            address=None,
            telegram_bot_token=None,
            telegram_chat_id=None,
            poll_interval=None,
            state_file=None,
            notify_contributed=None,
            telegram_status_interval_minutes=None,
            miner_binary=None,
            pool=None,
            worker=None,
            password=None,
            devices=None,
            status_interval=None,
            force_backend=None,
            restart_delay=None,
            max_temp_c=None,
            max_power_w=None,
            gpu_report_interval=None,
            gpu_check_interval=None,
        )
        with patch.dict(
            os.environ,
            {
                "PRL_ADDRESS": "prl1ptest",
                "PRL_MINER_BINARY": "alpha-miner",
                "PRL_WORKER": "rig01",
                "PRL_POOL": "stratum+tcp://eu1.alphapool.tech:5566",
                "PRL_STATUS_INTERVAL": "60",
                "PRL_POLL_INTERVAL": "45",
                "PRL_GPU_REPORT_INTERVAL": "120",
                "TG_STATUS_INTERVAL_MINUTES": "30",
            },
            clear=True,
        ), patch("prl_watch.load_dotenv"):
            normalized = prl_watch.normalize_args(args)

        self.assertEqual(normalized.address, "prl1ptest")
        self.assertEqual(normalized.miner_binary, "alpha-miner")
        self.assertEqual(normalized.worker, "rig01")
        self.assertEqual(normalized.pool, "stratum+tcp://eu1.alphapool.tech:5566")
        self.assertEqual(normalized.status_interval, 60)
        self.assertEqual(normalized.poll_interval, 45)
        self.assertEqual(normalized.gpu_report_interval, 120)
        self.assertEqual(normalized.telegram_status_interval_minutes, 30)

    def test_format_status_message_summarizes_workers_rewards_and_blocks(self) -> None:
        payload = self.sample_payload()

        message = prl_watch.format_status_message(
            "prl1ptest",
            payload,
            header="PRL status",
            local_workers={"rig01"},
        )

        self.assertIn("Overview", message)
        self.assertIn("Workers", message)
        self.assertIn("Hashrate", message)
        self.assertIn("Rewards", message)
        self.assertIn("Blocks", message)
        self.assertRegex(message, r"connected\s+1")
        self.assertRegex(message, r"online\s+1")
        self.assertRegex(message, r"configured\s+1")
        self.assertRegex(message, r"live\s+1\.40 TH/s")
        self.assertRegex(message, r"1h\s+123 GH/s")
        self.assertRegex(message, r"pending balance\s+1\.25000000 PRL")
        self.assertRegex(message, r"paid out\s+12\.50000000 PRL")
        self.assertRegex(message, r"total earned\s+13\.75000000 PRL")
        self.assertRegex(message, r"direct finder\s+1")

    def test_format_status_message_keeps_connected_workers_at_zero_until_pool_sees_them(self) -> None:
        payload = {
            "workers": [],
            "blocks": [],
            "estHash1h": "0 H/s",
            "estHash24h": "0 H/s",
        }

        message = prl_watch.format_status_message(
            "prl1ptest",
            payload,
            header="PRL status",
            local_workers={"rig01"},
        )

        self.assertRegex(message, r"connected\s+0")
        self.assertRegex(message, r"configured\s+1")

    def test_format_workers_message_lists_worker_details(self) -> None:
        message = prl_watch.format_workers_message(
            "prl1ptest",
            self.sample_payload(),
            local_workers={"rig01"},
        )

        self.assertIn("Worker list", message)
        self.assertIn("1. rig01", message)
        self.assertRegex(message, r"status\s+online")
        self.assertRegex(message, r"live\s+1\.40 TH/s")
        self.assertRegex(message, r"difficulty\s+50,000")

    def test_build_telegram_command_response_routes_status_and_help(self) -> None:
        args = Namespace(address="prl1ptest", worker="rig01")

        with patch("prl_watch.get_miner_stats", return_value=self.sample_payload()):
            status_reply = prl_watch.build_telegram_command_response(args, "/status")
            help_reply = prl_watch.build_telegram_command_response(args, "/help")

        self.assertIn("PRL status", status_reply)
        self.assertIn("PRL bot commands", help_reply)

    def test_build_telegram_command_response_routes_devices(self) -> None:
        args = Namespace(address="prl1ptest", worker="rig01")
        gpus = [
            {
                "index": 0,
                "name": "RTX 4060 Ti",
                "temperature_c": 55.0,
                "power_draw_w": 120.0,
                "power_limit_w": 214.0,
                "utilization_pct": 99.0,
                "fan_pct": 30.0,
            }
        ]

        with patch("prl_watch.query_local_gpus", return_value=gpus):
            reply = prl_watch.build_telegram_command_response(args, "/devices")

        self.assertIn("Local devices", reply)
        self.assertRegex(reply, r"detected gpus\s+1")
        self.assertIn("gpu0  RTX 4060 Ti", reply)

    def test_build_telegram_command_response_unknown_command_returns_help_without_pool_call(self) -> None:
        args = Namespace(address="prl1ptest", worker="rig01")

        with patch("prl_watch.get_miner_stats") as get_miner_stats:
            reply = prl_watch.build_telegram_command_response(args, "/unknown")

        get_miner_stats.assert_not_called()
        self.assertIn("PRL bot commands", reply)

    def test_build_miner_process_env_drops_helper_secrets(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PATH": "keep",
                "LANG": "C.UTF-8",
                "TG_BOT_TOKEN": "secret",
                "TG_CHAT_ID": "secret",
                "PRL_PASSWORD": "secret",
                "PRL_ADDRESS": "prl1ptest",
                "OPENAI_API_KEY": "secret",
            },
            clear=True,
        ):
            child_env = prl_watch.build_miner_process_env()

        self.assertEqual(child_env["PATH"], "keep")
        self.assertEqual(child_env["LANG"], "C.UTF-8")
        self.assertNotIn("TG_BOT_TOKEN", child_env)
        self.assertNotIn("TG_CHAT_ID", child_env)
        self.assertNotIn("PRL_PASSWORD", child_env)
        self.assertNotIn("PRL_ADDRESS", child_env)
        self.assertNotIn("OPENAI_API_KEY", child_env)

    def test_explicit_cli_values_beat_env_even_when_equal_to_old_defaults(self) -> None:
        args = Namespace(
            address=None,
            telegram_bot_token=None,
            telegram_chat_id=None,
            poll_interval=30,
            state_file=".prl-watch-state.json",
            notify_contributed=False,
            telegram_status_interval_minutes=0,
            miner_binary="alpha-miner",
            pool=prl_watch.DEFAULT_POOL_URL,
            worker="rig01",
            password=None,
            devices=None,
            status_interval=None,
            force_backend=None,
            restart_delay=15,
            max_temp_c=None,
            max_power_w=None,
            gpu_report_interval=0,
            gpu_check_interval=5,
        )
        with patch.dict(
            os.environ,
            {
                "PRL_POLL_INTERVAL": "45",
                "TG_STATUS_INTERVAL_MINUTES": "60",
                "PRL_NOTIFY_CONTRIBUTED": "true",
            },
            clear=True,
        ), patch("prl_watch.load_dotenv"):
            normalized = prl_watch.normalize_args(args)

        self.assertEqual(normalized.poll_interval, 30)
        self.assertEqual(normalized.telegram_status_interval_minutes, 0)
        self.assertFalse(normalized.notify_contributed)

    def test_poll_and_notify_dedups_finder_and_contributed(self) -> None:
        args = Namespace(
            address="prl1ptest",
            worker="rig01",
            notify_contributed=True,
            telegram_bot_token=None,
            telegram_chat_id=None,
        )
        state = {"finder_blocks": [], "contributed_blocks": []}
        payload = {
            "blocks": [
                {
                    "height": 1,
                    "hash": "block1",
                    "reward_prl": 10,
                    "my_share_grain": 100,
                    "finder": True,
                    "is_solo": False,
                    "confirmed": False,
                    "paid_out": False,
                    "orphaned": False,
                }
            ]
        }
        messages: list[str] = []

        with (
            patch("prl_watch.get_miner_stats", return_value=payload),
            patch("prl_watch.notify_if_configured", side_effect=lambda _args, msg: messages.append(msg)),
            patch("prl_watch.save_state"),
        ):
            seeded, _payload = prl_watch.poll_and_notify(args, state, Path("state.json"), seeded=True)
            self.assertTrue(seeded)
            self.assertEqual(len(messages), 1)
            self.assertIn("PRL block event", messages[0])
            self.assertIn("direct finder block", messages[0])
            self.assertRegex(messages[0], r"workers configured\s+1")
            prl_watch.poll_and_notify(args, state, Path("state.json"), seeded=True)
            self.assertEqual(len(messages), 1)


if __name__ == "__main__":
    unittest.main()
