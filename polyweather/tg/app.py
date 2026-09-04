"""Telegram control surface for the weather bot.

Every command is gated on the allow-list from settings; an unknown chat gets a
flat refusal and is logged.
"""
from __future__ import annotations

import asyncio
import functools
import html
import logging
from datetime import datetime, timezone

from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from ..config import settings
from ..engine.bot import TradingBot
from .notifier import Notifier

log = logging.getLogger(__name__)

HELP = """*Polymarket Weather Bot*

*Status*
/status – engine state, PnL, limits
/positions – open positions
/trades – last 15 trades
/pnl – running PnL
/signals – run a scan now and show the edges found
/leaders – copy-trade wallets being followed

*Control*
/arm – enable live order placement
/disarm – disable live order placement
/pause /resume – halt or resume all strategies
/kill – emergency stop (blocks every order)
/revive – clear the kill switch
/mode paper|live – switch trading mode
/strategy forecast on|off, /strategy copy on|off

*Tuning*
/set maxpos 25 – max USDC per market
/set daily 200 – max USDC notional per day
/set edge 0.06 – minimum edge to trade
/set copyscale 0.05 – fraction of leader size to mirror
/config – show current settings
/reload – re-read the leader wallet file
"""


def _esc(s) -> str:
    return html.escape(str(s))


def restricted(fn):
    """Allow-list guard for every handler."""
    @functools.wraps(fn)
    async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        allowed = settings.allowed_user_ids
        if allowed and (user is None or user.id not in allowed):
            log.warning("rejected command from %s (%s)", user.id if user else "?",
                        user.username if user else "?")
            if update.effective_message:
                await update.effective_message.reply_text("Not authorised.")
            return
        return await fn(self, update, context)
    return wrapper


