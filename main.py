#!/usr/bin/env python
"""Entry point for the Polymarket weather bot.

    python main.py            # engine + Telegram control
    python main.py --scan     # one forecast scan, print signals, exit
    python main.py --no-tg    # engine only, log to console
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from polyweather import logging_setup
from polyweather.config import settings
from polyweather.engine.bot import TradingBot

log = logging.getLogger("main")


def run_scan_only() -> int:
    bot = TradingBot()
    try:
        signals = bot.forecast.generate()
        if not signals:
            print("No signals above the edge threshold.")
            return 0
        print(f"\n{len(signals)} signal(s):\n")
        for s in signals:
            print(f"  {s.market}")
            print(f"    ask {s.price:.3f} | model {s.model_prob:.1%} | edge {s.edge:+.1%}")
            print(f"    {s.note}\n")
        return 0
    finally:
        bot.close()


def run_headless() -> int:
    bot = TradingBot()
    bot.start()
    log.info("running headless; Ctrl-C to stop")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        bot.close()
    return 0


def run_with_telegram() -> int:
    if not settings.telegram_bot_token:
        print("TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and fill it in,")
        print("or run with --no-tg to start the engine without Telegram control.")
        return 1

    from polyweather.tg.app import TelegramApp
    from polyweather.tg.notifier import Notifier

    chat_ids = [c.strip() for c in str(settings.telegram_chat_id).split(",") if c.strip()]
    if not chat_ids:
        log.warning("TELEGRAM_CHAT_ID is empty — alerts will have nowhere to go")

    notifier = Notifier(chat_ids)
    bot = TradingBot(notify=notifier.send)
    app = TelegramApp(bot, notifier)
    try:
        app.run()          # blocks; starts the engine from post_init
    except KeyboardInterrupt:
        pass
    finally:
        bot.close()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Polymarket weather trading bot")
    p.add_argument("--scan", action="store_true", help="run one scan and exit")
    p.add_argument("--no-tg", action="store_true", help="run the engine without Telegram")
    p.add_argument("--log-level", default=None)
    args = p.parse_args()

    logging_setup.setup(args.log_level)
    log.info("polyweather starting — mode=%s", settings.trading_mode)

    if args.scan:
        return run_scan_only()
    if args.no_tg:
        return run_headless()
    return run_with_telegram()


if __name__ == "__main__":
    sys.exit(main())
