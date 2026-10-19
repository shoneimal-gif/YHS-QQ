"""Credential boundary and foreground runner for authorized Paper copy trading."""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import signal
import threading

from alpaca_client import AlpacaPaperClient
from stock_quant.common import load_credentials
from stock_quant.notifications import StockNotifier
from .common import atomic_json, utc_now
from .copier import run_copier, validate_config


def main() -> int:
    parser = argparse.ArgumentParser(description='AlphaAgent | Alpaca Paper only')
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--observer-runtime', type=Path, required=True)
    args = parser.parse_args()
    stop = threading.Event()
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: stop.set())
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(name)s %(message)s')
    broker = notifier = None
    try:
        cfg = validate_config(json.loads(args.config.read_text(encoding='utf-8-sig')))
        if cfg['execute_paper'] is not True:
            raise ValueError('copy_execution_not_enabled')
        credentials = load_credentials()
        broker = AlpacaPaperClient(credentials['ALPACA_API_KEY'], credentials['ALPACA_SECRET_KEY'],
                                  timeout_seconds=10, max_get_retries=0)
        webhook = credentials.get('YHS_DISCORD_WEBHOOK')
        if webhook:
            notifier = StockNotifier(webhook, args.runtime / 'discord_alpha_outbox.json',
                                     header='AlphaAgent 跟单 | Alpaca Paper')
        result = run_copier(cfg, broker, observer_runtime=args.observer_runtime, runtime=args.runtime,
                            coordinator_root=Path('/var/lib/yhs-qq/runtime/account_coordination'),
                            notifier=notifier, stop_event=stop)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except Exception as exc:
        category = getattr(exc, 'category', 'copy_runner_startup_failed')
        if not isinstance(category, str) or len(category) > 80 or not all(c.isalnum() or c == '_' for c in category):
            category = 'copy_runner_startup_failed'
        result = {'failed': True, 'category': category, 'paper_only': True, 'updated_utc': utc_now().isoformat()}
        try:
            atomic_json(args.runtime / 'runner_error.json', result)
        except Exception:
            pass
        print(json.dumps(result))
        return 1
    finally:
        if notifier is not None:
            notifier.close()
        if broker is not None:
            broker.close()


if __name__ == '__main__':
    raise SystemExit(main())