class TelegramApp:
    def __init__(self, bot: TradingBot, notifier: Notifier):
        self.bot = bot
        self.notifier = notifier
        self.app = Application.builder().token(settings.telegram_bot_token).post_init(
            self._post_init
        ).build()
        self._register()

    # ------------------------------------------------------------- lifecycle
    async def _post_init(self, app: Application) -> None:
        self.notifier.bind(app.bot, asyncio.get_running_loop())
        await app.bot.set_my_commands([
            BotCommand("status", "engine state and PnL"),
            BotCommand("positions", "open positions"),
            BotCommand("trades", "recent trades"),
            BotCommand("signals", "scan now"),
            BotCommand("leaders", "copy-trade wallets"),
            BotCommand("arm", "enable live orders"),
            BotCommand("disarm", "disable live orders"),
            BotCommand("pause", "halt strategies"),
            BotCommand("resume", "resume strategies"),
            BotCommand("kill", "emergency stop"),
            BotCommand("config", "show settings"),
            BotCommand("help", "command list"),
        ])
        self.bot.start()
        self.notifier.send(
            f"🌤 Weather bot online — mode *{settings.trading_mode}*.\n"
            f"Send /status for details."
        )

    def _register(self) -> None:
        h = self.app.add_handler
        h(CommandHandler(["start", "help"], self.cmd_help))
        h(CommandHandler("status", self.cmd_status))
        h(CommandHandler("positions", self.cmd_positions))
        h(CommandHandler("trades", self.cmd_trades))
        h(CommandHandler("pnl", self.cmd_pnl))
        h(CommandHandler("signals", self.cmd_signals))
        h(CommandHandler("leaders", self.cmd_leaders))
        h(CommandHandler("arm", self.cmd_arm))
        h(CommandHandler("disarm", self.cmd_disarm))
        h(CommandHandler("pause", self.cmd_pause))
        h(CommandHandler("resume", self.cmd_resume))
        h(CommandHandler("kill", self.cmd_kill))
        h(CommandHandler("revive", self.cmd_revive))
        h(CommandHandler("mode", self.cmd_mode))
        h(CommandHandler("strategy", self.cmd_strategy))
        h(CommandHandler("set", self.cmd_set))
        h(CommandHandler("config", self.cmd_config))
        h(CommandHandler("reload", self.cmd_reload))
        self.app.add_error_handler(self.on_error)

    def run(self) -> None:
        log.info("starting Telegram polling")
        self.app.run_polling(drop_pending_updates=True, stop_signals=None)

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.exception("telegram handler error", exc_info=context.error)

    # -------------------------------------------------------------- handlers
    @restricted
    async def cmd_help(self, update: Update, _ctx) -> None:
        await update.effective_message.reply_text(HELP, parse_mode=ParseMode.MARKDOWN)

    @restricted
    async def cmd_status(self, update: Update, _ctx) -> None:
        await update.effective_message.reply_text(
            self.bot.status_text(), parse_mode=ParseMode.MARKDOWN
        )

    @restricted
    async def cmd_positions(self, update: Update, _ctx) -> None:
        rows = self.bot.store.open_positions()
        if not rows:
            await update.effective_message.reply_text("No open positions.")
            return
        lines = ["<b>Open positions</b>"]
        total = 0.0
        for r in rows[:25]:
            total += float(r["cost"])
            avg = float(r["cost"]) / float(r["shares"]) if r["shares"] else 0
            lines.append(
                f"• {_esc(r['market'])}\n"
                f"  {float(r['shares']):.1f} sh @ {avg:.3f} = ${float(r['cost']):.2f}"
            )
        lines.append(f"\n<b>Total cost:</b> ${total:.2f}")
        await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    @restricted
    async def cmd_trades(self, update: Update, _ctx) -> None:
        rows = self.bot.store.recent_trades(15)
        if not rows:
            await update.effective_message.reply_text("No trades yet.")
            return
        lines = ["<b>Recent trades</b>"]
        for r in rows:
            ts = datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime("%m-%d %H:%M")
            lines.append(
                f"• <code>{ts}</code> [{_esc(r['mode'])}/{_esc(r['strategy'])}]\n"
                f"  {_esc(r['market'])}\n"
                f"  {_esc(r['side'])} {float(r['size']):.1f} @ {float(r['price']):.3f} "
                f"= ${float(r['notional']):.2f} (edge {float(r['edge'] or 0):+.1%})"
            )
        await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    @restricted
    async def cmd_pnl(self, update: Update, _ctx) -> None:
        today = self.bot.store.today()
        totals = self.bot.store.totals()
        await update.effective_message.reply_text(
            f"Today: {float(today['realized_pnl']):+.2f} USDC "
            f"over {today['n_trades']} trade(s)\n"
            f"All time: {totals['realized_pnl']:+.2f} USDC "
            f"over {totals['n_trades']} trade(s)"
        )

    @restricted
    async def cmd_signals(self, update: Update, _ctx) -> None:
        msg = await update.effective_message.reply_text("Scanning weather markets…")
        signals = await asyncio.to_thread(self.bot.forecast.generate)
        if not signals:
            await msg.edit_text("No signals clear the edge threshold right now.")
            return
        lines = [f"<b>{len(signals)} signal(s)</b>"]
        for s in signals[:12]:
            lines.append(
                f"• {_esc(s.market)}\n"
                f"  ask {s.price:.3f} vs model {s.model_prob:.1%} "
                f"→ edge <b>{s.edge:+.1%}</b>\n"
                f"  <i>{_esc(s.note)}</i>"
            )
        await msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)

    @restricted
    async def cmd_leaders(self, update: Update, _ctx) -> None:
        leaders = self.bot.copier.leaders
        if not leaders:
            await update.effective_message.reply_text(
                "No leader wallets loaded.\n"
                f"Expected file: {settings.copy_wallets_file}"
            )
            return
        lines = [f"<b>Following {len(leaders)} wallet(s)</b>"]
        for ld in leaders:
            lines.append(f"• {_esc(ld.label)} — <code>{_esc(ld.wallet)}</code> ×{ld.weight:g}")
        lines.append(f"\nMirroring {settings.copy_scale:.1%} of leader notional.")
        await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    # ---------------------------------------------------------------- control
    @restricted
    async def cmd_arm(self, update: Update, _ctx) -> None:
        missing = settings.require_live_ready()
        if settings.trading_mode == "live" and missing:
            await update.effective_message.reply_text(
                "Cannot arm: missing " + ", ".join(missing)
            )
            return
        self.bot.risk.set_armed(True)
        await update.effective_message.reply_text(
            "🔓 Armed. Live orders enabled."
            if settings.trading_mode == "live"
            else "🔓 Armed (still in paper mode — /mode live to trade for real)."
        )

    @restricted
    async def cmd_disarm(self, update: Update, _ctx) -> None:
        self.bot.risk.set_armed(False)
        await update.effective_message.reply_text("🔒 Disarmed. No live orders will be placed.")

    @restricted
    async def cmd_pause(self, update: Update, _ctx) -> None:
        self.bot.risk.set_paused(True)
        await update.effective_message.reply_text("⏸ Paused.")

    @restricted
    async def cmd_resume(self, update: Update, _ctx) -> None:
        self.bot.risk.set_paused(False)
        await update.effective_message.reply_text("▶️ Resumed.")

    @restricted
    async def cmd_kill(self, update: Update, _ctx) -> None:
        self.bot.risk.set_kill(True, "manual /kill")
        result = await asyncio.to_thread(self.bot.clob.cancel_all)
        await update.effective_message.reply_text(
            "🛑 KILL SWITCH ENGAGED. No further orders.\n"
            f"Cancel open orders: {'ok' if result.ok else result.error}"
        )

    @restricted
    async def cmd_revive(self, update: Update, _ctx) -> None:
        self.bot.risk.set_kill(False, "")
        await update.effective_message.reply_text("Kill switch cleared. Bot may trade again.")

    @restricted
    async def cmd_mode(self, update: Update, ctx) -> None:
        args = ctx.args or []
        if not args or args[0] not in ("paper", "live"):
            await update.effective_message.reply_text(
                f"Current mode: {settings.trading_mode}\nUsage: /mode paper|live"
            )
            return
        target = args[0]
        if target == "live":
            missing = settings.require_live_ready()
            if missing:
                await update.effective_message.reply_text(
                    "Cannot switch to live: missing " + ", ".join(missing)
                )
                return
        settings.trading_mode = target
        self.bot.risk.set_armed(False)
        await update.effective_message.reply_text(
            f"Mode set to *{target}*. Disarmed as a precaution — /arm to enable orders.",
            parse_mode=ParseMode.MARKDOWN,
        )

    @restricted
    async def cmd_strategy(self, update: Update, ctx) -> None:
        args = [a.lower() for a in (ctx.args or [])]
        if len(args) != 2 or args[0] not in ("forecast", "copy") or args[1] not in ("on", "off"):
            await update.effective_message.reply_text("Usage: /strategy forecast|copy on|off")
            return
        on = args[1] == "on"
        if args[0] == "forecast":
            settings.enable_forecast_edge = on
        else:
            settings.enable_copy_trading = on
        await update.effective_message.reply_text(f"{args[0]} strategy {'enabled' if on else 'disabled'}.")

    @restricted
    async def cmd_set(self, update: Update, ctx) -> None:
        fields = {
            "maxpos": ("max_position_usdc", float),
            "daily": ("max_daily_notional_usdc", float),
            "edge": ("min_edge", float),
            "copyscale": ("copy_scale", float),
            "bankroll": ("bankroll_usdc", float),
            "maxloss": ("max_daily_loss_usdc", float),
            "kelly": ("kelly_fraction", float),
            "maxopen": ("max_open_positions", int),
        }
        args = ctx.args or []
        if len(args) != 2 or args[0].lower() not in fields:
            await update.effective_message.reply_text(
                "Usage: /set <key> <value>\nKeys: " + ", ".join(fields)
            )
            return
        attr, cast = fields[args[0].lower()]
        try:
            value = cast(args[1])
        except ValueError:
            await update.effective_message.reply_text(f"'{args[1]}' is not a valid number.")
            return
        if value <= 0:
            await update.effective_message.reply_text("Value must be positive.")
            return
        setattr(settings, attr, value)
        await update.effective_message.reply_text(f"{attr} = {value}")

    @restricted
    async def cmd_config(self, update: Update, _ctx) -> None:
        s = settings
        await update.effective_message.reply_text(
            f"<b>Config</b>\n"
            f"mode: {s.trading_mode}\n"
            f"bankroll: ${s.bankroll_usdc:.0f}   kelly: {s.kelly_fraction:g}\n"
            f"max/market: ${s.max_position_usdc:.0f}   max/day: ${s.max_daily_notional_usdc:.0f}\n"
            f"max open: {s.max_open_positions}   daily stop: -${s.max_daily_loss_usdc:.0f}\n"
            f"min edge: {s.min_edge:.1%}   price band: {s.min_price}–{s.max_price}\n"
            f"max spread: {s.max_spread}   min volume: ${s.min_market_volume:.0f}\n"
            f"copy scale: {s.copy_scale:.1%}   copy max age: {s.copy_max_age_sec}s\n"
            f"scan every {s.scan_interval_sec}s, copy poll every {s.copy_poll_interval_sec}s\n"
            f"clob: {_esc(self.bot.clob.health())}",
            parse_mode=ParseMode.HTML,
        )

    @restricted
    async def cmd_reload(self, update: Update, _ctx) -> None:
        n = self.bot.copier.reload_leaders()
        await update.effective_message.reply_text(f"Reloaded {n} leader wallet(s).")
