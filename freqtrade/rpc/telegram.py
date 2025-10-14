# pragma pylint: disable=unused-argument, unused-variable, protected-access, invalid-name

"""
This module manage Telegram communication
"""

import asyncio
import json
import logging
import re
from collections.abc import Callable, Coroutine
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import partial, wraps
from html import escape
from itertools import chain
from math import isnan
from threading import Thread
from typing import Any, Literal

from tabulate import tabulate
from telegram import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import MessageLimit, ParseMode
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.ext import (
    Application,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown

from freqtrade.__init__ import __version__
from freqtrade.constants import DUST_PER_COIN, Config
from freqtrade.enums import MarketDirection, RPCMessageType, SignalDirection, TradingMode
from freqtrade.exceptions import OperationalException
from freqtrade.misc import chunks, plural
from freqtrade.persistence import Trade
from freqtrade.rpc import RPC, RPCException, RPCHandler
from freqtrade.rpc.rpc_types import RPCEntryMsg, RPCExitMsg, RPCOrderMsg, RPCSendMsg
from freqtrade.rpc.trend_control import get_trend_controller
from freqtrade.util import (
    dt_from_ts,
    dt_humanize_delta,
    fmt_coin,
    fmt_coin2,
    format_date,
    round_value,
)


MAX_MESSAGE_LENGTH = MessageLimit.MAX_TEXT_LENGTH


logger = logging.getLogger(__name__)

logger.debug("Included module rpc.telegram ...")


def safe_async_db(func: Callable[..., Any]):
    """
    Decorator to safely handle sessions when switching async context
    :param func: function to decorate
    :return: decorated function
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        """Decorator logic"""
        try:
            return func(*args, **kwargs)
        finally:
            Trade.session.remove()

    return wrapper


@dataclass
class TimeunitMappings:
    header: str
    message: str
    message2: str
    callback: str
    default: int
    dateformat: str


def authorized_only(command_handler: Callable[..., Coroutine[Any, Any, None]]):
    """
    Decorator to check if the message comes from the correct chat_id
    can only be used with Telegram Class to decorate instance methods.
    :param command_handler: Telegram CommandHandler
    :return: decorated function
    """

    @wraps(command_handler)
    async def wrapper(self, *args, **kwargs) -> None:
        """Decorator logic"""
        update = kwargs.get("update") or args[0]

        # Reject unauthorized messages
        message: Message = (
            update.message if update.callback_query is None else update.callback_query.message
        )
        cchat_id: int = int(message.chat_id)
        ctopic_id: int | None = message.message_thread_id
        from_user_id: str = str(update.effective_user.id if update.effective_user else "")

        chat_id = int(self._config["telegram"]["chat_id"])
        if cchat_id != chat_id:
            logger.info(f"Rejected unauthorized message from: {cchat_id}")
            return None
        if (topic_id := self._config["telegram"].get("topic_id")) is not None:
            if str(ctopic_id) != topic_id:
                # This can be quite common in multi-topic environments.
                logger.debug(f"Rejected message from wrong channel: {cchat_id}, {ctopic_id}")
                return None

        authorized = self._config["telegram"].get("authorized_users", None)
        if authorized is not None and from_user_id not in authorized:
            logger.info(f"Unauthorized user tried to control the bot: {from_user_id}")
            return None
        # Rollback session to avoid getting data stored in a transaction.
        Trade.rollback()
        logger.debug("Executing handler: %s for chat_id: %s", command_handler.__name__, chat_id)
        try:
            return await command_handler(self, *args, **kwargs)
        except RPCException as e:
            await self._send_msg(str(e))
        except BaseException:
            logger.exception("Exception occurred within Telegram module")
        finally:
            Trade.session.remove()

    return wrapper


class Telegram(RPCHandler):
    """This class handles all telegram communication"""

    def __init__(self, rpc: RPC, config: Config) -> None:
        """
        Init the Telegram call, and init the super class RPCHandler
        :param rpc: instance of RPC Helper class
        :param config: Configuration object
        :return: None
        """
        super().__init__(rpc, config)

        self._app: Application
        self._loop: asyncio.AbstractEventLoop
        self._init_keyboard()
        self._start_thread()

    def _start_thread(self):
        """
        Creates and starts the polling thread
        """
        self._thread = Thread(target=self._init, name="FTTelegram")
        self._thread.start()

    def _init_keyboard(self) -> None:
        """
        Validates the keyboard configuration from telegram config
        section.
        """
        self._keyboard: list[list[str | KeyboardButton]] = [
            ["📅 /daily", "💰 /profit", "💼 /balance"],
            ["📊 /status table", "📈 /performance"],
            ["📈 /longall", "📉 /shortall", "❌ /closelong", "❌ /closeshort"],
            ["✅ /enable", "⛔ /disablelong", "✅ /enablelong", "⛔ /disableshort", "✅ /enableshort"],
            ["🔢 /count", "▶️ /start", "⏹️ /stop", "❓ /help"],
        ]
        # do not allow commands with mandatory arguments and critical cmds
        # TODO: DRY! - its not good to list all valid cmds here. But otherwise
        #       this needs refactoring of the whole telegram module (same
        #       problem in _help()).
        valid_keys: list[str] = [
            r"/start$",
            r"/pause$",
            r"/stop$",
            r"/status$",
            r"/status table$",
            r"/trades$",
            r"/performance$",
            r"/buys",
            r"/entries",
            r"/sells",
            r"/exits",
            r"/mix_tags",
            r"/daily$",
            r"/daily \d+$",
            r"/profit([_ ]long|[_ ]short)?$",
            r"/profit([_ ]long|[_ ]short)? \d+$",
            r"/stats$",
            r"/count$",
            r"/locks$",
            r"/balance$",
            r"/stopbuy$",
            r"/stopentry$",
            r"/reload_config$",
            r"/show_config$",
            r"/logs$",
            r"/whitelist$",
            r"/whitelist(\ssorted|\sbaseonly)+$",
            r"/blacklist$",
            r"/bl_delete$",
            r"/enable_pairs$",
            r"/enable$",
            r"/disable_pairs$",
            r"/disable$",
            r"/weekly$",
            r"/weekly \d+$",
            r"/monthly$",
            r"/monthly \d+$",
            r"/forcebuy$",
            r"/forcelong$",
            r"/forceshort$",
            r"/longall$",
            r"/shortall$",
            r"/closelong$",
            r"/closeshort$",
            r"/forcesell$",
            r"/forceexit$",
            r"/health$",
            r"/help$",
            r"/version$",
            r"/marketdir (long|short|even|none)$",
            r"/marketdir$",
            r"/enablelong$",
            r"/enableshort$",
            r"/disablelong$",
            r"/disableshort$",
            # Safety & Risk Management
            r"/setmaxopen$",
            r"/emergency_stop$",
            r"/pausefor$",
            r"/risk_status$",
            r"/set_stoploss$",
            # Quick Status & Monitoring
            r"/quickstatus$",
            r"/qs$",
            r"/alerts$",
            r"/watchlist$",
            r"/pnl$",
            # Batch Operations & Presets
            r"/preset$",
            r"/close_profitable$",
            r"/close_losing$",
            r"/scale_out$",
            # Smart Filtering & Control
            r"/enable_volatile$",
            r"/disable_low_volume$",
            r"/rotate_pairs$",
            r"/pause_new_shorts$",
            r"/pause_new_longs$",
            # Market Analysis
            r"/market_sentiment$",
            r"/top_gainers$",
            r"/top_losers$",
            # Trade Management
            r"/avg_down$",
            r"/take_profit$",
            r"/breakeven$",
            # Configuration Backup/Restore
            r"/config_backup$",
            r"/config_restore$",
            r"/strategy_reload$",
            # Menu System
            r"/menu_trade$",
            r"/menu_risk$",
            r"/menu_status$",
            # Profit Control
            r"/disable_roi$",
            r"/enable_roi$",
            r"/set_roi$",
            r"/disable_trailing$",
            r"/enable_trailing$",
            r"/profit_mode$",
            r"/profit_status$",
            # Separate Long/Short ROI
            r"/set_roi_long$",
            r"/set_roi_short$",
            r"/disable_roi_long$",
            r"/disable_roi_short$",
            r"/enable_roi_long$",
            r"/enable_roi_short$",
            r"/roi_status$",
            # ROI Management
            r"/force_roi_check$",
            r"/check_roi_targets$",
            r"/auto_roi_mode$",
            r"/extend_roi$",
        ]
        # Create keys for generation
        valid_keys_print = [k.replace("$", "") for k in valid_keys]

        # custom keyboard specified in config.json
        cust_keyboard = self._config["telegram"].get("keyboard", [])
        if cust_keyboard:
            combined = "(" + ")|(".join(valid_keys) + ")"
            # check for valid shortcuts
            # Strip emojis and extra whitespace before validation
            invalid_keys = [
                b for b in chain.from_iterable(cust_keyboard) 
                if not re.match(combined, re.sub(r'^[^\w\s/]+\s*', '', b))
            ]
            if len(invalid_keys):
                err_msg = (
                    "config.telegram.keyboard: Invalid commands for "
                    f"custom Telegram keyboard: {invalid_keys}"
                    f"\nvalid commands are: {valid_keys_print}"
                )
                raise OperationalException(err_msg)
            else:
                self._keyboard = cust_keyboard
                logger.info(f"using custom keyboard from config.json: {self._keyboard}")

    def _init_telegram_app(self):
        return Application.builder().token(self._config["telegram"]["token"]).build()

    @authorized_only
    async def _preprocess_emoji_commands(self, update: Update, context: CallbackContext) -> None:
        """
        Preprocesses messages with emoji prefixes to extract and route the actual command.
        This allows keyboard buttons like "📅 /daily" to work properly.
        """
        if not update.message or not update.message.text:
            return
        
        text = update.message.text
        # Check if message starts with non-word, non-slash, non-whitespace characters (emojis)
        if not re.match(r'^[^\w\s/]+\s*/', text):
            # No emoji prefix, let normal handlers process it
            return
        
        # Strip emojis and leading whitespace to get the clean command
        cleaned_text = re.sub(r'^[^\w\s/]+\s*', '', text).strip()
        
        # Parse command and arguments
        parts = cleaned_text.split()
        if not parts or not parts[0].startswith('/'):
            return
        
        command = parts[0][1:]  # Remove the leading '/'
        context.args = parts[1:] if len(parts) > 1 else []
        
        # Map command names to handler methods
        command_map = {
            'status': self._status,
            'profit': self._profit,
            'balance': self._balance,
            'start': self._start,
            'stop': self._stop,
            'daily': self._daily,
            'weekly': self._weekly,
            'monthly': self._monthly,
            'count': self._count,
            'performance': self._performance,
            'longall': self._longall,
            'shortall': self._shortall,
            'closelong': self._closelong,
            'closeshort': self._closeshort,
            'enable': self._enable,
            'enablelong': self._enablelong,
            'enableshort': self._enableshort,
            'disablelong': self._disablelong,
            'disableshort': self._disableshort,
            'help': self._help,
        }
        
        # Handle special multi-word commands like "status table"
        if command == 'status' and context.args and context.args[0] == 'table':
            await self._status_table(update, context)
            return
        
        # Route to the appropriate handler
        handler = command_map.get(command)
        if handler:
            await handler(update, context)

    def _init(self) -> None:
        """
        Initializes this module with the given config,
        registers all known command handlers
        and starts polling for message updates
        Runs in a separate thread.
        """
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

        self._app = self._init_telegram_app()

        # Register command handler and start telegram message polling
        handles = [
            CommandHandler("status", self._status),
            CommandHandler("profit", self._profit),
            CommandHandler("balance", self._balance),
            CommandHandler("start", self._start),
            CommandHandler("stop", self._stop),
            CommandHandler(["forcesell", "forceexit", "fx"], self._force_exit),
            CommandHandler(
                ["forcebuy", "forcelong"],
                partial(self._force_enter, order_side=SignalDirection.LONG),
            ),
            CommandHandler(
                "forceshort", partial(self._force_enter, order_side=SignalDirection.SHORT)
            ),
            CommandHandler("longall", self._longall),
            CommandHandler("shortall", self._shortall),
            CommandHandler("closelong", self._closelong),
            CommandHandler("closeshort", self._closeshort),
            CommandHandler("reload_trade", self._reload_trade_from_exchange),
            CommandHandler("trades", self._trades),
            CommandHandler("delete", self._delete_trade),
            CommandHandler(["coo", "cancel_open_order"], self._cancel_open_order),
            CommandHandler("performance", self._performance),
            CommandHandler(["buys", "entries"], self._enter_tag_performance),
            CommandHandler(["sells", "exits"], self._exit_reason_performance),
            CommandHandler("mix_tags", self._mix_tag_performance),
            CommandHandler("stats", self._stats),
            CommandHandler("daily", self._daily),
            CommandHandler("weekly", self._weekly),
            CommandHandler("monthly", self._monthly),
            CommandHandler("count", self._count),
            CommandHandler("locks", self._locks),
            CommandHandler(["unlock", "delete_locks"], self._delete_locks),
            CommandHandler(["reload_config", "reload_conf"], self._reload_config),
            CommandHandler(["show_config", "show_conf"], self._show_config),
            CommandHandler(["stopbuy", "stopentry", "pause"], self._pause),
            CommandHandler("whitelist", self._whitelist),
            CommandHandler("blacklist", self._blacklist),
            CommandHandler(["blacklist_delete", "bl_delete"], self._blacklist_delete),
            CommandHandler("enable_pairs", self._enable_pairs),
            CommandHandler("enable", self._enable),
            CommandHandler(["disable_pairs", "disable"], self._disable_pairs),
            CommandHandler("logs", self._logs),
            CommandHandler("health", self._health),
            CommandHandler("help", self._help),
            CommandHandler("version", self._version),
            CommandHandler("marketdir", self._changemarketdir),
            CommandHandler("enablelong", self._enablelong),
            CommandHandler("enableshort", self._enableshort),
            CommandHandler("disablelong", self._disablelong),
            CommandHandler("disableshort", self._disableshort),
            CommandHandler("order", self._order),
            CommandHandler("list_custom_data", self._list_custom_data),
            CommandHandler("tg_info", self._tg_info),
            CommandHandler("profit_long", self._profit_long),
            CommandHandler("profit_short", self._profit_short),
            # Safety & Risk Management
            CommandHandler("setmaxopen", self._setmaxopen),
            CommandHandler("emergency_stop", self._emergency_stop),
            CommandHandler("pausefor", self._pausefor),
            CommandHandler("risk_status", self._risk_status),
            CommandHandler("set_stoploss", self._set_stoploss),
            # Quick Status & Monitoring
            CommandHandler(["quickstatus", "qs"], self._quickstatus),
            CommandHandler("alerts", self._alerts),
            CommandHandler("watchlist", self._watchlist),
            CommandHandler("pnl", self._pnl),
            # Batch Operations & Presets
            CommandHandler("preset", self._preset_save),  # Will handle both save/load in method
            CommandHandler("close_profitable", self._close_profitable),
            CommandHandler("close_losing", self._close_losing),
            CommandHandler("scale_out", self._scale_out),
            # Smart Filtering & Control
            CommandHandler("enable_volatile", self._enable_volatile),
            CommandHandler("disable_low_volume", self._disable_low_volume),
            CommandHandler("rotate_pairs", self._rotate_pairs),
            CommandHandler("pause_new_shorts", self._pause_new_shorts),
            CommandHandler("pause_new_longs", self._pause_new_longs),
            # Market Analysis
            CommandHandler("market_sentiment", self._market_sentiment),
            CommandHandler("top_gainers", self._top_gainers),
            CommandHandler("top_losers", self._top_losers),
            # Trade Management
            CommandHandler("avg_down", self._avg_down),
            CommandHandler("take_profit", self._take_profit),
            CommandHandler("breakeven", self._breakeven),
            # Configuration Backup/Restore
            CommandHandler("config_backup", self._config_backup),
            CommandHandler("config_restore", self._config_restore),
            CommandHandler("strategy_reload", self._strategy_reload),
            # Menu System
            CommandHandler("menu_trade", self._menu_trade),
            CommandHandler("menu_risk", self._menu_risk),
            CommandHandler("menu_status", self._menu_status),
            # Profit Control Commands
            CommandHandler("disable_roi", self._disable_roi),
            CommandHandler("enable_roi", self._enable_roi),
            CommandHandler("set_roi", self._set_roi),
            CommandHandler("disable_trailing", self._disable_trailing),
            CommandHandler("enable_trailing", self._enable_trailing),
            CommandHandler("profit_mode", self._profit_mode),
            CommandHandler("profit_status", self._profit_status),
            # Separate Long/Short ROI Commands
            CommandHandler("set_roi_long", self._set_roi_long),
            CommandHandler("set_roi_short", self._set_roi_short),
            CommandHandler("disable_roi_long", self._disable_roi_long),
            CommandHandler("disable_roi_short", self._disable_roi_short),
            CommandHandler("enable_roi_long", self._enable_roi_long),
            CommandHandler("enable_roi_short", self._enable_roi_short),
            CommandHandler("roi_status", self._roi_status_separate),
            # ROI Management Commands
            CommandHandler("force_roi_check", self._force_roi_check),
            CommandHandler("check_roi_targets", self._check_roi_targets),
            CommandHandler("auto_roi_mode", self._auto_roi_mode),
            CommandHandler("extend_roi", self._extend_roi),
            # Trend Control Commands
            CommandHandler("trendset", self._trend_set),
            CommandHandler("trendget", self._trend_get),
            CommandHandler("trendclear", self._trend_clear),
            CommandHandler("trendstatus", self._trend_status),
            CommandHandler("trendhold", self._trend_hold),
            CommandHandler("longall", self._trend_longall),
            CommandHandler("shortall", self._trend_shortall),
            CommandHandler(["trendneutral", "neutraltrend"], self._trend_neutral),
        ]
        callbacks = [
            CallbackQueryHandler(self._status_table, pattern="update_status_table"),
            CallbackQueryHandler(self._daily, pattern="update_daily"),
            CallbackQueryHandler(self._weekly, pattern="update_weekly"),
            CallbackQueryHandler(self._monthly, pattern="update_monthly"),
            CallbackQueryHandler(self._profit_long, pattern="update_profit_long"),
            CallbackQueryHandler(self._profit_short, pattern="update_profit_short"),
            CallbackQueryHandler(self._profit, pattern=r"update_profit$"),
            CallbackQueryHandler(self._balance, pattern="update_balance"),
            CallbackQueryHandler(self._performance, pattern="update_performance"),
            CallbackQueryHandler(
                self._enter_tag_performance, pattern="update_enter_tag_performance"
            ),
            CallbackQueryHandler(
                self._exit_reason_performance, pattern="update_exit_reason_performance"
            ),
            CallbackQueryHandler(self._mix_tag_performance, pattern="update_mix_tag_performance"),
            CallbackQueryHandler(self._count, pattern="update_count"),
            CallbackQueryHandler(self._force_exit_inline, pattern=r"force_exit__\S+"),
            CallbackQueryHandler(self._force_enter_inline, pattern=r"force_enter__\S+"),
            CallbackQueryHandler(self._longall_callback, pattern=r"confirm_longall__\S+"),
            CallbackQueryHandler(self._shortall_callback, pattern=r"confirm_shortall__\S+"),
            CallbackQueryHandler(self._closelong_callback, pattern=r"confirm_closelong__\S+"),
            CallbackQueryHandler(self._closeshort_callback, pattern=r"confirm_closeshort__\S+"),
            CallbackQueryHandler(
                self._trend_longall_callback, pattern=r"confirm_trend_longall__\S+"
            ),
            CallbackQueryHandler(
                self._trend_shortall_callback, pattern=r"confirm_trend_shortall__\S+"
            ),
        ]
        
        # Add emoji preprocessing handler with group=-1 to run before command handlers
        # This allows keyboard buttons with emoji prefixes (e.g., "📅 /daily") to work
        emoji_preprocessor = MessageHandler(filters.TEXT, self._preprocess_emoji_commands)
        self._app.add_handler(emoji_preprocessor, group=-1)
        
        for handle in handles:
            self._app.add_handler(handle)

        for callback in callbacks:
            self._app.add_handler(callback)

        logger.info(
            "rpc.telegram is listening for following commands: %s",
            [[x for x in sorted(h.commands)] for h in handles],
        )
        self._loop.run_until_complete(self._startup_telegram())

    async def _startup_telegram(self) -> None:
        retries = 3
        attempt = 0
        while attempt < retries:
            try:
                await self._app.initialize()
                await self._app.start()
                break
            except Exception as ex:
                logger.error(
                    "Error starting Telegram bot (attempt %d/%d): %s", attempt + 1, retries, ex
                )
                attempt += 1
                if attempt == retries:
                    logger.warning("Telegram init failed.")
                    return
                await asyncio.sleep(2)
        if self._app.updater:
            await self._app.updater.start_polling(
                bootstrap_retries=10,
                timeout=20,
                drop_pending_updates=True,
            )
            while True:
                await asyncio.sleep(10)
                if not self._app.updater.running:
                    break

    async def _cleanup_telegram(self) -> None:
        if self._app.updater:
            await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()

    def cleanup(self) -> None:
        """
        Stops all running telegram threads.
        :return: None
        """
        # This can take up to `timeout` from the call to `start_polling`.
        asyncio.run_coroutine_threadsafe(self._cleanup_telegram(), self._loop)
        self._thread.join()

    def _exchange_from_msg(self, msg: RPCOrderMsg) -> str:
        """
        Extracts the exchange name from the given message.
        :param msg: The message to extract the exchange name from.
        :return: The exchange name.
        """
        return f"{msg['exchange']}{' (dry)' if self._config['dry_run'] else ''}"

    def _add_analyzed_candle(self, pair: str) -> str:
        candle_val = (
            self._config["telegram"].get("notification_settings", {}).get("show_candle", "off")
        )
        if candle_val != "off":
            if candle_val == "ohlc":
                analyzed_df, _ = self._rpc._freqtrade.dataprovider.get_analyzed_dataframe(
                    pair, self._config["timeframe"]
                )
                candle = analyzed_df.iloc[-1].squeeze() if len(analyzed_df) > 0 else None
                if candle is not None:
                    return (
                        f"*Candle OHLC*: `{candle['open']}, {candle['high']}, "
                        f"{candle['low']}, {candle['close']}`\n"
                    )

        return ""

    def _format_entry_msg(self, msg: RPCEntryMsg) -> str:
        is_fill = msg["type"] in [RPCMessageType.ENTRY_FILL]
        emoji = "\N{CHECK MARK}" if is_fill else "\N{LARGE BLUE CIRCLE}"

        terminology = {
            "1_enter": "New Trade",
            "1_entered": "New Trade filled",
            "x_enter": "Increasing position",
            "x_entered": "Position increase filled",
        }

        key = f"{'x' if msg['sub_trade'] else '1'}_{'entered' if is_fill else 'enter'}"
        wording = terminology[key]

        message = (
            f"{emoji} *{self._exchange_from_msg(msg)}:*"
            f" {wording} (#{msg['trade_id']})\n"
            f"*Pair:* `{msg['pair']}`\n"
        )
        message += self._add_analyzed_candle(msg["pair"])
        message += f"*Enter Tag:* `{msg['enter_tag']}`\n" if msg.get("enter_tag") else ""
        message += f"*Amount:* `{round_value(msg['amount'], 8)}`\n"
        message += f"*Direction:* `{msg['direction']}"
        if msg.get("leverage") and msg.get("leverage", 1.0) != 1.0:
            message += f" ({msg['leverage']:.3g}x)"
        message += "`\n"
        message += f"*Open Rate:* `{fmt_coin2(msg['open_rate'], msg['quote_currency'])}`\n"
        if msg["type"] == RPCMessageType.ENTRY and msg["current_rate"]:
            message += (
                f"*Current Rate:* `{fmt_coin2(msg['current_rate'], msg['quote_currency'])}`\n"
            )

        profit_fiat_extra = self.__format_profit_fiat(msg, "stake_amount")  # type: ignore
        total = fmt_coin(msg["stake_amount"], msg["quote_currency"])

        message += f"*{'New ' if msg['sub_trade'] else ''}Total:* `{total}{profit_fiat_extra}`"

        return message

    def _format_exit_msg(self, msg: RPCExitMsg) -> str:
        duration = msg["close_date"].replace(microsecond=0) - msg["open_date"].replace(
            microsecond=0
        )
        duration_min = duration.total_seconds() / 60

        leverage_text = (
            f" ({msg['leverage']:.3g}x)"
            if msg.get("leverage") and msg.get("leverage", 1.0) != 1.0
            else ""
        )

        profit_fiat_extra = self.__format_profit_fiat(msg, "profit_amount")

        profit_extra = (
            f" ({msg['gain']}: {fmt_coin(msg['profit_amount'], msg['quote_currency'])}"
            f"{profit_fiat_extra})"
        )

        is_fill = msg["type"] == RPCMessageType.EXIT_FILL
        is_sub_trade = msg.get("sub_trade")
        is_sub_profit = msg["profit_amount"] != msg.get("cumulative_profit")
        is_final_exit = msg.get("is_final_exit", False) and is_sub_profit
        profit_prefix = "Sub " if is_sub_trade else ""
        cp_extra = ""
        exit_wording = "Exited" if is_fill else "Exiting"
        if is_sub_trade or is_final_exit:
            cp_fiat = self.__format_profit_fiat(msg, "cumulative_profit")

            if is_final_exit:
                profit_prefix = "Sub "
                cp_extra = (
                    f"*Final Profit:* `{msg['final_profit_ratio']:.2%} "
                    f"({msg['cumulative_profit']:.8f} {msg['quote_currency']}{cp_fiat})`\n"
                )
            else:
                exit_wording = f"Partially {exit_wording.lower()}"
                if msg["cumulative_profit"]:
                    cp_extra = (
                        f"*Cumulative Profit:* `"
                        f"{fmt_coin(msg['cumulative_profit'], msg['stake_currency'])}{cp_fiat}`\n"
                    )
        enter_tag = f"*Enter Tag:* `{msg['enter_tag']}`\n" if msg.get("enter_tag") else ""
        message = (
            f"{self._get_exit_emoji(msg)} *{self._exchange_from_msg(msg)}:* "
            f"{exit_wording} {msg['pair']} (#{msg['trade_id']})\n"
            f"{self._add_analyzed_candle(msg['pair'])}"
            f"*{f'{profit_prefix}Profit' if is_fill else f'Unrealized {profit_prefix}Profit'}:* "
            f"`{msg['profit_ratio']:.2%}{profit_extra}`\n"
            f"{cp_extra}"
            f"{enter_tag}"
            f"*Exit Reason:* `{msg['exit_reason']}`\n"
            f"*Direction:* `{msg['direction']}"
            f"{leverage_text}`\n"
            f"*Amount:* `{round_value(msg['amount'], 8)}`\n"
            f"*Open Rate:* `{fmt_coin2(msg['open_rate'], msg['quote_currency'])}`\n"
        )
        if msg["type"] == RPCMessageType.EXIT and msg["current_rate"]:
            message += (
                f"*Current Rate:* `{fmt_coin2(msg['current_rate'], msg['quote_currency'])}`\n"
            )
            if msg["order_rate"]:
                message += f"*Exit Rate:* `{fmt_coin2(msg['order_rate'], msg['quote_currency'])}`"
        elif msg["type"] == RPCMessageType.EXIT_FILL:
            message += f"*Exit Rate:* `{fmt_coin2(msg['close_rate'], msg['quote_currency'])}`"

        if is_sub_trade:
            stake_amount_fiat = self.__format_profit_fiat(msg, "stake_amount")

            rem = fmt_coin(msg["stake_amount"], msg["quote_currency"])
            message += f"\n*Remaining:* `{rem}{stake_amount_fiat}`"
        else:
            message += f"\n*Duration:* `{duration} ({duration_min:.1f} min)`"
        return message

    def __format_profit_fiat(
        self, msg: RPCExitMsg, key: Literal["stake_amount", "profit_amount", "cumulative_profit"]
    ) -> str:
        """
        Format Fiat currency to append to regular profit output
        """
        profit_fiat_extra = ""
        if self._rpc._fiat_converter and (fiat_currency := msg.get("fiat_currency")):
            profit_fiat = self._rpc._fiat_converter.convert_amount(
                msg[key], msg["stake_currency"], fiat_currency
            )
            profit_fiat_extra = f" / {profit_fiat:.3f} {fiat_currency}"
        return profit_fiat_extra

    def _normalize_roi_keys(self, roi: dict) -> dict:
        """
        Convert ROI dictionary keys to integers.
        JSON config files store ROI keys as strings, but the strategy expects integer keys.
        """
        if not roi:
            return {}
        return {int(k): v for k, v in roi.items()}

    def compose_message(self, msg: RPCSendMsg) -> str | None:
        if msg["type"] == RPCMessageType.ENTRY or msg["type"] == RPCMessageType.ENTRY_FILL:
            message = self._format_entry_msg(msg)

        elif msg["type"] == RPCMessageType.EXIT or msg["type"] == RPCMessageType.EXIT_FILL:
            message = self._format_exit_msg(msg)

        elif (
            msg["type"] == RPCMessageType.ENTRY_CANCEL or msg["type"] == RPCMessageType.EXIT_CANCEL
        ):
            message_side = "enter" if msg["type"] == RPCMessageType.ENTRY_CANCEL else "exit"
            message = (
                f"\N{WARNING SIGN} *{self._exchange_from_msg(msg)}:* "
                f"Cancelling {'partial ' if msg.get('sub_trade') else ''}"
                f"{message_side} Order for {msg['pair']} "
                f"(#{msg['trade_id']}). Reason: {msg['reason']}."
            )

        elif msg["type"] == RPCMessageType.PROTECTION_TRIGGER:
            message = (
                f"*Protection* triggered due to {msg['reason']}. "
                f"`{msg['pair']}` will be locked until `{msg['lock_end_time']}`."
            )

        elif msg["type"] == RPCMessageType.PROTECTION_TRIGGER_GLOBAL:
            message = (
                f"*Protection* triggered due to {msg['reason']}. "
                f"*All pairs* will be locked until `{msg['lock_end_time']}`."
            )

        elif msg["type"] == RPCMessageType.STATUS:
            message = f"*Status:* `{msg['status']}`"

        elif msg["type"] == RPCMessageType.WARNING:
            message = f"\N{WARNING SIGN} *Warning:* `{msg['status']}`"
        elif msg["type"] == RPCMessageType.EXCEPTION:
            # Errors will contain exceptions, which are wrapped in triple ticks.
            message = f"\N{WARNING SIGN} *ERROR:* \n {msg['status']}"

        elif msg["type"] == RPCMessageType.STARTUP:
            message = f"{msg['status']}"
        elif msg["type"] == RPCMessageType.STRATEGY_MSG:
            message = f"{msg['msg']}"
        else:
            logger.debug("Unknown message type: %s", msg["type"])
            return None
        return message

    def _message_loudness(self, msg: RPCSendMsg) -> str:
        """Determine the loudness of the message - on, off or silent"""
        default_noti = "on"

        msg_type = msg["type"]
        noti = ""
        if msg["type"] == RPCMessageType.EXIT or msg["type"] == RPCMessageType.EXIT_FILL:
            sell_noti = (
                self._config["telegram"].get("notification_settings", {}).get(str(msg_type), {})
            )

            # For backward compatibility sell still can be string
            if isinstance(sell_noti, str):
                noti = sell_noti
            else:
                default_noti = sell_noti.get("*", default_noti)
                noti = sell_noti.get(str(msg["exit_reason"]), default_noti)
        else:
            noti = (
                self._config["telegram"]
                .get("notification_settings", {})
                .get(str(msg_type), default_noti)
            )

        return noti

    def send_msg(self, msg: RPCSendMsg) -> None:
        """Send a message to telegram channel"""
        noti = self._message_loudness(msg)

        if noti == "off":
            logger.info(f"Notification '{msg['type']}' not sent.")
            # Notification disabled
            return

        message = self.compose_message(deepcopy(msg))
        if message:
            asyncio.run_coroutine_threadsafe(
                self._send_msg(message, disable_notification=(noti == "silent")), self._loop
            )

    def _get_exit_emoji(self, msg):
        """
        Get emoji for exit-messages
        """

        if float(msg["profit_ratio"]) >= 0.05:
            return "\N{ROCKET}"
        elif float(msg["profit_ratio"]) >= 0.0:
            return "\N{EIGHT SPOKED ASTERISK}"
        elif msg["exit_reason"] == "stop_loss":
            return "\N{WARNING SIGN}"
        else:
            return "\N{CROSS MARK}"

    def _prepare_order_details(self, filled_orders: list, quote_currency: str, is_open: bool):
        """
        Prepare details of trade with entry adjustment enabled
        """
        lines_detail: list[str] = []
        if len(filled_orders) > 0:
            first_avg = filled_orders[0]["safe_price"]
        order_nr = 0
        for order in filled_orders:
            lines: list[str] = []
            if order["is_open"] is True:
                continue
            order_nr += 1
            wording = "Entry" if order["ft_is_entry"] else "Exit"

            cur_entry_amount = order["filled"] or order["amount"]
            cur_entry_average = order["safe_price"]
            lines.append("  ")
            lines.append(f"*{wording} #{order_nr}:*")
            if order_nr == 1:
                lines.append(
                    f"*Amount:* {round_value(cur_entry_amount, 8)} "
                    f"({fmt_coin(order['cost'], quote_currency)})"
                )
                lines.append(f"*Average Price:* {round_value(cur_entry_average, 8)}")
            else:
                # TODO: This calculation ignores fees.
                price_to_1st_entry = (cur_entry_average - first_avg) / first_avg
                if is_open:
                    lines.append("({})".format(dt_humanize_delta(order["order_filled_date"])))
                lines.append(
                    f"*Amount:* {round_value(cur_entry_amount, 8)} "
                    f"({fmt_coin(order['cost'], quote_currency)})"
                )
                lines.append(
                    f"*Average {wording} Price:* {round_value(cur_entry_average, 8)} "
                    f"({price_to_1st_entry:.2%} from 1st entry rate)"
                )
                lines.append(f"*Order Filled:* {order['order_filled_date']}")

            lines_detail.append("\n".join(lines))

        return lines_detail

    @authorized_only
    async def _order(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /order.
        Returns the orders of the trade
        :param bot: telegram bot
        :param update: message update
        :return: None
        """

        trade_ids = []
        if context.args and len(context.args) > 0:
            trade_ids = [int(i) for i in context.args if i.isnumeric()]

        results = self._rpc._rpc_trade_status(trade_ids=trade_ids)
        for r in results:
            lines = ["*Order List for Trade #*`{trade_id}`"]

            lines_detail = self._prepare_order_details(
                r["orders"], r["quote_currency"], r["is_open"]
            )
            lines.extend(lines_detail if lines_detail else "")
            await self.__send_order_msg(lines, r)

    async def __send_order_msg(self, lines: list[str], r: dict[str, Any]) -> None:
        """
        Send status message.
        """
        msg = ""

        for line in lines:
            if line:
                if (len(msg) + len(line) + 1) < MAX_MESSAGE_LENGTH:
                    msg += line + "\n"
                else:
                    await self._send_msg(msg.format(**r))
                    msg = "*Order List for Trade #*`{trade_id}` - continued\n" + line + "\n"

        await self._send_msg(msg.format(**r))

    @authorized_only
    async def _status(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /status.
        Returns the current TradeThread status
        :param bot: telegram bot
        :param update: message update
        :return: None
        """

        if context.args and "table" in context.args:
            await self._status_table(update, context)
            return
        else:
            await self._status_msg(update, context)

    async def _status_msg(self, update: Update, context: CallbackContext) -> None:
        """
        handler for `/status` and `/status <id>`.

        """
        # Check if there's at least one numerical ID provided.
        # If so, try to get only these trades.
        trade_ids = []
        if context.args and len(context.args) > 0:
            trade_ids = [int(i) for i in context.args if i.isnumeric()]

        results = self._rpc._rpc_trade_status(trade_ids=trade_ids)
        position_adjust = self._config.get("position_adjustment_enable", False)
        max_entries = self._config.get("max_entry_position_adjustment", -1)
        for r in results:
            r["open_date_hum"] = dt_humanize_delta(r["open_date"])
            r["num_entries"] = len([o for o in r["orders"] if o["ft_is_entry"]])
            r["num_exits"] = len(
                [
                    o
                    for o in r["orders"]
                    if not o["ft_is_entry"] and not o["ft_order_side"] == "stoploss"
                ]
            )
            r["exit_reason"] = r.get("exit_reason", "")
            r["stake_amount_r"] = fmt_coin(r["stake_amount"], r["quote_currency"])
            r["max_stake_amount_r"] = fmt_coin(
                r["max_stake_amount"] or r["stake_amount"], r["quote_currency"]
            )
            r["profit_abs_r"] = fmt_coin(r["profit_abs"], r["quote_currency"])
            r["realized_profit_r"] = fmt_coin(r["realized_profit"], r["quote_currency"])
            r["total_profit_abs_r"] = fmt_coin(r["total_profit_abs"], r["quote_currency"])
            lines = [
                "*Trade ID:* `{trade_id}`" + (" `(since {open_date_hum})`" if r["is_open"] else ""),
                "*Current Pair:* {pair}",
                (
                    f"*Direction:* {'`Short`' if r.get('is_short') else '`Long`'}"
                    + " ` ({leverage}x)`"
                    if r.get("leverage")
                    else ""
                ),
                "*Amount:* `{amount} ({stake_amount_r})`",
                "*Total invested:* `{max_stake_amount_r}`" if position_adjust else "",
                "*Enter Tag:* `{enter_tag}`" if r["enter_tag"] else "",
                "*Exit Reason:* `{exit_reason}`" if r["exit_reason"] else "",
            ]

            if position_adjust:
                max_buy_str = f"/{max_entries + 1}" if (max_entries > 0) else ""
                lines.extend(
                    [
                        "*Number of Entries:* `{num_entries}" + max_buy_str + "`",
                        "*Number of Exits:* `{num_exits}`",
                    ]
                )

            lines.extend(
                [
                    f"*Open Rate:* `{round_value(r['open_rate'], 8)}`",
                    f"*Close Rate:* `{round_value(r['close_rate'], 8)}`" if r["close_rate"] else "",
                    "*Open Date:* `{open_date}`",
                    "*Close Date:* `{close_date}`" if r["close_date"] else "",
                    (
                        f" \n*Current Rate:* `{round_value(r['current_rate'], 8)}`"
                        if r["is_open"]
                        else ""
                    ),
                    ("*Unrealized Profit:* " if r["is_open"] else "*Close Profit: *")
                    + "`{profit_ratio:.2%}` `({profit_abs_r})`",
                ]
            )

            if r["is_open"]:
                if r.get("realized_profit"):
                    lines.extend(
                        [
                            "*Realized Profit:* `{realized_profit_ratio:.2%} "
                            "({realized_profit_r})`",
                            "*Total Profit:* `{total_profit_ratio:.2%} ({total_profit_abs_r})`",
                        ]
                    )

                # Append empty line to improve readability
                lines.append(" ")
                if (
                    r["stop_loss_abs"] != r["initial_stop_loss_abs"]
                    and r["initial_stop_loss_ratio"] is not None
                ):
                    # Adding initial stoploss only if it is different from stoploss
                    lines.append(
                        "*Initial Stoploss:* `{initial_stop_loss_abs:.8f}` "
                        "`({initial_stop_loss_ratio:.2%})`"
                    )

                # Adding stoploss and stoploss percentage only if it is not None
                lines.append(
                    f"*Stoploss:* `{round_value(r['stop_loss_abs'], 8)}` "
                    + ("`({stop_loss_ratio:.2%})`" if r["stop_loss_ratio"] else "")
                )
                lines.append(
                    f"*Stoploss distance:* `{round_value(r['stoploss_current_dist'], 8)}` "
                    "`({stoploss_current_dist_ratio:.2%})`"
                )
                if r.get("open_orders"):
                    lines.append(
                        "*Open Order:* `{open_orders}`"
                        + ("- `{exit_order_status}`" if r["exit_order_status"] else "")
                    )

            await self.__send_status_msg(lines, r)

    async def __send_status_msg(self, lines: list[str], r: dict[str, Any]) -> None:
        """
        Send status message.
        """
        msg = ""

        for line in lines:
            if line:
                if (len(msg) + len(line) + 1) < MAX_MESSAGE_LENGTH:
                    msg += line + "\n"
                else:
                    await self._send_msg(msg.format(**r))
                    msg = "*Trade ID:* `{trade_id}` - continued\n" + line + "\n"

        await self._send_msg(msg.format(**r))

    @authorized_only
    async def _status_table(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /status table.
        Returns the current TradeThread status in table format
        :param bot: telegram bot
        :param update: message update
        :return: None
        """
        fiat_currency = self._config.get("fiat_display_currency", "")
        statlist, head, fiat_profit_sum, fiat_total_profit_sum = self._rpc._rpc_status_table(
            self._config["stake_currency"], fiat_currency
        )

        show_total = not isnan(fiat_profit_sum) and len(statlist) > 1
        show_total_realized = (
            not isnan(fiat_total_profit_sum) and len(statlist) > 1 and fiat_profit_sum
        ) != fiat_total_profit_sum
        max_trades_per_msg = 50
        """
        Calculate the number of messages of 50 trades per message
        0.99 is used to make sure that there are no extra (empty) messages
        As an example with 50 trades, there will be int(50/50 + 0.99) = 1 message
        """
        messages_count = max(int(len(statlist) / max_trades_per_msg + 0.99), 1)
        for i in range(0, messages_count):
            trades = statlist[i * max_trades_per_msg : (i + 1) * max_trades_per_msg]
            if show_total and i == messages_count - 1:
                # append total line
                trades.append(["Total", "", "", f"{fiat_profit_sum:.2f} {fiat_currency}"])
                if show_total_realized:
                    trades.append(
                        [
                            "Total",
                            "(incl. realized Profits)",
                            "",
                            f"{fiat_total_profit_sum:.2f} {fiat_currency}",
                        ]
                    )

            message = tabulate(trades, headers=head, tablefmt="simple")
            if show_total and i == messages_count - 1:
                # insert separators line between Total
                lines = message.split("\n")
                offset = 2 if show_total_realized else 1
                message = "\n".join(lines[:-offset] + [lines[1]] + lines[-offset:])
            await self._send_msg(
                f"<pre>{message}</pre>",
                parse_mode=ParseMode.HTML,
                reload_able=True,
                callback_path="update_status_table",
                query=update.callback_query,
            )

    async def _timeunit_stats(self, update: Update, context: CallbackContext, unit: str) -> None:
        """
        Handler for /daily <n>
        Returns a daily profit (in BTC) over the last n days.
        :param bot: telegram bot
        :param update: message update
        :return: None
        """

        vals = {
            "days": TimeunitMappings("Day", "Daily", "days", "update_daily", 7, "%Y-%m-%d"),
            "weeks": TimeunitMappings(
                "Monday", "Weekly", "weeks (starting from Monday)", "update_weekly", 8, "%Y-%m-%d"
            ),
            "months": TimeunitMappings("Month", "Monthly", "months", "update_monthly", 6, "%Y-%m"),
        }
        val = vals[unit]

        stake_cur = self._config["stake_currency"]
        fiat_disp_cur = self._config.get("fiat_display_currency", "")
        try:
            timescale = int(context.args[0]) if context.args else val.default
        except (TypeError, ValueError, IndexError):
            timescale = val.default
        stats = self._rpc._rpc_timeunit_profit(timescale, stake_cur, fiat_disp_cur, unit)
        stats_tab = tabulate(
            [
                [
                    f"{period['date']:{val.dateformat}} ({period['trade_count']})",
                    f"{fmt_coin(period['abs_profit'], stats['stake_currency'])}",
                    f"{period['fiat_value']:.2f} {stats['fiat_display_currency']}",
                    f"{period['rel_profit']:.2%}",
                ]
                for period in stats["data"]
            ],
            headers=[
                f"{val.header} (count)",
                f"{stake_cur}",
                f"{fiat_disp_cur}",
                "Profit %",
                "Trades",
            ],
            tablefmt="simple",
        )
        message = (
            f"<b>{val.message} Profit over the last {timescale} {val.message2}</b>:\n"
            f"<pre>{stats_tab}</pre>"
        )
        await self._send_msg(
            message,
            parse_mode=ParseMode.HTML,
            reload_able=True,
            callback_path=val.callback,
            query=update.callback_query,
        )

    @authorized_only
    async def _daily(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /daily <n>
        Returns a daily profit (in BTC) over the last n days.
        :param bot: telegram bot
        :param update: message update
        :return: None
        """
        await self._timeunit_stats(update, context, "days")

    @authorized_only
    async def _weekly(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /weekly <n>
        Returns a weekly profit (in BTC) over the last n weeks.
        :param bot: telegram bot
        :param update: message update
        :return: None
        """
        await self._timeunit_stats(update, context, "weeks")

    @authorized_only
    async def _monthly(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /monthly <n>
        Returns a monthly profit (in BTC) over the last n months.
        :param bot: telegram bot
        :param update: message update
        :return: None
        """
        await self._timeunit_stats(update, context, "months")

    def _format_profit_message(
        self,
        stats: dict,
        stake_cur: str,
        fiat_disp_cur: str,
        timescale: int | None = None,
        direction: str | None = None,
    ) -> str:
        """
        Format profit statistics message for telegram.

        :param stats: Trade statistics dictionary
        :param stake_cur: Stake currency
        :param fiat_disp_cur: Fiat display currency
        :param timescale: Optional timescale filter
        :param direction: Optional direction filter ('long', 'short', or None for all)
        :return: Formatted markdown message
        """
        # Extract common variables
        profit_closed_coin = stats["profit_closed_coin"]
        profit_closed_ratio_mean = stats["profit_closed_ratio_mean"]
        profit_closed_percent = stats["profit_closed_percent"]
        profit_closed_fiat = stats["profit_closed_fiat"]
        profit_all_coin = stats["profit_all_coin"]
        profit_all_ratio_mean = stats["profit_all_ratio_mean"]
        profit_all_percent = stats["profit_all_percent"]
        profit_all_fiat = stats["profit_all_fiat"]
        trade_count = stats["trade_count"]
        first_trade_date = f"{stats['first_trade_humanized']} ({stats['first_trade_date']})"
        latest_trade_date = f"{stats['latest_trade_humanized']} ({stats['latest_trade_date']})"
        avg_duration = stats["avg_duration"]
        best_pair = stats["best_pair"]
        best_pair_profit_ratio = stats["best_pair_profit_ratio"]
        best_pair_profit_abs = fmt_coin(stats["best_pair_profit_abs"], stake_cur)
        winrate = stats["winrate"]
        expectancy = stats["expectancy"]
        expectancy_ratio = stats["expectancy_ratio"]

        # Direction-specific labels
        direction_label = f" {direction}" if direction else ""
        no_trades_msg = (
            f"No{direction_label} trades yet.\n*Bot started:* `{stats['bot_start_date']}`"
        )
        no_closed_msg = f"`No closed{direction_label} trade` \n"
        closed_roi_label = f"*ROI:* Closed{direction_label} trades"
        all_roi_label = f"*ROI:* All{direction_label} trades"

        if stats["trade_count"] == 0:
            return no_trades_msg

        # Build message
        if stats["closed_trade_count"] > 0:
            fiat_closed_trades = (
                f"∙ `{fmt_coin(profit_closed_fiat, fiat_disp_cur)}`\n" if fiat_disp_cur else ""
            )
            markdown_msg = (
                f"{closed_roi_label}\n"
                f"∙ `{fmt_coin(profit_closed_coin, stake_cur)} "
                f"({profit_closed_ratio_mean:.2%}) "
                f"({profit_closed_percent} \N{GREEK CAPITAL LETTER SIGMA}%)`\n"
                f"{fiat_closed_trades}"
            )
        else:
            markdown_msg = no_closed_msg

        fiat_all_trades = (
            f"∙ `{fmt_coin(profit_all_fiat, fiat_disp_cur)}`\n" if fiat_disp_cur else ""
        )
        markdown_msg += (
            f"{all_roi_label}\n"
            f"∙ `{fmt_coin(profit_all_coin, stake_cur)} "
            f"({profit_all_ratio_mean:.2%}) "
            f"({profit_all_percent} \N{GREEK CAPITAL LETTER SIGMA}%)`\n"
            f"{fiat_all_trades}"
            f"*Total Trade Count:* `{trade_count}`\n"
            f"*Bot started:* `{stats['bot_start_date']}`\n"
            f"*{'First Trade opened' if not timescale else 'Showing Profit since'}:* "
            f"`{first_trade_date}`\n"
            f"*Latest Trade opened:* `{latest_trade_date}`\n"
            f"*Win / Loss:* `{stats['winning_trades']} / {stats['losing_trades']}`\n"
            f"*Winrate:* `{winrate:.2%}`\n"
            f"*Expectancy (Ratio):* `{expectancy:.2f} ({expectancy_ratio:.2f})`"
        )

        if stats["closed_trade_count"] > 0:
            markdown_msg += (
                f"\n*Avg. Duration:* `{avg_duration}`\n"
                f"*Best Performing:* `{best_pair}: {best_pair_profit_abs} "
                f"({best_pair_profit_ratio:.2%})`\n"
                f"*Trading volume:* `{fmt_coin(stats['trading_volume'], stake_cur)}`\n"
                f"*Profit factor:* `{stats['profit_factor']:.2f}`\n"
                f"*Max Drawdown:* `{stats['max_drawdown']:.2%} "
                f"({fmt_coin(stats['max_drawdown_abs'], stake_cur)})`\n"
                f"    from `{stats['max_drawdown_start']} "
                f"({fmt_coin(stats['drawdown_high'], stake_cur)})`\n"
                f"    to `{stats['max_drawdown_end']} "
                f"({fmt_coin(stats['drawdown_low'], stake_cur)})`\n"
                f"*Current Drawdown:* `{stats['current_drawdown']:.2%} "
                f"({fmt_coin(stats['current_drawdown_abs'], stake_cur)})`\n"
                f"    from `{stats['current_drawdown_start']} "
                f"({fmt_coin(stats['current_drawdown_high'], stake_cur)})`\n"
            )

        return markdown_msg

    async def _profit_handler(
        self,
        update: Update,
        context: CallbackContext,
        direction: str | None = None,
    ) -> None:
        """
        Common handler for profit commands.

        :param update: Telegram update
        :param context: Callback context
        :param direction: Trade direction filter ('long', 'short', or None)
        :param callback_path: Callback path for message updates
        """
        stake_cur = self._config["stake_currency"]
        fiat_disp_cur = self._config.get("fiat_display_currency", "")

        start_date = datetime.fromtimestamp(0)
        timescale = None
        try:
            if context.args:
                if not direction:
                    arg = context.args[0].lower()
                    if arg in ("short", "long"):
                        direction = arg
                        context.args.pop(0)  # Remove direction from args
                timescale = int(context.args[0]) - 1
                today_start = datetime.combine(date.today(), datetime.min.time())
                start_date = today_start - timedelta(days=timescale)
        except (TypeError, ValueError, IndexError):
            pass

        # Get stats with optional direction filter
        stats_kwargs = {
            "stake_currency": stake_cur,
            "fiat_display_currency": fiat_disp_cur,
            "start_date": start_date,
        }
        if direction:
            stats_kwargs["direction"] = direction

        stats = self._rpc._rpc_trade_statistics(**stats_kwargs)
        markdown_msg = self._format_profit_message(
            stats, stake_cur, fiat_disp_cur, timescale, direction
        )

        await self._send_msg(
            markdown_msg,
            reload_able=True,
            callback_path="update_profit" if not direction else f"update_profit_{direction}",
            query=update.callback_query,
        )

    @authorized_only
    async def _profit(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /profit.
        Returns a cumulative profit statistics.
        :param bot: telegram bot
        :param update: message update
        :return: None
        """
        await self._profit_handler(update, context)

    @authorized_only
    async def _profit_long(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /profit_long.
        Returns cumulative profit statistics for long trades.
        """
        await self._profit_handler(update, context, direction="long")

    @authorized_only
    async def _profit_short(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /profit_short.
        Returns cumulative profit statistics for short trades.
        """
        await self._profit_handler(update, context, direction="short")

    @authorized_only
    async def _stats(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /stats
        Show stats of recent trades
        """
        stats = self._rpc._rpc_stats()

        reason_map = {
            "roi": "ROI",
            "stop_loss": "Stoploss",
            "trailing_stop_loss": "Trail. Stop",
            "stoploss_on_exchange": "Stoploss",
            "exit_signal": "Exit Signal",
            "force_exit": "Force Exit",
            "emergency_exit": "Emergency Exit",
        }
        exit_reasons_tabulate = [
            [reason_map.get(reason, reason), sum(count.values()), count["wins"], count["losses"]]
            for reason, count in stats["exit_reasons"].items()
        ]
        exit_reasons_msg = "No trades yet."
        for reason in chunks(exit_reasons_tabulate, 25):
            exit_reasons_msg = tabulate(reason, headers=["Exit Reason", "Exits", "Wins", "Losses"])
            if len(exit_reasons_tabulate) > 25:
                await self._send_msg(f"```\n{exit_reasons_msg}```", ParseMode.MARKDOWN)
                exit_reasons_msg = ""

        durations = stats["durations"]
        duration_msg = tabulate(
            [
                [
                    "Wins",
                    (
                        str(timedelta(seconds=durations["wins"]))
                        if durations["wins"] is not None
                        else "N/A"
                    ),
                ],
                [
                    "Losses",
                    (
                        str(timedelta(seconds=durations["losses"]))
                        if durations["losses"] is not None
                        else "N/A"
                    ),
                ],
            ],
            headers=["", "Avg. Duration"],
        )
        msg = f"""```\n{exit_reasons_msg}```\n```\n{duration_msg}```"""

        await self._send_msg(msg, ParseMode.MARKDOWN)

    @authorized_only
    async def _balance(self, update: Update, context: CallbackContext) -> None:
        """Handler for /balance"""
        full_result = context.args and "full" in context.args
        result = self._rpc._rpc_balance(
            self._config["stake_currency"], self._config.get("fiat_display_currency", "")
        )

        balance_dust_level = self._config["telegram"].get("balance_dust_level", 0.0)
        if not balance_dust_level:
            balance_dust_level = DUST_PER_COIN.get(self._config["stake_currency"], 1.0)

        output = ""
        if self._config["dry_run"]:
            output += "*Warning:* Simulated balances in Dry Mode.\n"
        starting_cap = fmt_coin(result["starting_capital"], self._config["stake_currency"])
        output += f"Starting capital: `{starting_cap}`"
        starting_cap_fiat = (
            fmt_coin(result["starting_capital_fiat"], self._config["fiat_display_currency"])
            if result["starting_capital_fiat"] > 0
            else ""
        )
        output += (f" `, {starting_cap_fiat}`.\n") if result["starting_capital_fiat"] > 0 else ".\n"

        total_dust_balance = 0
        total_dust_currencies = 0
        for curr in result["currencies"]:
            curr_output = ""
            if (curr["is_position"] or curr["est_stake"] > balance_dust_level) and (
                full_result or curr["is_bot_managed"]
            ):
                if curr["is_position"]:
                    curr_output = (
                        f"*{curr['currency']}:*\n"
                        f"\t`{curr['side']}: {curr['position']:.8f}`\n"
                        f"\t`Est. {curr['stake']}: "
                        f"{fmt_coin(curr['est_stake'], curr['stake'], False)}`\n"
                    )
                else:
                    est_stake = fmt_coin(
                        curr["est_stake" if full_result else "est_stake_bot"], curr["stake"], False
                    )

                    curr_output = (
                        f"*{curr['currency']}:*\n"
                        f"\t`Available: {curr['free']:.8f}`\n"
                        f"\t`Balance: {curr['balance']:.8f}`\n"
                        f"\t`Pending: {curr['used']:.8f}`\n"
                        f"\t`Bot Owned: {curr['bot_owned']:.8f}`\n"
                        f"\t`Est. {curr['stake']}: {est_stake}`\n"
                    )

            elif curr["est_stake"] <= balance_dust_level:
                total_dust_balance += curr["est_stake"]
                total_dust_currencies += 1

            # Handle overflowing message length
            if len(output + curr_output) >= MAX_MESSAGE_LENGTH:
                await self._send_msg(output)
                output = curr_output
            else:
                output += curr_output

        if total_dust_balance > 0:
            output += (
                f"*{total_dust_currencies} Other "
                f"{plural(total_dust_currencies, 'Currency', 'Currencies')} "
                f"(< {balance_dust_level} {result['stake']}):*\n"
                f"\t`Est. {result['stake']}: "
                f"{fmt_coin(total_dust_balance, result['stake'], False)}`\n"
            )
        tc = result["trade_count"] > 0
        stake_improve = f" `({result['starting_capital_ratio']:.2%})`" if tc else ""
        fiat_val = f" `({result['starting_capital_fiat_ratio']:.2%})`" if tc else ""
        value = fmt_coin(result["value" if full_result else "value_bot"], result["symbol"], False)
        total_stake = fmt_coin(
            result["total" if full_result else "total_bot"], result["stake"], False
        )
        fiat_estimated_value = (
            f"\t`{result['symbol']}: {value}`{fiat_val}\n" if result["symbol"] else ""
        )
        output += (
            f"\n*Estimated Value{' (Bot managed assets only)' if not full_result else ''}*:\n"
            f"\t`{result['stake']}: {total_stake}`{stake_improve}\n"
            f"{fiat_estimated_value}"
        )
        await self._send_msg(
            output, reload_able=True, callback_path="update_balance", query=update.callback_query
        )

    @authorized_only
    async def _start(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /start.
        Starts TradeThread
        :param bot: telegram bot
        :param update: message update
        :return: None
        """
        msg = self._rpc._rpc_start()
        await self._send_msg(f"Status: `{msg['status']}`")

    @authorized_only
    async def _stop(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /stop.
        Stops TradeThread
        :param bot: telegram bot
        :param update: message update
        :return: None
        """
        msg = self._rpc._rpc_stop()
        await self._send_msg(f"Status: `{msg['status']}`")

    @authorized_only
    async def _reload_config(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /reload_config.
        Triggers a config file reload
        :param bot: telegram bot
        :param update: message update
        :return: None
        """
        msg = self._rpc._rpc_reload_config()
        await self._send_msg(f"Status: `{msg['status']}`")

    @authorized_only
    async def _pause(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /stop_buy /stop_entry and /pause.
        Sets bot state to paused
        :param bot: telegram bot
        :param update: message update
        :return: None
        """
        msg = self._rpc._rpc_pause()
        await self._send_msg(f"Status: `{msg['status']}`")

    @authorized_only
    async def _reload_trade_from_exchange(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /reload_trade <tradeid>.
        """
        if not context.args or len(context.args) == 0:
            raise RPCException("Trade-id not set.")
        trade_id = int(context.args[0])
        msg = self._rpc._rpc_reload_trade_from_exchange(trade_id)
        await self._send_msg(f"Status: `{msg['status']}`")

    @authorized_only
    async def _force_exit(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /forceexit <id>.
        Sells the given trade at current price
        :param bot: telegram bot
        :param update: message update
        :return: None
        """

        if context.args:
            trade_id = context.args[0]
            await self._force_exit_action(trade_id)
        else:
            fiat_currency = self._config.get("fiat_display_currency", "")
            try:
                statlist, _, _, _ = self._rpc._rpc_status_table(
                    self._config["stake_currency"], fiat_currency
                )
            except RPCException:
                await self._send_msg(msg="No open trade found.")
                return
            trades = []
            for trade in statlist:
                trades.append((trade[0], f"{trade[0]} {trade[1]} {trade[2]} {trade[3]}"))

            trade_buttons = [
                InlineKeyboardButton(text=trade[1], callback_data=f"force_exit__{trade[0]}")
                for trade in trades
            ]
            buttons_aligned = self._layout_inline_keyboard(trade_buttons, cols=1)

            buttons_aligned.append(
                [InlineKeyboardButton(text="Cancel", callback_data="force_exit__cancel")]
            )
            await self._send_msg(msg="Which trade?", keyboard=buttons_aligned)

    async def _force_exit_action(self, trade_id: str):
        if trade_id != "cancel":
            try:
                loop = asyncio.get_running_loop()
                # Workaround to avoid nested loops
                await loop.run_in_executor(None, safe_async_db(self._rpc._rpc_force_exit), trade_id)
            except RPCException as e:
                await self._send_msg(str(e))

    async def _force_exit_inline(self, update: Update, _: CallbackContext) -> None:
        if update.callback_query:
            query = update.callback_query
            if query.data and "__" in query.data:
                # Input data is "force_exit__<tradid|cancel>"
                trade_id = query.data.split("__")[1].split(" ")[0]
                if trade_id == "cancel":
                    await query.answer()
                    await query.edit_message_text(text="Force exit canceled.")
                    return
                trade: Trade | None = Trade.get_trades(trade_filter=Trade.id == trade_id).first()
                await query.answer()
                if trade:
                    await query.edit_message_text(
                        text=f"Manually exiting Trade #{trade_id}, {trade.pair}"
                    )
                    await self._force_exit_action(trade_id)
                else:
                    await query.edit_message_text(text=f"Trade {trade_id} not found.")

    async def _force_enter_action(self, pair, price: float | None, order_side: SignalDirection):
        if pair != "cancel":
            try:

                @safe_async_db
                def _force_enter():
                    self._rpc._rpc_force_entry(pair, price, order_side=order_side)

                loop = asyncio.get_running_loop()
                # Workaround to avoid nested loops
                await loop.run_in_executor(None, _force_enter)
            except RPCException as e:
                logger.exception("Forcebuy error!")
                await self._send_msg(str(e), ParseMode.HTML)

    async def _force_enter_inline(self, update: Update, _: CallbackContext) -> None:
        if update.callback_query:
            query = update.callback_query
            if query.data and "__" in query.data:
                # Input data is "force_enter__<pair|cancel>_<side>"
                payload = query.data.split("__")[1]
                if payload == "cancel":
                    await query.answer()
                    await query.edit_message_text(text="Force enter canceled.")
                    return
                if payload and "_||_" in payload:
                    pair, side = payload.split("_||_")
                    order_side = SignalDirection(side)
                    await query.answer()
                    await query.edit_message_text(text=f"Manually entering {order_side} for {pair}")
                    await self._force_enter_action(pair, None, order_side)

    @staticmethod
    def _layout_inline_keyboard(
        buttons: list[InlineKeyboardButton], cols=3
    ) -> list[list[InlineKeyboardButton]]:
        return [buttons[i : i + cols] for i in range(0, len(buttons), cols)]

    @authorized_only
    async def _force_enter(
        self, update: Update, context: CallbackContext, order_side: SignalDirection
    ) -> None:
        """
        Handler for /forcelong <asset> <price> and `/forceshort <asset> <price>
        Buys a pair trade at the given or current price
        :param bot: telegram bot
        :param update: message update
        :return: None
        """
        if context.args:
            pair = context.args[0]
            price = float(context.args[1]) if len(context.args) > 1 else None
            await self._force_enter_action(pair, price, order_side)
        else:
            whitelist = self._rpc._rpc_whitelist()["whitelist"]
            pair_buttons = [
                InlineKeyboardButton(
                    text=pair, callback_data=f"force_enter__{pair}_||_{order_side}"
                )
                for pair in sorted(whitelist)
            ]
            buttons_aligned = self._layout_inline_keyboard(pair_buttons)

            buttons_aligned.append(
                [InlineKeyboardButton(text="Cancel", callback_data="force_enter__cancel")]
            )
            await self._send_msg(
                msg="Which pair?", keyboard=buttons_aligned, query=update.callback_query
            )

    @authorized_only
    async def _longall(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /longall - Long all coins in whitelist
        """
        whitelist = self._rpc._rpc_whitelist()["whitelist"]
        if not whitelist:
            await self._send_msg("No pairs in whitelist")
            return

        # Show confirmation dialog
        msg = f"⚠️ **Confirm Long All**\n\n"
        msg += f"Open long positions for **{len(whitelist)} pairs**?\n\n"
        msg += f"Pairs: {', '.join(whitelist[:5])}"
        if len(whitelist) > 5:
            msg += f" and {len(whitelist) - 5} more..."

        keyboard = [
            [
                InlineKeyboardButton(text="✅ Yes", callback_data="confirm_longall__yes"),
                InlineKeyboardButton(text="❌ No", callback_data="confirm_longall__no"),
            ]
        ]
        await self._send_msg(msg, keyboard=keyboard, parse_mode=ParseMode.MARKDOWN)

    async def _longall_callback(self, update: Update, context: CallbackContext) -> None:
        """Callback handler for /longall confirmation"""
        if update.callback_query:
            query = update.callback_query
            await query.answer()

            if query.data and "__" in query.data:
                action = query.data.split("__")[1]

                if action == "no":
                    await query.edit_message_text(text="❌ Long all canceled.")
                    return

                # User confirmed - execute longall
                await query.edit_message_text(text="⏳ Opening long positions...")

                whitelist = self._rpc._rpc_whitelist()["whitelist"]
                success_pairs = []
                failed_pairs = []

                for pair in whitelist:
                    try:

                        @safe_async_db
                        def _force_long():
                            self._rpc._rpc_force_entry(
                                pair, None, order_side=SignalDirection.LONG
                            )

                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(None, _force_long)
                        success_pairs.append(pair)
                    except RPCException as e:
                        failed_pairs.append(f"{pair}: {str(e)}")

                msg = ""
                if success_pairs:
                    msg += f"✅ Opened long positions for: {', '.join(success_pairs)}\n"
                if failed_pairs:
                    msg += f"❌ Failed to open:\n" + "\n".join(failed_pairs)

                await query.edit_message_text(text=msg or "No positions opened")

    @authorized_only
    async def _shortall(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /shortall - Short all coins in whitelist
        """
        whitelist = self._rpc._rpc_whitelist()["whitelist"]
        if not whitelist:
            await self._send_msg("No pairs in whitelist")
            return

        # Show confirmation dialog
        msg = f"⚠️ **Confirm Short All**\n\n"
        msg += f"Open short positions for **{len(whitelist)} pairs**?\n\n"
        msg += f"Pairs: {', '.join(whitelist[:5])}"
        if len(whitelist) > 5:
            msg += f" and {len(whitelist) - 5} more..."

        keyboard = [
            [
                InlineKeyboardButton(text="✅ Yes", callback_data="confirm_shortall__yes"),
                InlineKeyboardButton(text="❌ No", callback_data="confirm_shortall__no"),
            ]
        ]
        await self._send_msg(msg, keyboard=keyboard, parse_mode=ParseMode.MARKDOWN)

    async def _shortall_callback(self, update: Update, context: CallbackContext) -> None:
        """Callback handler for /shortall confirmation"""
        if update.callback_query:
            query = update.callback_query
            await query.answer()

            if query.data and "__" in query.data:
                action = query.data.split("__")[1]

                if action == "no":
                    await query.edit_message_text(text="❌ Short all canceled.")
                    return

                # User confirmed - execute shortall
                await query.edit_message_text(text="⏳ Opening short positions...")

                whitelist = self._rpc._rpc_whitelist()["whitelist"]
                success_pairs = []
                failed_pairs = []

                for pair in whitelist:
                    try:

                        @safe_async_db
                        def _force_short():
                            self._rpc._rpc_force_entry(
                                pair, None, order_side=SignalDirection.SHORT
                            )

                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(None, _force_short)
                        success_pairs.append(pair)
                    except RPCException as e:
                        failed_pairs.append(f"{pair}: {str(e)}")

                msg = ""
                if success_pairs:
                    msg += f"✅ Opened short positions for: {', '.join(success_pairs)}\n"
                if failed_pairs:
                    msg += f"❌ Failed to open:\n" + "\n".join(failed_pairs)

                await query.edit_message_text(text=msg or "No positions opened")

    @authorized_only
    async def _closelong(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /closelong - Close all long trades
        """
        trades = Trade.get_open_trades()
        long_trades = [t for t in trades if not t.is_short]

        if not long_trades:
            await self._send_msg("No open long trades to close")
            return

        # Show confirmation dialog
        msg = f"⚠️ **Confirm Close Long**\n\n"
        msg += f"Close **{len(long_trades)} long trades**?\n\n"
        trade_list = [f"{t.pair} (#{t.id})" for t in long_trades[:5]]
        msg += f"Trades: {', '.join(trade_list)}"
        if len(long_trades) > 5:
            msg += f" and {len(long_trades) - 5} more..."

        keyboard = [
            [
                InlineKeyboardButton(text="✅ Yes", callback_data="confirm_closelong__yes"),
                InlineKeyboardButton(text="❌ No", callback_data="confirm_closelong__no"),
            ]
        ]
        await self._send_msg(msg, keyboard=keyboard, parse_mode=ParseMode.MARKDOWN)

    async def _closelong_callback(self, update: Update, context: CallbackContext) -> None:
        """Callback handler for /closelong confirmation"""
        if update.callback_query:
            query = update.callback_query
            await query.answer()

            if query.data and "__" in query.data:
                action = query.data.split("__")[1]

                if action == "no":
                    await query.edit_message_text(text="❌ Close long canceled.")
                    return

                # User confirmed - execute closelong
                await query.edit_message_text(text="⏳ Closing long trades...")

                try:
                    loop = asyncio.get_running_loop()
                    msg = await loop.run_in_executor(None, safe_async_db(self._closelong_sync))
                    await query.edit_message_text(text=msg)
                except RPCException as e:
                    await query.edit_message_text(text=str(e))

    def _closelong_sync(self) -> str:
        """
        Synchronous helper for closing long trades
        """
        trades = Trade.get_open_trades()
        long_trades = [t for t in trades if not t.is_short]

        if not long_trades:
            return "No open long trades to close"

        # Use the bulk processing approach similar to _rpc_force_exit("all")
        closed_trades = []
        failed_trades = []

        with self._rpc._freqtrade._exit_lock:
            for trade in long_trades:
                try:
                    success = self._rpc._RPC__exec_force_exit(trade, None)
                    if success:
                        closed_trades.append(f"{trade.pair} (#{trade.id})")
                    else:
                        failed_trades.append(f"{trade.pair} (#{trade.id}): Failed to execute exit")
                except Exception as e:
                    failed_trades.append(f"{trade.pair} (#{trade.id}): {str(e)}")

            # Commit all changes at once and update wallets
            Trade.commit()
            self._rpc._freqtrade.wallets.update()

        msg = ""
        if closed_trades:
            msg += f"✅ Closed long trades: {', '.join(closed_trades)}\n"
        if failed_trades:
            msg += f"❌ Failed to close:\n" + "\n".join(failed_trades)

        return msg or "No trades closed"

    @authorized_only
    async def _closeshort(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /closeshort - Close all short trades
        """
        trades = Trade.get_open_trades()
        short_trades = [t for t in trades if t.is_short]

        if not short_trades:
            await self._send_msg("No open short trades to close")
            return

        # Show confirmation dialog
        msg = f"⚠️ **Confirm Close Short**\n\n"
        msg += f"Close **{len(short_trades)} short trades**?\n\n"
        trade_list = [f"{t.pair} (#{t.id})" for t in short_trades[:5]]
        msg += f"Trades: {', '.join(trade_list)}"
        if len(short_trades) > 5:
            msg += f" and {len(short_trades) - 5} more..."

        keyboard = [
            [
                InlineKeyboardButton(text="✅ Yes", callback_data="confirm_closeshort__yes"),
                InlineKeyboardButton(text="❌ No", callback_data="confirm_closeshort__no"),
            ]
        ]
        await self._send_msg(msg, keyboard=keyboard, parse_mode=ParseMode.MARKDOWN)

    async def _closeshort_callback(self, update: Update, context: CallbackContext) -> None:
        """Callback handler for /closeshort confirmation"""
        if update.callback_query:
            query = update.callback_query
            await query.answer()

            if query.data and "__" in query.data:
                action = query.data.split("__")[1]

                if action == "no":
                    await query.edit_message_text(text="❌ Close short canceled.")
                    return

                # User confirmed - execute closeshort
                await query.edit_message_text(text="⏳ Closing short trades...")

                try:
                    loop = asyncio.get_running_loop()
                    msg = await loop.run_in_executor(None, safe_async_db(self._closeshort_sync))
                    await query.edit_message_text(text=msg)
                except RPCException as e:
                    await query.edit_message_text(text=str(e))

    def _closeshort_sync(self) -> str:
        """
        Synchronous helper for closing short trades
        """
        trades = Trade.get_open_trades()
        short_trades = [t for t in trades if t.is_short]

        if not short_trades:
            return "No open short trades to close"

        # Use the bulk processing approach similar to _rpc_force_exit("all")
        closed_trades = []
        failed_trades = []

        with self._rpc._freqtrade._exit_lock:
            for trade in short_trades:
                try:
                    success = self._rpc._RPC__exec_force_exit(trade, None)
                    if success:
                        closed_trades.append(f"{trade.pair} (#{trade.id})")
                    else:
                        failed_trades.append(f"{trade.pair} (#{trade.id}): Failed to execute exit")
                except Exception as e:
                    failed_trades.append(f"{trade.pair} (#{trade.id}): {str(e)}")

            # Commit all changes at once and update wallets
            Trade.commit()
            self._rpc._freqtrade.wallets.update()

        msg = ""
        if closed_trades:
            msg += f"✅ Closed short trades: {', '.join(closed_trades)}\n"
        if failed_trades:
            msg += f"❌ Failed to close:\n" + "\n".join(failed_trades)

        return msg or "No trades closed"

    @authorized_only
    async def _trades(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /trades <n>
        Returns last n recent trades.
        :param bot: telegram bot
        :param update: message update
        :return: None
        """
        stake_cur = self._config["stake_currency"]
        try:
            nrecent = int(context.args[0]) if context.args else 10
        except (TypeError, ValueError, IndexError):
            nrecent = 10
        nonspot = self._config.get("trading_mode", TradingMode.SPOT) != TradingMode.SPOT
        trades = self._rpc._rpc_trade_history(nrecent)
        trades_tab = tabulate(
            [
                [
                    dt_humanize_delta(dt_from_ts(trade["close_timestamp"])),
                    f"{trade['pair']} (#{trade['trade_id']}"
                    f"{(' ' + ('S' if trade['is_short'] else 'L')) if nonspot else ''})",
                    f"{(trade['close_profit']):.2%} ({trade['close_profit_abs']})",
                ]
                for trade in trades["trades"]
            ],
            headers=[
                "Close Date",
                "Pair (ID L/S)" if nonspot else "Pair (ID)",
                f"Profit ({stake_cur})",
            ],
            tablefmt="simple",
        )
        message = f"<b>{min(trades['trades_count'], nrecent)} recent trades</b>:\n" + (
            f"<pre>{trades_tab}</pre>" if trades["trades_count"] > 0 else ""
        )
        await self._send_msg(message, parse_mode=ParseMode.HTML)

    @authorized_only
    async def _delete_trade(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /delete <id>.
        Delete the given trade
        :param bot: telegram bot
        :param update: message update
        :return: None
        """
        if not context.args or len(context.args) == 0:
            raise RPCException("Trade-id not set.")
        trade_id = int(context.args[0])
        msg = self._rpc._rpc_delete(trade_id)
        await self._send_msg(
            f"{msg['result_msg']}\n"
            "Please make sure to take care of this asset on the exchange manually."
        )

    @authorized_only
    async def _cancel_open_order(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /cancel_open_order <id>.
        Cancel open order for tradeid
        :param bot: telegram bot
        :param update: message update
        :return: None
        """
        if not context.args or len(context.args) == 0:
            raise RPCException("Trade-id not set.")
        trade_id = int(context.args[0])
        self._rpc._rpc_cancel_open_order(trade_id)
        await self._send_msg("Open order canceled.")

    @authorized_only
    async def _performance(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /performance.
        Shows a performance statistic from finished trades
        :param bot: telegram bot
        :param update: message update
        :return: None
        """
        trades = self._rpc._rpc_performance()
        output = "<b>Performance:</b>\n"
        for i, trade in enumerate(trades):
            stat_line = (
                f"{i + 1}.\t <code>{trade['pair']}\t"
                f"{fmt_coin(trade['profit_abs'], self._config['stake_currency'])} "
                f"({trade['profit_ratio']:.2%}) "
                f"({trade['count']})</code>\n"
            )

            if len(output + stat_line) >= MAX_MESSAGE_LENGTH:
                await self._send_msg(output, parse_mode=ParseMode.HTML)
                output = stat_line
            else:
                output += stat_line

        await self._send_msg(
            output,
            parse_mode=ParseMode.HTML,
            reload_able=True,
            callback_path="update_performance",
            query=update.callback_query,
        )

    @authorized_only
    async def _enter_tag_performance(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /entries PAIR .
        Shows a performance statistic from finished trades
        :param bot: telegram bot
        :param update: message update
        :return: None
        """
        pair = None
        if context.args and isinstance(context.args[0], str):
            pair = context.args[0]

        trades = self._rpc._rpc_enter_tag_performance(pair)
        output = "*Entry Tag Performance:*\n"
        for i, trade in enumerate(trades):
            stat_line = (
                f"{i + 1}.\t `{trade['enter_tag']}\t"
                f"{fmt_coin(trade['profit_abs'], self._config['stake_currency'])} "
                f"({trade['profit_ratio']:.2%}) "
                f"({trade['count']})`\n"
            )

            if len(output + stat_line) >= MAX_MESSAGE_LENGTH:
                await self._send_msg(output, parse_mode=ParseMode.MARKDOWN)
                output = stat_line
            else:
                output += stat_line

        await self._send_msg(
            output,
            parse_mode=ParseMode.MARKDOWN,
            reload_able=True,
            callback_path="update_enter_tag_performance",
            query=update.callback_query,
        )

    @authorized_only
    async def _exit_reason_performance(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /exits.
        Shows a performance statistic from finished trades
        :param bot: telegram bot
        :param update: message update
        :return: None
        """
        pair = None
        if context.args and isinstance(context.args[0], str):
            pair = context.args[0]

        trades = self._rpc._rpc_exit_reason_performance(pair)
        output = "*Exit Reason Performance:*\n"
        for i, trade in enumerate(trades):
            stat_line = (
                f"{i + 1}.\t `{trade['exit_reason']}\t"
                f"{fmt_coin(trade['profit_abs'], self._config['stake_currency'])} "
                f"({trade['profit_ratio']:.2%}) "
                f"({trade['count']})`\n"
            )

            if len(output + stat_line) >= MAX_MESSAGE_LENGTH:
                await self._send_msg(output, parse_mode=ParseMode.MARKDOWN)
                output = stat_line
            else:
                output += stat_line

        await self._send_msg(
            output,
            parse_mode=ParseMode.MARKDOWN,
            reload_able=True,
            callback_path="update_exit_reason_performance",
            query=update.callback_query,
        )

    @authorized_only
    async def _mix_tag_performance(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /mix_tags.
        Shows a performance statistic from finished trades
        :param bot: telegram bot
        :param update: message update
        :return: None
        """
        pair = None
        if context.args and isinstance(context.args[0], str):
            pair = context.args[0]

        trades = self._rpc._rpc_mix_tag_performance(pair)
        output = "*Mix Tag Performance:*\n"
        for i, trade in enumerate(trades):
            stat_line = (
                f"{i + 1}.\t `{trade['mix_tag']}\t"
                f"{fmt_coin(trade['profit_abs'], self._config['stake_currency'])} "
                f"({trade['profit_ratio']:.2%}) "
                f"({trade['count']})`\n"
            )

            if len(output + stat_line) >= MAX_MESSAGE_LENGTH:
                await self._send_msg(output, parse_mode=ParseMode.MARKDOWN)
                output = stat_line
            else:
                output += stat_line

        await self._send_msg(
            output,
            parse_mode=ParseMode.MARKDOWN,
            reload_able=True,
            callback_path="update_mix_tag_performance",
            query=update.callback_query,
        )

    @authorized_only
    async def _count(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /count.
        Returns the number of trades running
        :param bot: telegram bot
        :param update: message update
        :return: None
        """
        counts = self._rpc._rpc_count()
        message = tabulate(
            {k: [v] for k, v in counts.items()},
            headers=["current", "max", "total stake"],
            tablefmt="simple",
        )
        message = f"<pre>{message}</pre>"
        logger.debug(message)
        await self._send_msg(
            message,
            parse_mode=ParseMode.HTML,
            reload_able=True,
            callback_path="update_count",
            query=update.callback_query,
        )

    @authorized_only
    async def _locks(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /locks.
        Returns the currently active locks
        """
        rpc_locks = self._rpc._rpc_locks()
        if not rpc_locks["locks"]:
            await self._send_msg("No active locks.", parse_mode=ParseMode.HTML)

        for locks in chunks(rpc_locks["locks"], 25):
            message = tabulate(
                [
                    [lock["id"], lock["pair"], lock["lock_end_time"], lock["reason"]]
                    for lock in locks
                ],
                headers=["ID", "Pair", "Until", "Reason"],
                tablefmt="simple",
            )
            message = f"<pre>{escape(message)}</pre>"
            logger.debug(message)
            await self._send_msg(message, parse_mode=ParseMode.HTML)

    @authorized_only
    async def _delete_locks(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /delete_locks.
        Returns the currently active locks
        """
        arg = context.args[0] if context.args and len(context.args) > 0 else None
        lockid = None
        pair = None
        if arg:
            try:
                lockid = int(arg)
            except ValueError:
                pair = arg

        self._rpc._rpc_delete_lock(lockid=lockid, pair=pair)
        await self._locks(update, context)

    @authorized_only
    async def _whitelist(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /whitelist
        Shows the currently active whitelist
        """
        whitelist = self._rpc._rpc_whitelist()

        if context.args:
            if "sorted" in context.args:
                whitelist["whitelist"] = sorted(whitelist["whitelist"])
            if "baseonly" in context.args:
                whitelist["whitelist"] = [pair.split("/")[0] for pair in whitelist["whitelist"]]

        message = f"Using whitelist `{whitelist['method']}` with {whitelist['length']} pairs\n"
        message += f"`{', '.join(whitelist['whitelist'])}`"

        logger.debug(message)
        await self._send_msg(message)

    @authorized_only
    async def _blacklist(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /blacklist
        Shows the currently active blacklist
        """
        await self.send_blacklist_msg(self._rpc._rpc_blacklist(context.args))

    async def send_blacklist_msg(self, blacklist: dict):
        errmsgs = []
        for _, error in blacklist["errors"].items():
            errmsgs.append(f"Error: {error['error_msg']}")
        if errmsgs:
            await self._send_msg("\n".join(errmsgs))

        message = f"Blacklist contains {blacklist['length']} pairs\n"
        message += f"`{', '.join(blacklist['blacklist'])}`"

        logger.debug(message)
        await self._send_msg(message)

    @authorized_only
    async def _blacklist_delete(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /bl_delete
        Deletes pair(s) from current blacklist
        """
        await self.send_blacklist_msg(self._rpc._rpc_blacklist_delete(context.args or []))

    @authorized_only
    async def _enable(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /enable - Enable trading entries for both long and short
        If arguments are provided, delegates to _enable_pairs for backward compatibility
        Usage: /enable (enables both long/short)
               /enable BTC/USDT ETH/USDT (enables specific pairs)
        """
        # Backward compatibility: if args provided, delegate to _enable_pairs
        if context.args:
            return await self._enable_pairs(update, context)
        
        # No args: enable both long and short entries
        old_dir = self._rpc._get_market_direction()
        
        if old_dir == MarketDirection.EVEN:
            await self._send_msg("✅ Both long and short entries are already enabled (market direction: even).")
            return
        
        self._rpc._update_market_direction(MarketDirection.EVEN)
        await self._send_msg(
            f"✅ Entries enabled for both long and short positions.\n"
            f"Market direction: *{old_dir}* → *{MarketDirection.EVEN}*"
        )

    @authorized_only
    async def _enable_pairs(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /enable_pairs - Enable (remove from blacklist) one or more pairs
        Usage: /enable_pairs BTC/USDT ETH/USDT
        """
        if not context.args:
            await self._send_msg(
                "Usage: `/enable_pairs <pair1> [pair2] ...`\nExample: `/enable_pairs BTC/USDT ETH/USDT`",
                ParseMode.MARKDOWN,
            )
            return

        pairs_to_enable = context.args
        enabled_pairs = []
        already_enabled = []
        invalid_pairs = []

        # Get current blacklist
        blacklist_data = self._rpc._rpc_blacklist()
        current_blacklist = blacklist_data.get("blacklist", [])

        # Get all valid pairs from exchange
        valid_pairs = list(self._rpc._freqtrade.exchange.get_markets().keys())

        for pair in pairs_to_enable:
            # Check if pair is valid
            if pair not in valid_pairs:
                # Try to match with wildcard patterns
                try:
                    from freqtrade.plugins.pairlist.pairlist_helpers import expand_pairlist

                    expanded = expand_pairlist([pair], valid_pairs)
                    if expanded:
                        # If it's a wildcard pattern, we need to handle each expanded pair
                        for expanded_pair in expanded:
                            if expanded_pair in current_blacklist:
                                enabled_pairs.append(expanded_pair)
                            else:
                                already_enabled.append(expanded_pair)
                    else:
                        invalid_pairs.append(pair)
                except:
                    invalid_pairs.append(pair)
            else:
                # Single pair case
                if pair in current_blacklist:
                    enabled_pairs.append(pair)
                else:
                    already_enabled.append(pair)

        # Remove pairs from blacklist
        if enabled_pairs:
            result = self._rpc._rpc_blacklist_delete(enabled_pairs)

        # Build response message
        msg = ""
        if enabled_pairs:
            msg += (
                f"✅ **Enabled pairs (removed from blacklist):**\n`{', '.join(enabled_pairs)}`\n\n"
            )
        if already_enabled:
            msg += f"ℹ️ **Already enabled pairs:**\n`{', '.join(already_enabled)}`\n\n"
        if invalid_pairs:
            msg += f"❌ **Invalid pairs:**\n`{', '.join(invalid_pairs)}`\n\n"

        if not msg:
            msg = "No changes made."
        else:
            # Show current blacklist status
            blacklist_data = self._rpc._rpc_blacklist()
            msg += f"📋 **Current blacklist:** {blacklist_data['length']} pairs"
            if blacklist_data["length"] > 0:
                msg += f"\n`{', '.join(blacklist_data['blacklist'][:10])}`"
                if blacklist_data["length"] > 10:
                    msg += f"\n... and {blacklist_data['length'] - 10} more"

        await self._send_msg(msg, ParseMode.MARKDOWN)

    @authorized_only
    async def _disable_pairs(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /disable_pairs - Disable (add to blacklist) one or more pairs
        Usage: /disable_pairs BTC/USDT ETH/USDT
        """
        if not context.args:
            await self._send_msg(
                "Usage: `/disable_pairs <pair1> [pair2] ...`\nExample: `/disable_pairs BTC/USDT ETH/USDT`",
                ParseMode.MARKDOWN,
            )
            return

        pairs_to_disable = context.args
        disabled_pairs = []
        already_disabled = []
        invalid_pairs = []

        # Add pairs to blacklist
        result = self._rpc._rpc_blacklist(pairs_to_disable)

        # Process results
        errors = result.get("errors", {})
        current_blacklist = result.get("blacklist", [])

        for pair in pairs_to_disable:
            if pair in errors:
                error_msg = errors[pair].get("error_msg", "")
                if "already in pairlist" in error_msg:
                    already_disabled.append(pair)
                elif "not a valid" in error_msg:
                    invalid_pairs.append(pair)
            else:
                disabled_pairs.append(pair)

        # Build response message
        msg = ""
        if disabled_pairs:
            msg += f"🚫 **Disabled pairs (added to blacklist):**\n`{', '.join(disabled_pairs)}`\n\n"
        if already_disabled:
            msg += f"ℹ️ **Already disabled pairs:**\n`{', '.join(already_disabled)}`\n\n"
        if invalid_pairs:
            msg += f"❌ **Invalid pairs:**\n`{', '.join(invalid_pairs)}`\n\n"

        if not msg:
            msg = "No changes made."
        else:
            # Show current blacklist status
            msg += f"📋 **Current blacklist:** {result['length']} pairs"
            if result["length"] > 0:
                msg += f"\n`{', '.join(current_blacklist[:10])}`"
                if result["length"] > 10:
                    msg += f"\n... and {result['length'] - 10} more"

        await self._send_msg(msg, ParseMode.MARKDOWN)

    # ====== SAFETY & RISK MANAGEMENT COMMANDS ======

    @authorized_only
    async def _setmaxopen(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /setmaxopen <number> - Dynamically adjust max open trades
        """
        if not context.args or len(context.args) != 1:
            await self._send_msg(
                "Usage: `/setmaxopen <number>`\nExample: `/setmaxopen 5`", ParseMode.MARKDOWN
            )
            return

        try:
            new_max = int(context.args[0])
            if new_max < 0:
                await self._send_msg("❌ Max open trades must be positive or 0")
                return

            old_max = self._config.get("max_open_trades", 0)
            self._config["max_open_trades"] = new_max
            self._rpc._freqtrade.config["max_open_trades"] = new_max

            current_open = len(Trade.get_open_trades())
            msg = f"✅ Max open trades changed from {old_max} to {new_max}\n"
            msg += f"Currently open: {current_open}/{new_max}"

            await self._send_msg(msg)
        except ValueError:
            await self._send_msg("❌ Invalid number. Please provide a valid integer.")

    @authorized_only
    async def _emergency_stop(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /emergency_stop - Instantly close all positions and stop bot
        """
        # First stop the bot
        self._rpc._rpc_stop()

        # Close all open trades using bulk processing
        trades = Trade.get_open_trades()
        closed_count = 0
        failed_count = 0

        if trades:
            with self._rpc._freqtrade._exit_lock:
                for trade in trades:
                    try:
                        success = self._rpc._RPC__exec_force_exit(trade, None)
                        if success:
                            closed_count += 1
                        else:
                            failed_count += 1
                    except Exception:
                        failed_count += 1

                # Commit all changes at once and update wallets
                Trade.commit()
                self._rpc._freqtrade.wallets.update()

        msg = "🚨 **EMERGENCY STOP EXECUTED**\n\n"
        msg += f"✅ Bot stopped\n"
        msg += f"✅ Closed {closed_count} trades\n"
        if failed_count > 0:
            msg += f"❌ Failed to close {failed_count} trades\n"
        msg += "\n⚠️ Bot is now stopped. Use /start to resume."

        await self._send_msg(msg, ParseMode.MARKDOWN)

    @authorized_only
    async def _pausefor(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /pausefor <minutes> - Pause bot for X minutes then auto-resume
        """
        if not context.args or len(context.args) != 1:
            await self._send_msg(
                "Usage: `/pausefor <minutes>`\nExample: `/pausefor 30`", ParseMode.MARKDOWN
            )
            return

        try:
            minutes = int(context.args[0])
            if minutes <= 0:
                await self._send_msg("❌ Minutes must be positive")
                return

            # Pause the bot
            self._rpc._rpc_stop_entry()

            # Schedule resume
            async def resume_bot():
                await asyncio.sleep(minutes * 60)
                self._rpc._rpc_start()
                await self._send_msg(f"🔄 Bot automatically resumed after {minutes} minutes")

            # Start the resume task in background
            asyncio.create_task(resume_bot())

            msg = f"⏸️ Bot paused for {minutes} minutes\n"
            msg += f"Will automatically resume at {(datetime.now() + timedelta(minutes=minutes)).strftime('%H:%M:%S')}"
            await self._send_msg(msg)

        except ValueError:
            await self._send_msg("❌ Invalid number. Please provide minutes as integer.")

    @authorized_only
    async def _risk_status(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /risk_status - Show current risk metrics
        """
        trades = Trade.get_open_trades()
        if not trades:
            await self._send_msg("📊 No open trades - No risk exposure")
            return

        stake_currency = self._config.get("stake_currency", "")
        total_stake = sum(trade.stake_amount for trade in trades)

        # Calculate current values and P&L
        current_values = []
        total_profit = 0
        for trade in trades:
            current_rate = self._rpc._freqtrade.exchange.get_rate(
                trade.pair, side="exit", is_short=trade.is_short, refresh=False
            )
            current_value = trade.amount * current_rate
            profit = trade.calc_profit_ratio(current_rate)
            total_profit += trade.calc_profit(current_rate)
            current_values.append((trade.pair, current_value, profit))

        # Calculate metrics
        balance = self._rpc._rpc_balance()
        total_balance = balance["total"]
        exposure_pct = (total_stake / total_balance * 100) if total_balance > 0 else 0

        # Find max drawdown
        max_loss = min(profit for _, _, profit in current_values) if current_values else 0

        msg = "📊 **Risk Status**\n\n"
        msg += (
            f"**Exposure:** {total_stake:.2f} {stake_currency} ({exposure_pct:.1f}% of balance)\n"
        )
        msg += (
            f"**Open Trades:** {len(trades)}/{self._config.get('max_open_trades', 'unlimited')}\n"
        )
        msg += f"**Current P&L:** {total_profit:.2f} {stake_currency}\n"
        msg += f"**Max Drawdown:** {max_loss:.2%}\n"
        msg += f"**Trading Mode:** {self._config.get('trading_mode', 'spot')}\n"

        if len(trades) > 0:
            msg += "\n**Per Trade Risk:**\n"
            for trade in trades[:5]:  # Show first 5
                profit_pct = trade.calc_profit_ratio(
                    self._rpc._freqtrade.exchange.get_rate(
                        trade.pair, side="exit", is_short=trade.is_short, refresh=False
                    )
                )
                msg += f"  {trade.pair}: {profit_pct:.2%}\n"

            if len(trades) > 5:
                msg += f"  ... and {len(trades) - 5} more\n"

        await self._send_msg(msg, ParseMode.MARKDOWN)

    @authorized_only
    async def _set_stoploss(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /set_stoploss <percent> - Adjust stop-loss for new trades
        """
        if not context.args or len(context.args) != 1:
            await self._send_msg(
                "Usage: `/set_stoploss <percent>`\nExample: `/set_stoploss -5` (for -5%)",
                ParseMode.MARKDOWN,
            )
            return

        try:
            new_stoploss = float(context.args[0])
            if new_stoploss > 0:
                new_stoploss = -new_stoploss  # Ensure it's negative

            if new_stoploss < -50:
                await self._send_msg("❌ Stop-loss cannot be more than -50%")
                return

            old_stoploss = self._config.get("stoploss", 0)
            self._config["stoploss"] = new_stoploss / 100  # Convert percentage to decimal
            self._rpc._freqtrade.strategy.stoploss = new_stoploss / 100

            msg = f"✅ Stop-loss changed from {old_stoploss * 100:.1f}% to {new_stoploss:.1f}%\n"
            msg += "⚠️ Note: Only affects new trades"

            await self._send_msg(msg)
        except ValueError:
            await self._send_msg("❌ Invalid number. Please provide a valid percentage.")

    # ====== QUICK STATUS & MONITORING COMMANDS ======

    @authorized_only
    async def _quickstatus(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /quickstatus or /qs - Compact one-line status
        """
        trades = Trade.get_open_trades()
        profit_sum = 0

        for trade in trades:
            current_rate = self._rpc._freqtrade.exchange.get_rate(
                trade.pair, side="exit", is_short=trade.is_short, refresh=False
            )
            profit_sum += trade.calc_profit(current_rate)

        stake_cur = self._config["stake_currency"]
        status = "running" if self._rpc._freqtrade.state == State.RUNNING else "stopped"

        msg = f"🤖 {status.upper()} | 📈 {len(trades)} trades | 💰 {profit_sum:.2f} {stake_cur}"
        await self._send_msg(msg)

    @authorized_only
    async def _alerts(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /alerts on/off - Toggle trade notifications
        """
        if not context.args or context.args[0] not in ["on", "off"]:
            await self._send_msg("Usage: `/alerts on` or `/alerts off`", ParseMode.MARKDOWN)
            return

        enable = context.args[0] == "on"
        self._config["telegram"]["notification_settings"] = {
            "buy": enable,
            "sell": enable,
            "buy_cancel": enable,
            "sell_cancel": enable,
            "buy_fill": enable,
            "sell_fill": enable,
        }

        status = "enabled 🔔" if enable else "disabled 🔕"
        await self._send_msg(f"Trade notifications {status}")

    @authorized_only
    async def _watchlist(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /watchlist - Show pairs being monitored for entry
        """
        whitelist = self._rpc._rpc_whitelist()["whitelist"]
        blacklist = self._rpc._rpc_blacklist()["blacklist"]

        # Get pairs that are in whitelist but not blacklisted
        active_pairs = [p for p in whitelist if p not in blacklist]

        # Get open trades
        open_trades = Trade.get_open_trades()
        open_pairs = [t.pair for t in open_trades]

        # Pairs being watched (in whitelist, not blacklisted, not in trade)
        watching = [p for p in active_pairs if p not in open_pairs]

        msg = f"👁️ **Watchlist** ({len(watching)} pairs)\n\n"
        if watching:
            msg += "**Available for entry:**\n"
            for pair in watching[:20]:  # Limit to 20 pairs
                msg += f"  • {pair}\n"
            if len(watching) > 20:
                msg += f"  ... and {len(watching) - 20} more\n"
        else:
            msg += "No pairs available for entry\n"

        msg += f"\n**Already in position:** {len(open_pairs)} pairs"

        await self._send_msg(msg, ParseMode.MARKDOWN)

    @authorized_only
    async def _pnl(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /pnl [today/week/month] - Quick P&L summary
        """
        period = context.args[0] if context.args else "today"

        if period not in ["today", "week", "month"]:
            period = "today"

        # Get the date range
        now = datetime.now()
        if period == "today":
            start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
            period_label = "Today"
        elif period == "week":
            start_date = now - timedelta(days=now.weekday())
            start_date = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
            period_label = "This Week"
        else:  # month
            start_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            period_label = "This Month"

        # Get trades
        trades = Trade.get_trades([Trade.close_date >= start_date, Trade.is_open.is_(False)]).all()

        # Calculate P&L
        total_profit = sum(t.close_profit_abs for t in trades if t.close_profit_abs)
        win_trades = [t for t in trades if t.close_profit_abs and t.close_profit_abs > 0]
        lose_trades = [t for t in trades if t.close_profit_abs and t.close_profit_abs < 0]

        stake_cur = self._config["stake_currency"]

        msg = f"📊 **{period_label} P&L**\n\n"
        msg += f"**Total:** {total_profit:.2f} {stake_cur}\n"
        msg += f"**Trades:** {len(trades)} (✅ {len(win_trades)} / ❌ {len(lose_trades)})\n"

        if trades:
            win_rate = len(win_trades) / len(trades) * 100
            msg += f"**Win Rate:** {win_rate:.1f}%\n"

            if win_trades:
                avg_win = sum(t.close_profit_abs for t in win_trades) / len(win_trades)
                msg += f"**Avg Win:** {avg_win:.2f} {stake_cur}\n"

            if lose_trades:
                avg_loss = sum(t.close_profit_abs for t in lose_trades) / len(lose_trades)
                msg += f"**Avg Loss:** {avg_loss:.2f} {stake_cur}\n"

        await self._send_msg(msg, ParseMode.MARKDOWN)

    # ====== BATCH OPERATIONS & PRESETS ======

    @authorized_only
    async def _preset_save(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /preset save|load <name> - Save/load presets
        """
        if not context.args or len(context.args) < 2:
            await self._send_msg(
                "Usage: `/preset save <name>` or `/preset load <name>`\nExample: `/preset save bull_market`",
                ParseMode.MARKDOWN,
            )
            return

        action = context.args[0]
        if action == "load":
            return await self._preset_load(update, context)
        elif action != "save":
            await self._send_msg(
                "Usage: `/preset save <name>` or `/preset load <name>`", ParseMode.MARKDOWN
            )
            return

        preset_name = context.args[1]

        # Create presets directory if not exists
        import os

        preset_dir = os.path.join(self._config["user_data_dir"], "presets")
        os.makedirs(preset_dir, exist_ok=True)

        # Save current configuration
        preset_data = {
            "whitelist": self._rpc._rpc_whitelist()["whitelist"],
            "blacklist": self._rpc._rpc_blacklist()["blacklist"],
            "max_open_trades": self._config.get("max_open_trades"),
            "stake_amount": self._config.get("stake_amount"),
            "stoploss": self._config.get("stoploss"),
            "timestamp": datetime.now().isoformat(),
        }

        preset_file = os.path.join(preset_dir, f"{preset_name}.json")
        with open(preset_file, "w") as f:
            json.dump(preset_data, f, indent=2)

        msg = f"✅ Preset '{preset_name}' saved with:\n"
        msg += f"  • {len(preset_data['whitelist'])} whitelist pairs\n"
        msg += f"  • {len(preset_data['blacklist'])} blacklist pairs\n"
        msg += f"  • Max trades: {preset_data['max_open_trades']}"

        await self._send_msg(msg)

    @authorized_only
    async def _preset_load(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /preset load <name> - Load saved preset
        """
        if not context.args or len(context.args) < 2 or context.args[0] != "load":
            await self._send_msg(
                "Usage: `/preset load <name>`\nExample: `/preset load bull_market`",
                ParseMode.MARKDOWN,
            )
            return

        preset_name = context.args[1]

        import os

        preset_file = os.path.join(self._config["user_data_dir"], "presets", f"{preset_name}.json")

        if not os.path.exists(preset_file):
            await self._send_msg(f"❌ Preset '{preset_name}' not found")
            return

        try:
            with open(preset_file, "r") as f:
                preset_data = json.load(f)

            # Apply preset
            # Update blacklist
            current_blacklist = self._rpc._rpc_blacklist()["blacklist"]
            for pair in current_blacklist:
                if pair not in preset_data["blacklist"]:
                    self._rpc._freqtrade.pairlists.blacklist.remove(pair)

            for pair in preset_data["blacklist"]:
                if pair not in current_blacklist:
                    self._rpc._freqtrade.pairlists.blacklist.append(pair)

            # Update config
            if preset_data.get("max_open_trades"):
                self._config["max_open_trades"] = preset_data["max_open_trades"]
                self._rpc._freqtrade.config["max_open_trades"] = preset_data["max_open_trades"]

            msg = f"✅ Preset '{preset_name}' loaded\n"
            msg += f"Saved on: {preset_data.get('timestamp', 'unknown')}"

            await self._send_msg(msg)

        except Exception as e:
            await self._send_msg(f"❌ Error loading preset: {str(e)}")

    @authorized_only
    async def _close_profitable(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /close_profitable - Close only profitable trades
        """
        trades = Trade.get_open_trades()
        closed_trades = []
        failed_trades = []
        total_profit = 0

        # First, identify profitable trades
        profitable_trades = []
        for trade in trades:
            try:
                current_rate = self._rpc._freqtrade.exchange.get_rate(
                    trade.pair, side="exit", is_short=trade.is_short, refresh=False
                )
                profit = trade.calc_profit(current_rate)

                if profit > 0:
                    profitable_trades.append((trade, profit))
            except Exception:
                continue

        if not profitable_trades:
            await self._send_msg("No profitable trades to close")
            return

        # Close profitable trades using bulk processing
        with self._rpc._freqtrade._exit_lock:
            for trade, profit in profitable_trades:
                try:
                    success = self._rpc._RPC__exec_force_exit(trade, None)
                    if success:
                        closed_trades.append((trade.pair, profit))
                        total_profit += profit
                    else:
                        failed_trades.append(f"{trade.pair} (#{trade.id}): Failed to execute exit")
                except Exception as e:
                    failed_trades.append(f"{trade.pair} (#{trade.id}): {str(e)}")

            # Commit all changes at once and update wallets
            Trade.commit()
            self._rpc._freqtrade.wallets.update()

        if closed_trades:
            stake_cur = self._config["stake_currency"]
            msg = f"✅ Closed {len(closed_trades)} profitable trades\n"
            msg += f"**Total profit:** {total_profit:.2f} {stake_cur}\n\n"
            for pair, profit in closed_trades[:10]:
                msg += f"  • {pair}: +{profit:.2f} {stake_cur}\n"
            if len(closed_trades) > 10:
                msg += f"  ... and {len(closed_trades) - 10} more"
            if failed_trades:
                msg += f"\n❌ Failed to close:\n" + "\n".join(failed_trades[:5])
        else:
            msg = "Failed to close profitable trades"

        await self._send_msg(msg, ParseMode.MARKDOWN)

    @authorized_only
    async def _close_losing(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /close_losing - Close only losing trades
        """
        trades = Trade.get_open_trades()
        closed_trades = []
        failed_trades = []
        total_loss = 0

        # First, identify losing trades
        losing_trades = []
        for trade in trades:
            try:
                current_rate = self._rpc._freqtrade.exchange.get_rate(
                    trade.pair, side="exit", is_short=trade.is_short, refresh=False
                )
                profit = trade.calc_profit(current_rate)

                if profit < 0:
                    losing_trades.append((trade, profit))
            except Exception:
                continue

        if not losing_trades:
            await self._send_msg("No losing trades to close")
            return

        # Close losing trades using bulk processing
        with self._rpc._freqtrade._exit_lock:
            for trade, profit in losing_trades:
                try:
                    success = self._rpc._RPC__exec_force_exit(trade, None)
                    if success:
                        closed_trades.append((trade.pair, profit))
                        total_loss += profit
                    else:
                        failed_trades.append(f"{trade.pair} (#{trade.id}): Failed to execute exit")
                except Exception as e:
                    failed_trades.append(f"{trade.pair} (#{trade.id}): {str(e)}")

            # Commit all changes at once and update wallets
            Trade.commit()
            self._rpc._freqtrade.wallets.update()

        if closed_trades:
            stake_cur = self._config["stake_currency"]
            msg = f"✅ Closed {len(closed_trades)} losing trades\n"
            msg += f"**Total loss:** {total_loss:.2f} {stake_cur}\n\n"
            for pair, loss in closed_trades[:10]:
                msg += f"  • {pair}: {loss:.2f} {stake_cur}\n"
            if len(closed_trades) > 10:
                msg += f"  ... and {len(closed_trades) - 10} more"
            if failed_trades:
                msg += f"\n❌ Failed to close:\n" + "\n".join(failed_trades[:5])
        else:
            msg = "Failed to close losing trades"

        await self._send_msg(msg, ParseMode.MARKDOWN)

    @authorized_only
    async def _scale_out(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /scale_out <trade_id> <percent> - Partial exit
        """
        if not context.args or len(context.args) != 2:
            await self._send_msg(
                "Usage: `/scale_out <trade_id> <percent>`\nExample: `/scale_out 5 50` (sell 50% of trade 5)",
                ParseMode.MARKDOWN,
            )
            return

        try:
            trade_id = int(context.args[0])
            percent = float(context.args[1])

            if percent <= 0 or percent > 100:
                await self._send_msg("❌ Percent must be between 1 and 100")
                return

            trade = Trade.get_trades([Trade.id == trade_id, Trade.is_open.is_(True)]).first()
            if not trade:
                await self._send_msg(f"❌ Trade {trade_id} not found or not open")
                return

            # Calculate amount to sell
            amount_to_sell = trade.amount * (percent / 100)

            # Force partial exit
            self._rpc._rpc_force_exit(str(trade_id), amount=amount_to_sell)

            msg = f"✅ Scaled out {percent}% of {trade.pair}\n"
            msg += f"Sold amount: {amount_to_sell:.8f}"

            await self._send_msg(msg)

        except (ValueError, IndexError):
            await self._send_msg(
                "❌ Invalid parameters. Use: `/scale_out <trade_id> <percent>`", ParseMode.MARKDOWN
            )

    # ====== SMART FILTERING & CONTROL ======

    @authorized_only
    async def _enable_volatile(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /enable_volatile - Enable only high volatility pairs
        """
        # Get all pairs and their 24h price changes
        whitelist = self._rpc._rpc_whitelist()["whitelist"]
        volatile_pairs = []

        for pair in whitelist:
            try:
                ticker = self._rpc._freqtrade.exchange.fetch_ticker(pair)
                if ticker and "percentage" in ticker:
                    change_pct = abs(ticker["percentage"])
                    if change_pct > 5:  # More than 5% change
                        volatile_pairs.append((pair, change_pct))
            except:
                continue

        # Sort by volatility
        volatile_pairs.sort(key=lambda x: x[1], reverse=True)

        # Enable top volatile pairs
        if volatile_pairs:
            # First disable all
            self._rpc._rpc_blacklist(whitelist)

            # Then enable volatile ones
            pairs_to_enable = [p[0] for p in volatile_pairs[:10]]  # Top 10
            self._rpc._rpc_blacklist_delete(pairs_to_enable)

            msg = f"✅ Enabled {len(pairs_to_enable)} volatile pairs:\n"
            for pair, change in volatile_pairs[:10]:
                msg += f"  • {pair}: {change:.1f}% change\n"
        else:
            msg = "No volatile pairs found (>5% 24h change)"

        await self._send_msg(msg)

    @authorized_only
    async def _disable_low_volume(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /disable_low_volume - Disable pairs with low volume
        """
        whitelist = self._rpc._rpc_whitelist()["whitelist"]
        low_volume_pairs = []

        for pair in whitelist:
            try:
                ticker = self._rpc._freqtrade.exchange.fetch_ticker(pair)
                if ticker and "quoteVolume" in ticker:
                    volume = ticker["quoteVolume"]
                    if volume < 100000:  # Less than 100k volume
                        low_volume_pairs.append((pair, volume))
            except:
                continue

        if low_volume_pairs:
            pairs_to_disable = [p[0] for p in low_volume_pairs]
            self._rpc._rpc_blacklist(pairs_to_disable)

            msg = f"🚫 Disabled {len(pairs_to_disable)} low volume pairs:\n"
            for pair, volume in low_volume_pairs[:10]:
                msg += f"  • {pair}: {volume:.0f} volume\n"
            if len(low_volume_pairs) > 10:
                msg += f"  ... and {len(low_volume_pairs) - 10} more"
        else:
            msg = "No low volume pairs found (<100k)"

        await self._send_msg(msg)

    @authorized_only
    async def _rotate_pairs(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /rotate_pairs <number> - Keep only top N performing pairs
        """
        if not context.args or len(context.args) != 1:
            await self._send_msg(
                "Usage: `/rotate_pairs <number>`\nExample: `/rotate_pairs 10`", ParseMode.MARKDOWN
            )
            return

        try:
            top_n = int(context.args[0])
            if top_n <= 0:
                await self._send_msg("❌ Number must be positive")
                return

            # Get performance data
            performance = self._rpc._rpc_performance()

            if not performance:
                await self._send_msg("No performance data available")
                return

            # Sort by profit
            performance.sort(key=lambda x: x["profit_abs"], reverse=True)

            # Get top performers
            top_pairs = [p["pair"] for p in performance[:top_n]]

            # Disable all pairs first
            whitelist = self._rpc._rpc_whitelist()["whitelist"]
            self._rpc._rpc_blacklist(whitelist)

            # Enable only top performers
            if top_pairs:
                self._rpc._rpc_blacklist_delete(top_pairs)

            msg = f"✅ Rotated to top {len(top_pairs)} performing pairs:\n"
            for p in performance[:top_n]:
                msg += f"  • {p['pair']}: {p['profit_abs']:.2f} profit\n"

            await self._send_msg(msg)

        except ValueError:
            await self._send_msg("❌ Invalid number")

    @authorized_only
    async def _pause_new_shorts(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /pause_new_shorts - Only pause short entries
        """
        # This would need strategy-level implementation
        # For now, we'll track it in config
        if "pause_shorts" not in self._config:
            self._config["pause_shorts"] = False

        self._config["pause_shorts"] = True
        await self._send_msg(
            "⏸️ New short positions paused\n⚠️ Note: Existing shorts will continue to be managed"
        )

    @authorized_only
    async def _pause_new_longs(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /pause_new_longs - Only pause long entries
        """
        # This would need strategy-level implementation
        # For now, we'll track it in config
        if "pause_longs" not in self._config:
            self._config["pause_longs"] = False

        self._config["pause_longs"] = True
        await self._send_msg(
            "⏸️ New long positions paused\n⚠️ Note: Existing longs will continue to be managed"
        )

    # ====== MARKET ANALYSIS ======

    @authorized_only
    async def _market_sentiment(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /market_sentiment - Show overall market direction
        """
        whitelist = self._rpc._rpc_whitelist()["whitelist"]

        bullish = 0
        bearish = 0
        neutral = 0

        for pair in whitelist[:20]:  # Check first 20 pairs
            try:
                ticker = self._rpc._freqtrade.exchange.fetch_ticker(pair)
                if ticker and "percentage" in ticker:
                    change = ticker["percentage"]
                    if change > 1:
                        bullish += 1
                    elif change < -1:
                        bearish += 1
                    else:
                        neutral += 1
            except:
                continue

        total = bullish + bearish + neutral
        if total == 0:
            await self._send_msg("Unable to determine market sentiment")
            return

        bull_pct = (bullish / total) * 100
        bear_pct = (bearish / total) * 100

        if bull_pct > 60:
            sentiment = "🟢 BULLISH"
            emoji = "📈"
        elif bear_pct > 60:
            sentiment = "🔴 BEARISH"
            emoji = "📉"
        else:
            sentiment = "🟡 NEUTRAL"
            emoji = "➡️"

        msg = f"{emoji} **Market Sentiment: {sentiment}**\n\n"
        msg += f"Based on {total} pairs:\n"
        msg += f"🟢 Bullish: {bullish} ({bull_pct:.1f}%)\n"
        msg += f"🔴 Bearish: {bearish} ({bear_pct:.1f}%)\n"
        msg += f"🟡 Neutral: {neutral} ({(neutral / total * 100):.1f}%)"

        await self._send_msg(msg, ParseMode.MARKDOWN)

    @authorized_only
    async def _top_gainers(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /top_gainers [number] - Show best performing pairs
        """
        limit = int(context.args[0]) if context.args else 5

        whitelist = self._rpc._rpc_whitelist()["whitelist"]
        gainers = []

        for pair in whitelist:
            try:
                ticker = self._rpc._freqtrade.exchange.fetch_ticker(pair)
                if ticker and "percentage" in ticker:
                    gainers.append((pair, ticker["percentage"]))
            except:
                continue

        gainers.sort(key=lambda x: x[1], reverse=True)

        msg = f"📈 **Top {min(limit, len(gainers))} Gainers (24h)**\n\n"
        for pair, change in gainers[:limit]:
            msg += f"• {pair}: +{change:.2f}%\n"

        if not gainers:
            msg = "No gainer data available"

        await self._send_msg(msg, ParseMode.MARKDOWN)

    @authorized_only
    async def _top_losers(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /top_losers [number] - Show worst performing pairs
        """
        limit = int(context.args[0]) if context.args else 5

        whitelist = self._rpc._rpc_whitelist()["whitelist"]
        losers = []

        for pair in whitelist:
            try:
                ticker = self._rpc._freqtrade.exchange.fetch_ticker(pair)
                if ticker and "percentage" in ticker:
                    losers.append((pair, ticker["percentage"]))
            except:
                continue

        losers.sort(key=lambda x: x[1])

        msg = f"📉 **Top {min(limit, len(losers))} Losers (24h)**\n\n"
        for pair, change in losers[:limit]:
            msg += f"• {pair}: {change:.2f}%\n"

        if not losers:
            msg = "No loser data available"

        await self._send_msg(msg, ParseMode.MARKDOWN)

    # ====== TRADE MANAGEMENT ======

    @authorized_only
    async def _avg_down(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /avg_down <trade_id> - Add to losing position
        """
        if not context.args or len(context.args) != 1:
            await self._send_msg("Usage: `/avg_down <trade_id>`", ParseMode.MARKDOWN)
            return

        try:
            trade_id = int(context.args[0])
            trade = Trade.get_trades([Trade.id == trade_id, Trade.is_open.is_(True)]).first()

            if not trade:
                await self._send_msg(f"❌ Trade {trade_id} not found or not open")
                return

            # Check if position adjustment is enabled
            if not self._rpc._freqtrade.strategy.position_adjustment_enable:
                await self._send_msg("❌ Position adjustment not enabled in strategy")
                return

            # Force another entry for the same pair
            self._rpc._rpc_force_entry(
                trade.pair,
                None,
                order_side=SignalDirection.SHORT if trade.is_short else SignalDirection.LONG,
            )

            msg = f"✅ Added to position for {trade.pair}\n"
            msg += f"This will average down your entry price"

            await self._send_msg(msg)

        except ValueError:
            await self._send_msg("❌ Invalid trade ID")

    @authorized_only
    async def _take_profit(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /take_profit <trade_id> <price> - Set TP for specific trade
        """
        if not context.args or len(context.args) != 2:
            await self._send_msg("Usage: `/take_profit <trade_id> <price>`", ParseMode.MARKDOWN)
            return

        try:
            trade_id = int(context.args[0])
            tp_price = float(context.args[1])

            trade = Trade.get_trades([Trade.id == trade_id, Trade.is_open.is_(True)]).first()
            if not trade:
                await self._send_msg(f"❌ Trade {trade_id} not found or not open")
                return

            # Store take profit in custom data
            trade.set_custom_data("take_profit", tp_price)

            msg = f"✅ Take profit set for {trade.pair} at {tp_price}\n"
            msg += f"⚠️ Note: Manual monitoring required - will notify when price is reached"

            await self._send_msg(msg)

        except ValueError:
            await self._send_msg("❌ Invalid parameters")

    @authorized_only
    async def _breakeven(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /breakeven <trade_id> - Move SL to breakeven
        """
        if not context.args or len(context.args) != 1:
            await self._send_msg("Usage: `/breakeven <trade_id>`", ParseMode.MARKDOWN)
            return

        try:
            trade_id = int(context.args[0])
            trade = Trade.get_trades([Trade.id == trade_id, Trade.is_open.is_(True)]).first()

            if not trade:
                await self._send_msg(f"❌ Trade {trade_id} not found or not open")
                return

            # Set stop loss to entry price (breakeven)
            trade.adjust_stop_loss(trade.open_rate, 0)

            msg = f"✅ Stop loss moved to breakeven for {trade.pair}\n"
            msg += f"Entry: {trade.open_rate}, New SL: {trade.stop_loss}"

            await self._send_msg(msg)

        except ValueError:
            await self._send_msg("❌ Invalid trade ID")

    # ====== CONFIGURATION BACKUP/RESTORE ======

    @authorized_only
    async def _config_backup(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /config_backup - Backup current config
        """
        import os
        from shutil import copyfile

        backup_dir = os.path.join(self._config["user_data_dir"], "backups")
        os.makedirs(backup_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = os.path.join(backup_dir, f"config_backup_{timestamp}.json")

        # Save current config
        config_to_save = {
            "exchange": self._config.get("exchange"),
            "stake_currency": self._config.get("stake_currency"),
            "stake_amount": self._config.get("stake_amount"),
            "max_open_trades": self._config.get("max_open_trades"),
            "stoploss": self._config.get("stoploss"),
            "trailing_stop": self._config.get("trailing_stop"),
            "whitelist": self._rpc._rpc_whitelist()["whitelist"],
            "blacklist": self._rpc._rpc_blacklist()["blacklist"],
        }

        with open(backup_file, "w") as f:
            json.dump(config_to_save, f, indent=2)

        msg = f"✅ Configuration backed up\n"
        msg += f"File: config_backup_{timestamp}.json"

        await self._send_msg(msg)

    @authorized_only
    async def _config_restore(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /config_restore - Restore from backup
        """
        import os

        backup_dir = os.path.join(self._config["user_data_dir"], "backups")

        if not os.path.exists(backup_dir):
            await self._send_msg("❌ No backups found")
            return

        # Get latest backup
        backups = [f for f in os.listdir(backup_dir) if f.startswith("config_backup_")]

        if not backups:
            await self._send_msg("❌ No backups found")
            return

        backups.sort()
        latest_backup = backups[-1]

        backup_file = os.path.join(backup_dir, latest_backup)

        try:
            with open(backup_file, "r") as f:
                backup_config = json.load(f)

            # Restore settings
            for key in ["stake_amount", "max_open_trades", "stoploss"]:
                if key in backup_config:
                    self._config[key] = backup_config[key]
                    self._rpc._freqtrade.config[key] = backup_config[key]

            msg = f"✅ Configuration restored from {latest_backup}\n"
            msg += "⚠️ Restart bot for full restoration"

            await self._send_msg(msg)

        except Exception as e:
            await self._send_msg(f"❌ Error restoring config: {str(e)}")

    @authorized_only
    async def _strategy_reload(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /strategy_reload - Reload strategy without restart
        """
        try:
            # Reload strategy
            self._rpc._freqtrade.strategy.reload()
            await self._send_msg("✅ Strategy reloaded successfully")
        except Exception as e:
            await self._send_msg(f"❌ Error reloading strategy: {str(e)}")

    # ====== MENU SYSTEM ======

    @authorized_only
    async def _menu_trade(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /menu_trade - Shows trading commands menu
        """
        msg = "📊 **Trading Commands**\n\n"
        msg += "**Trend Control:**\n"
        msg += "`/trendset bull|bear|neutral [hrs]` - Set trend\n"
        msg += "`/longall` - Bull trend 24h (longs only)\n"
        msg += "`/shortall` - Bear trend 24h (shorts only)\n"
        msg += "`/trendstatus` - Check active trend\n"
        msg += "`/trendclear` - Clear trend\n\n"
        msg += "**Entry/Exit:**\n"
        msg += "`/closelong` - Close all long trades\n"
        msg += "`/closeshort` - Close all short trades\n"
        msg += "`/close_profitable` - Close profitable trades\n"
        msg += "`/close_losing` - Close losing trades\n"
        msg += "`/scale_out <id> <pct>` - Partial exit\n\n"
        msg += "**Management:**\n"
        msg += "`/avg_down <id>` - Average down position\n"
        msg += "`/take_profit <id> <price>` - Set take profit\n"
        msg += "`/breakeven <id>` - Move SL to breakeven"

        await self._send_msg(msg, ParseMode.MARKDOWN)

    @authorized_only
    async def _menu_risk(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /menu_risk - Shows risk management commands menu
        """
        msg = "🛡️ **Risk Management Commands**\n\n"
        msg += "**Trend Control:**\n"
        msg += "`/trendset bull|bear|neutral [hrs]` - Control direction\n"
        msg += "`/trendhold on|off [hrs]` - Hold on trend\n"
        msg += "`/trendstatus` - Check trend settings\n\n"
        msg += "**Position Risk:**\n"
        msg += "`/risk_status` - Current risk metrics\n"
        msg += "`/setmaxopen <n>` - Set max open trades\n"
        msg += "`/set_stoploss <pct>` - Set stop-loss %\n\n"
        msg += "**Emergency Controls:**\n"
        msg += "`/emergency_stop` - Stop bot & close all\n"
        msg += "`/pausefor <min>` - Pause for X minutes\n"
        msg += "`/pause_new_longs` - Pause long entries\n"
        msg += "`/pause_new_shorts` - Pause short entries\n\n"
        msg += "**Profit Control:**\n"
        msg += "`/disable_roi` - Let trends run (no profit exits)\n"
        msg += "`/profit_mode disabled` - Same as disable_roi\n"
        msg += "`/profit_status` - Current profit settings"

        await self._send_msg(msg, ParseMode.MARKDOWN)

    @authorized_only
    async def _menu_status(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /menu_status - Shows monitoring commands menu
        """
        msg = "📈 **Status & Monitoring Commands**\n\n"
        msg += "`/qs` - Quick status\n"
        msg += "`/pnl [today/week/month]` - P&L summary\n"
        msg += "`/watchlist` - Pairs being monitored\n"
        msg += "`/market_sentiment` - Market direction\n"
        msg += "`/top_gainers [n]` - Best performers\n"
        msg += "`/top_losers [n]` - Worst performers\n"
        msg += "`/alerts on/off` - Toggle notifications"

        await self._send_msg(msg, ParseMode.MARKDOWN)

    # ====== PROFIT CONTROL COMMANDS ======

    @authorized_only
    async def _disable_roi(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /disable_roi - Disable ROI-based exits (let trends run)
        """
        # Set ROI to a very high value effectively disabling it
        original_roi = self._config.get("minimal_roi", {})

        # Store original for restoration (normalize keys to integers)
        self._config["original_minimal_roi"] = self._normalize_roi_keys(original_roi)

        # Set extremely high ROI to disable automatic exits
        disabled_roi = {0: 100.0}  # 10000% ROI requirement

        self._config["minimal_roi"] = disabled_roi
        self._rpc._freqtrade.strategy.minimal_roi = disabled_roi

        msg = "🚫 **ROI-based exits DISABLED**\n\n"
        msg += "✅ Positions will run until:\n"
        msg += "  • Strategy exit signals\n"
        msg += "  • Stop-loss hits\n"
        msg += "  • Manual exit commands\n\n"
        msg += "⚠️ **Higher risk** - Monitor positions closely!\n"
        msg += "Use `/enable_roi` to restore profit-taking"

        await self._send_msg(msg, ParseMode.MARKDOWN)

    @authorized_only
    async def _enable_roi(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /enable_roi - Re-enable ROI-based exits
        """
        # Restore original ROI if available
        original_roi = self._config.get("original_minimal_roi")

        if original_roi:
            # Normalize keys to integers (in case they were stored as strings from config)
            original_roi = self._normalize_roi_keys(original_roi)
            self._config["minimal_roi"] = original_roi
            self._rpc._freqtrade.strategy.minimal_roi = original_roi
            msg = "✅ **ROI-based exits RESTORED**\n\n"
            msg += "Original ROI settings:\n"
            for time_key, roi_value in original_roi.items():
                msg += f"  • {time_key}min: {roi_value:.1%}\n"
        else:
            # Default conservative ROI
            default_roi = {0: 0.10, 40: 0.05, 100: 0.02, 200: 0.01}
            self._config["minimal_roi"] = default_roi
            self._rpc._freqtrade.strategy.minimal_roi = default_roi
            msg = "✅ **ROI-based exits ENABLED**\n\n"
            msg += "Using default ROI settings:\n"
            for time_key, roi_value in default_roi.items():
                msg += f"  • {time_key}min: {roi_value:.1%}\n"

        await self._send_msg(msg, ParseMode.MARKDOWN)

    @authorized_only
    async def _set_roi(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /set_roi <percentage> - Set simple ROI target
        Usage: /set_roi 5 (for 5% profit target)
        """
        if not context.args or len(context.args) != 1:
            await self._send_msg(
                "Usage: `/set_roi <percentage>`\nExample: `/set_roi 5` (for 5% target)",
                ParseMode.MARKDOWN,
            )
            return

        try:
            roi_pct = float(context.args[0])
            if roi_pct <= 0:
                await self._send_msg("❌ ROI percentage must be positive")
                return

            roi_decimal = roi_pct / 100

            # Store original ROI for restoration (normalize keys to integers)
            self._config["original_minimal_roi"] = self._normalize_roi_keys(
                self._config.get("minimal_roi", {})
            )

            # Set simple ROI target
            new_roi = {0: roi_decimal}
            self._config["minimal_roi"] = new_roi
            self._rpc._freqtrade.strategy.minimal_roi = new_roi

            msg = f"✅ **ROI target set to {roi_pct}%**\n\n"
            msg += f"Trades will exit when {roi_pct}% profit is reached\n"
            msg += "Use `/disable_roi` to let trends run longer"

            await self._send_msg(msg, ParseMode.MARKDOWN)

        except ValueError:
            await self._send_msg("❌ Invalid percentage. Please provide a valid number.")

    @authorized_only
    async def _disable_trailing(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /disable_trailing - Disable trailing stop
        """
        # Store original settings
        self._config["original_trailing_stop"] = self._config.get("trailing_stop", False)
        self._config["original_trailing_stop_positive"] = self._config.get("trailing_stop_positive")

        # Disable trailing stop
        self._config["trailing_stop"] = False
        self._rpc._freqtrade.strategy.trailing_stop = False

        msg = "🚫 **Trailing stop DISABLED**\n\n"
        msg += "✅ Stop-loss will remain fixed\n"
        msg += "⚠️ Won't lock in profits automatically\n"
        msg += "Use `/enable_trailing` to restore"

        await self._send_msg(msg, ParseMode.MARKDOWN)

    @authorized_only
    async def _enable_trailing(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /enable_trailing - Re-enable trailing stop
        """
        # Restore original settings or use defaults
        original_trailing = self._config.get("original_trailing_stop", True)
        original_positive = self._config.get("original_trailing_stop_positive", 0.01)

        self._config["trailing_stop"] = original_trailing
        self._config["trailing_stop_positive"] = original_positive
        self._rpc._freqtrade.strategy.trailing_stop = original_trailing
        self._rpc._freqtrade.strategy.trailing_stop_positive = original_positive

        msg = "✅ **Trailing stop ENABLED**\n\n"
        if original_trailing:
            msg += f"Trailing positive: {original_positive:.1%}\n"
            msg += "Will lock in profits as price moves favorably"
        else:
            msg += "Fixed stop-loss mode restored"

        await self._send_msg(msg, ParseMode.MARKDOWN)

    @authorized_only
    async def _profit_mode(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /profit_mode <mode> - Set profit-taking behavior
        Modes: disabled, conservative, normal, aggressive
        """
        if not context.args or len(context.args) != 1:
            await self._send_msg(
                "Usage: `/profit_mode <mode>`\n\n"
                "**Available modes:**\n"
                "`disabled` - Let all trends run (no ROI exits)\n"
                "`conservative` - 10%+ profit targets\n"
                "`normal` - 5%+ profit targets\n"
                "`aggressive` - 2%+ profit targets",
                ParseMode.MARKDOWN,
            )
            return

        mode = context.args[0].lower()

        # Store original settings (normalize keys to integers)
        self._config["original_minimal_roi"] = self._normalize_roi_keys(
            self._config.get("minimal_roi", {})
        )

        if mode == "disabled":
            # Disable ROI completely
            new_roi = {0: 100.0}  # 10000% requirement
            msg = "🚫 **PROFIT MODE: DISABLED**\n\nLetting all trends run until strategy exits or stop-loss"
        elif mode == "conservative":
            new_roi = {0: 0.15, 30: 0.10, 60: 0.08, 120: 0.05}
            msg = "🛡️ **PROFIT MODE: CONSERVATIVE**\n\nTargeting 10-15% profits with patience"
        elif mode == "normal":
            new_roi = {0: 0.08, 20: 0.05, 40: 0.03, 80: 0.02}
            msg = "⚖️ **PROFIT MODE: NORMAL**\n\nBalanced 5-8% profit targets"
        elif mode == "aggressive":
            new_roi = {0: 0.05, 10: 0.03, 20: 0.02, 40: 0.01}
            msg = "⚡ **PROFIT MODE: AGGRESSIVE**\n\nQuick 2-5% profit taking"
        else:
            await self._send_msg(
                "❌ Invalid mode. Use: disabled, conservative, normal, or aggressive"
            )
            return

        # Apply new ROI settings
        self._config["minimal_roi"] = new_roi
        self._rpc._freqtrade.strategy.minimal_roi = new_roi

        msg += "\n\n**ROI Schedule:**\n"
        for time_key, roi_value in new_roi.items():
            if roi_value < 10:  # Don't show the disabled mode's 100.0
                msg += f"  • {time_key}min: {roi_value:.1%}\n"

        await self._send_msg(msg, ParseMode.MARKDOWN)

    @authorized_only
    async def _profit_status(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /profit_status - Show current profit-taking settings
        """
        roi_config = self._normalize_roi_keys(self._config.get("minimal_roi", {}))
        trailing = self._config.get("trailing_stop", False)
        trailing_positive = self._config.get("trailing_stop_positive", 0)

        msg = "📊 **Current Profit Settings**\n\n"

        # Determine mode
        if roi_config.get("0", 0) >= 10:
            mode = "🚫 DISABLED"
        elif roi_config.get("0", 0) >= 0.10:
            mode = "🛡️ CONSERVATIVE"
        elif roi_config.get("0", 0) >= 0.05:
            mode = "⚖️ NORMAL"
        else:
            mode = "⚡ AGGRESSIVE"

        msg += f"**Mode:** {mode}\n\n"

        msg += "**ROI Schedule:**\n"
        for time_key, roi_value in roi_config.items():
            if roi_value < 10:  # Don't show disabled mode
                msg += f"  • {time_key}min: {roi_value:.1%}\n"

        if roi_config.get("0", 0) >= 10:
            msg += "  • ROI exits disabled - trends will run\n"

        msg += f"\n**Trailing Stop:** {'✅ Enabled' if trailing else '❌ Disabled'}\n"
        if trailing and trailing_positive:
            msg += f"**Trailing Positive:** {trailing_positive:.1%}\n"

        # Show impact on open trades
        open_trades = len(Trade.get_open_trades())
        if open_trades > 0:
            msg += f"\n📈 **{open_trades} open trades** using these settings"

        await self._send_msg(msg, ParseMode.MARKDOWN)

    @authorized_only
    async def _set_roi_long(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /set_roi_long <percentage> [duration] - Set ROI target for long trades
        Usage: /set_roi_long 30 => permanent 30% target
               /set_roi_long 15 2h => 15% target for 2 hours, then revert to strategy
        """
        if not context.args or len(context.args) < 1 or len(context.args) > 2:
            msg = "Usage: `/set_roi_long <percentage> [duration]`\n\n"
            msg += "Examples:\n"
            msg += "• `/set_roi_long 30` - 30% permanent target\n"
            msg += "• `/set_roi_long 15 2h` - 15% for 2 hours\n"
            msg += "• `/set_roi_long 20 30m` - 20% for 30 minutes\n"
            msg += "• `/set_roi_long 10 1.5h` - 10% for 1.5 hours"
            await self._send_msg(msg, ParseMode.MARKDOWN)
            return

        try:
            roi_pct = float(context.args[0])
            if roi_pct <= 0:
                await self._send_msg("❌ ROI percentage must be positive")
                return

            roi_decimal = roi_pct / 100
            duration_minutes = None
            duration_text = "permanent"

            # Parse duration if provided
            if len(context.args) == 2:
                duration_str = context.args[1].lower()
                duration_minutes = self._parse_duration(duration_str)
                if duration_minutes is None:
                    await self._send_msg("❌ Invalid duration format. Use examples: 2h, 30m, 1.5h")
                    return
                duration_text = duration_str

            # Store long ROI setting with timestamp if temporary
            if "roi_settings" not in self._config:
                self._config["roi_settings"] = {}

            from datetime import datetime, timedelta

            self._config["roi_settings"]["long_roi"] = roi_decimal

            if duration_minutes:
                # Set expiration time
                expiry_time = datetime.now() + timedelta(minutes=duration_minutes)
                self._config["roi_settings"]["long_roi_expiry"] = expiry_time.isoformat()
                msg = f"⏰ **Long ROI target: {roi_pct}% for {duration_text}**\n\n"
                msg += f"📈 Long trades exit at {roi_pct}% profit\n"
                msg += f"⏱️ Expires: {expiry_time.strftime('%H:%M:%S')}\n"
                msg += f"🔄 After expiry: Strategy controls exits"
            else:
                # Remove expiry if setting permanent ROI
                self._config["roi_settings"].pop("long_roi_expiry", None)
                msg = f"✅ **Long ROI target: {roi_pct}% (permanent)**\n\n"
                msg += f"📈 Long trades will exit at {roi_pct}% profit\n"
                msg += "📉 Short trades use separate settings"

            msg += "\nUse `/roi_status` to see all settings"
            await self._send_msg(msg, ParseMode.MARKDOWN)

        except ValueError:
            await self._send_msg("❌ Invalid percentage. Please provide a valid number.")

    @authorized_only
    async def _set_roi_short(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /set_roi_short <percentage> [duration] - Set ROI target for short trades
        Usage: /set_roi_short 20 => permanent 20% target
               /set_roi_short 12 1h => 12% target for 1 hour, then revert to strategy
        """
        if not context.args or len(context.args) < 1 or len(context.args) > 2:
            msg = "Usage: `/set_roi_short <percentage> [duration]`\n\n"
            msg += "Examples:\n"
            msg += "• `/set_roi_short 20` - 20% permanent target\n"
            msg += "• `/set_roi_short 12 1h` - 12% for 1 hour\n"
            msg += "• `/set_roi_short 15 45m` - 15% for 45 minutes\n"
            msg += "• `/set_roi_short 8 2.5h` - 8% for 2.5 hours"
            await self._send_msg(msg, ParseMode.MARKDOWN)
            return

        try:
            roi_pct = float(context.args[0])
            if roi_pct <= 0:
                await self._send_msg("❌ ROI percentage must be positive")
                return

            roi_decimal = roi_pct / 100
            duration_minutes = None
            duration_text = "permanent"

            # Parse duration if provided
            if len(context.args) == 2:
                duration_str = context.args[1].lower()
                duration_minutes = self._parse_duration(duration_str)
                if duration_minutes is None:
                    await self._send_msg("❌ Invalid duration format. Use examples: 1h, 45m, 2.5h")
                    return
                duration_text = duration_str

            # Store short ROI setting with timestamp if temporary
            if "roi_settings" not in self._config:
                self._config["roi_settings"] = {}

            from datetime import datetime, timedelta

            self._config["roi_settings"]["short_roi"] = roi_decimal

            if duration_minutes:
                # Set expiration time
                expiry_time = datetime.now() + timedelta(minutes=duration_minutes)
                self._config["roi_settings"]["short_roi_expiry"] = expiry_time.isoformat()
                msg = f"⏰ **Short ROI target: {roi_pct}% for {duration_text}**\n\n"
                msg += f"📉 Short trades exit at {roi_pct}% profit\n"
                msg += f"⏱️ Expires: {expiry_time.strftime('%H:%M:%S')}\n"
                msg += f"🔄 After expiry: Strategy controls exits"
            else:
                # Remove expiry if setting permanent ROI
                self._config["roi_settings"].pop("short_roi_expiry", None)
                msg = f"✅ **Short ROI target: {roi_pct}% (permanent)**\n\n"
                msg += f"📉 Short trades will exit at {roi_pct}% profit\n"
                msg += "📈 Long trades use separate settings"

            msg += "\nUse `/roi_status` to see all settings"
            await self._send_msg(msg, ParseMode.MARKDOWN)

        except ValueError:
            await self._send_msg("❌ Invalid percentage. Please provide a valid number.")

    @authorized_only
    async def _disable_roi_long(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /disable_roi_long - Disable ROI exits for long trades only
        """
        if "roi_settings" not in self._config:
            self._config["roi_settings"] = {}

        # Store that long ROI is disabled
        self._config["roi_settings"]["long_roi"] = 100.0  # 10000% = disabled

        msg = "🚫 **Long ROI DISABLED**\n\n"
        msg += "📈 Long positions will run until:\n"
        msg += "  • Strategy exit signals\n"
        msg += "  • Stop-loss hits\n"
        msg += "  • Manual exit commands\n\n"
        msg += "📉 Short trades still use ROI settings\n"
        msg += "Use `/enable_roi_long` to restore"

        await self._send_msg(msg, ParseMode.MARKDOWN)

    @authorized_only
    async def _disable_roi_short(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /disable_roi_short - Disable ROI exits for short trades only
        """
        if "roi_settings" not in self._config:
            self._config["roi_settings"] = {}

        # Store that short ROI is disabled
        self._config["roi_settings"]["short_roi"] = 100.0  # 10000% = disabled

        msg = "🚫 **Short ROI DISABLED**\n\n"
        msg += "📉 Short positions will run until:\n"
        msg += "  • Strategy exit signals\n"
        msg += "  • Stop-loss hits\n"
        msg += "  • Manual exit commands\n\n"
        msg += "📈 Long trades still use ROI settings\n"
        msg += "Use `/enable_roi_short` to restore"

        await self._send_msg(msg, ParseMode.MARKDOWN)

    @authorized_only
    async def _enable_roi_long(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /enable_roi_long - Re-enable ROI for long trades
        """
        if "roi_settings" not in self._config:
            self._config["roi_settings"] = {}

        # Set default long ROI if none exists
        default_long_roi = 0.08  # 8% default for longs
        self._config["roi_settings"]["long_roi"] = default_long_roi

        msg = f"✅ **Long ROI ENABLED**\n\n"
        msg += f"📈 Long trades: {default_long_roi:.1%} target\n"
        msg += "Use `/set_roi_long <pct>` to customize"

        await self._send_msg(msg, ParseMode.MARKDOWN)

    @authorized_only
    async def _enable_roi_short(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /enable_roi_short - Re-enable ROI for short trades
        """
        if "roi_settings" not in self._config:
            self._config["roi_settings"] = {}

        # Set default short ROI if none exists
        default_short_roi = 0.06  # 6% default for shorts
        self._config["roi_settings"]["short_roi"] = default_short_roi

        msg = f"✅ **Short ROI ENABLED**\n\n"
        msg += f"📉 Short trades: {default_short_roi:.1%} target\n"
        msg += "Use `/set_roi_short <pct>` to customize"

        await self._send_msg(msg, ParseMode.MARKDOWN)

    @authorized_only
    async def _roi_status_separate(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /roi_status - Show separate long/short ROI settings with expiration
        """
        from datetime import datetime

        roi_settings = self._config.get("roi_settings", {})
        long_roi = roi_settings.get("long_roi", "Not set")
        short_roi = roi_settings.get("short_roi", "Not set")
        long_expiry = roi_settings.get("long_roi_expiry")
        short_expiry = roi_settings.get("short_roi_expiry")
        current_time = datetime.now()

        msg = "📊 **Separate ROI Settings**\n\n"

        # Long settings with expiration check
        if long_roi == "Not set":
            msg += "📈 **Long Trades:** Using global ROI\n"
        elif long_roi >= 10:
            msg += "📈 **Long Trades:** 🚫 DISABLED (letting trends run)\n"
        else:
            msg += f"📈 **Long Trades:** {long_roi:.1%} profit target"
            if long_expiry:
                try:
                    expiry_time = datetime.fromisoformat(long_expiry)
                    if current_time > expiry_time:
                        msg += " ⏰ **EXPIRED** (reverted to strategy)\n"
                        # Clean up expired setting
                        self._cleanup_expired_roi("long")
                    else:
                        time_left = expiry_time - current_time
                        hours_left = int(time_left.total_seconds() / 3600)
                        mins_left = int((time_left.total_seconds() % 3600) / 60)
                        if hours_left > 0:
                            msg += f" ⏱️ Expires in {hours_left}h {mins_left}m\n"
                        else:
                            msg += f" ⏱️ Expires in {mins_left}m\n"
                except (ValueError, TypeError):
                    msg += " ⚠️ Invalid expiry - cleaned up\n"
                    self._cleanup_expired_roi("long")
            else:
                msg += " (permanent)\n"

        # Short settings with expiration check
        if short_roi == "Not set":
            msg += "📉 **Short Trades:** Using global ROI\n"
        elif short_roi >= 10:
            msg += "📉 **Short Trades:** 🚫 DISABLED (letting trends run)\n"
        else:
            msg += f"📉 **Short Trades:** {short_roi:.1%} profit target"
            if short_expiry:
                try:
                    expiry_time = datetime.fromisoformat(short_expiry)
                    if current_time > expiry_time:
                        msg += " ⏰ **EXPIRED** (reverted to strategy)\n"
                        # Clean up expired setting
                        self._cleanup_expired_roi("short")
                    else:
                        time_left = expiry_time - current_time
                        hours_left = int(time_left.total_seconds() / 3600)
                        mins_left = int((time_left.total_seconds() % 3600) / 60)
                        if hours_left > 0:
                            msg += f" ⏱️ Expires in {hours_left}h {mins_left}m\n"
                        else:
                            msg += f" ⏱️ Expires in {mins_left}m\n"
                except (ValueError, TypeError):
                    msg += " ⚠️ Invalid expiry - cleaned up\n"
                    self._cleanup_expired_roi("short")
            else:
                msg += " (permanent)\n"

        # Show current trades breakdown
        trades = Trade.get_open_trades()
        long_trades = [t for t in trades if not t.is_short]
        short_trades = [t for t in trades if t.is_short]

        msg += f"\n**Current Positions:**\n"
        msg += f"  📈 Long: {len(long_trades)} trades\n"
        msg += f"  📉 Short: {len(short_trades)} trades\n"

        msg += f"\n**Quick Commands:**\n"
        msg += f"`/set_roi_long 30 2h` - 30% for 2 hours\n"
        msg += f"`/set_roi_short 20` - 20% permanent\n"
        msg += f"`/check_roi_targets` - See ready trades"

        await self._send_msg(msg, ParseMode.MARKDOWN)

    @authorized_only
    async def _extend_roi(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /extend_roi <direction> <duration> - Extend ROI expiration time
        Usage: /extend_roi long 1h   - Extend long ROI by 1 hour
               /extend_roi short 30m - Extend short ROI by 30 minutes
        """
        if not context.args or len(context.args) != 2:
            msg = "Usage: `/extend_roi <direction> <duration>`\n\n"
            msg += "Examples:\n"
            msg += "• `/extend_roi long 1h` - Extend long ROI by 1 hour\n"
            msg += "• `/extend_roi short 30m` - Extend short ROI by 30 minutes\n"
            msg += "• `/extend_roi both 45m` - Extend both by 45 minutes"
            await self._send_msg(msg, ParseMode.MARKDOWN)
            return

        direction = context.args[0].lower()
        duration_str = context.args[1].lower()

        if direction not in ["long", "short", "both"]:
            await self._send_msg("❌ Direction must be 'long', 'short', or 'both'")
            return

        duration_minutes = self._parse_duration(duration_str)
        if duration_minutes is None:
            await self._send_msg("❌ Invalid duration format. Use examples: 1h, 30m, 2.5h")
            return

        from datetime import datetime, timedelta

        roi_settings = self._config.get("roi_settings", {})
        current_time = datetime.now()
        extensions = []

        if direction in ["long", "both"]:
            long_expiry = roi_settings.get("long_roi_expiry")
            if long_expiry:
                try:
                    expiry_time = datetime.fromisoformat(long_expiry)
                    new_expiry = expiry_time + timedelta(minutes=duration_minutes)
                    roi_settings["long_roi_expiry"] = new_expiry.isoformat()
                    extensions.append(f"📈 Long extended to {new_expiry.strftime('%H:%M:%S')}")
                except (ValueError, TypeError):
                    extensions.append("❌ Long ROI expiry was invalid")
            else:
                extensions.append("⚠️ Long ROI has no expiration to extend")

        if direction in ["short", "both"]:
            short_expiry = roi_settings.get("short_roi_expiry")
            if short_expiry:
                try:
                    expiry_time = datetime.fromisoformat(short_expiry)
                    new_expiry = expiry_time + timedelta(minutes=duration_minutes)
                    roi_settings["short_roi_expiry"] = new_expiry.isoformat()
                    extensions.append(f"📉 Short extended to {new_expiry.strftime('%H:%M:%S')}")
                except (ValueError, TypeError):
                    extensions.append("❌ Short ROI expiry was invalid")
            else:
                extensions.append("⚠️ Short ROI has no expiration to extend")

        msg = f"⏰ **Extended ROI by {duration_str}:**\n\n"
        msg += "\n".join(extensions)
        msg += "\n\nUse `/roi_status` to see current settings"

        await self._send_msg(msg, ParseMode.MARKDOWN)

    def _parse_duration(self, duration_str: str) -> int:
        """
        Parse duration string to minutes
        Examples: '2h', '30m', '1.5h', '45m'
        Returns: duration in minutes or None if invalid
        """
        import re

        # Remove spaces and convert to lowercase
        duration_str = duration_str.strip().lower()

        # Match patterns like: 2h, 30m, 1.5h
        hour_match = re.match(r"^(\d+(?:\.\d+)?)h$", duration_str)
        minute_match = re.match(r"^(\d+)m$", duration_str)

        if hour_match:
            hours = float(hour_match.group(1))
            return int(hours * 60)
        elif minute_match:
            minutes = int(minute_match.group(1))
            return minutes

        return None

    def get_roi_for_trade(self, trade) -> dict:
        """
        Get the appropriate ROI table for a trade based on its direction and expiration
        This method can be called by the strategy or bot to get position-specific ROI
        """
        from datetime import datetime

        roi_settings = self._config.get("roi_settings", {})
        current_time = datetime.now()

        if trade.is_short:
            # Short trade - check if ROI has expired
            short_roi = roi_settings.get("short_roi")
            short_roi_expiry = roi_settings.get("short_roi_expiry")

            if short_roi is not None:
                # Check if ROI has expired
                if short_roi_expiry:
                    try:
                        expiry_time = datetime.fromisoformat(short_roi_expiry)
                        if current_time > expiry_time:
                            # ROI expired - clean up and revert to strategy
                            self._cleanup_expired_roi("short")
                            return None  # Let strategy handle ROI
                    except (ValueError, TypeError):
                        # Invalid expiry format, clean up
                        self._cleanup_expired_roi("short")
                        return None

                if short_roi >= 10:  # Disabled
                    return {0: 100.0}  # Very high ROI = disabled
                else:
                    return {0: short_roi}
        else:
            # Long trade - check if ROI has expired
            long_roi = roi_settings.get("long_roi")
            long_roi_expiry = roi_settings.get("long_roi_expiry")

            if long_roi is not None:
                # Check if ROI has expired
                if long_roi_expiry:
                    try:
                        expiry_time = datetime.fromisoformat(long_roi_expiry)
                        if current_time > expiry_time:
                            # ROI expired - clean up and revert to strategy
                            self._cleanup_expired_roi("long")
                            return None  # Let strategy handle ROI
                    except (ValueError, TypeError):
                        # Invalid expiry format, clean up
                        self._cleanup_expired_roi("long")
                        return None

                if long_roi >= 10:  # Disabled
                    return {0: 100.0}  # Very high ROI = disabled
                else:
                    return {0: long_roi}

        # Fall back to global ROI if no specific setting
        return self._normalize_roi_keys(self._config.get("minimal_roi", {0: 0.05}))

    def _cleanup_expired_roi(self, direction: str) -> None:
        """
        Clean up expired ROI settings and notify user
        """
        if "roi_settings" not in self._config:
            return

        roi_settings = self._config["roi_settings"]

        if direction == "long":
            roi_settings.pop("long_roi", None)
            roi_settings.pop("long_roi_expiry", None)
        elif direction == "short":
            roi_settings.pop("short_roi", None)
            roi_settings.pop("short_roi_expiry", None)

        # Could send notification here if needed
        # For now, silent cleanup

    def should_exit_roi(self, trade, current_profit: float) -> bool:
        """
        Check if a trade should exit based on position-specific ROI
        This replaces the standard ROI check with direction-aware logic
        """
        roi_table = self.get_roi_for_trade(trade)

        # Get the ROI target (assuming simple ROI for now)
        roi_target = list(roi_table.values())[0]

        # If ROI is disabled (very high value), don't exit
        if roi_target >= 10:
            return False

        # Exit if current profit meets or exceeds ROI target
        return current_profit >= roi_target

    @authorized_only
    async def _force_roi_check(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /force_roi_check - Manually check and exit trades that meet ROI targets
        """
        try:
            result = self._rpc._force_roi_exit_trades()

            if "exited_trades" in result and result["exited_trades"]:
                msg = "✅ **ROI Exits Executed:**\n\n"
                for trade_info in result["exited_trades"][:10]:
                    direction = "📉 Short" if trade_info["direction"] == "short" else "📈 Long"
                    msg += f"• {direction} {trade_info['pair']}: "
                    msg += f"{trade_info['current_profit_pct']:.1f}% (target: {trade_info['roi_target']:.1f}%)\n"

                if len(result["exited_trades"]) > 10:
                    msg += f"... and {len(result['exited_trades']) - 10} more"
            else:
                msg = "📊 No trades meet their ROI targets yet"

        except RPCException as e:
            msg = f"❌ Error checking ROI: {str(e)}"

        await self._send_msg(msg, ParseMode.MARKDOWN)

    @authorized_only
    async def _check_roi_targets(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /check_roi_targets - Show which trades are close to ROI targets
        """
        try:
            result = self._rpc._check_roi_for_trades()

            if result.get("result") == "No open trades to check":
                await self._send_msg("No open trades to check")
                return

            msg = "🎯 **ROI Target Status:**\n\n"

            # Show summary first
            summary = result.get("summary", {})
            msg += f"**Summary:**\n"
            msg += f"• Long ROI: {'Enabled' if summary.get('long_roi_enabled') else 'Disabled'}"
            if summary.get("long_roi_enabled"):
                msg += f" ({summary.get('long_roi_target', 0):.1f}%)"
            msg += "\n"
            msg += f"• Short ROI: {'Enabled' if summary.get('short_roi_enabled') else 'Disabled'}"
            if summary.get("short_roi_enabled"):
                msg += f" ({summary.get('short_roi_target', 0):.1f}%)"
            msg += f"\n• Trades ready for exit: {summary.get('trades_ready_for_roi_exit', 0)}\n\n"

            # Show trades ready for ROI exit
            roi_ready = result.get("roi_ready_trades", [])
            if roi_ready:
                msg += "**✅ Ready for ROI Exit:**\n"
                for trade_info in roi_ready[:5]:
                    direction = "📉" if trade_info["direction"] == "short" else "📈"
                    msg += f"{direction} **{trade_info['pair']}** (#{trade_info['trade_id']})\n"
                    msg += f"   Current: {trade_info['current_profit_pct']:.1f}% | "
                    msg += f"Target: {trade_info['roi_target']:.1f}%\n"
                    msg += f"   🟢 **TARGET REACHED!**\n\n"

                if len(roi_ready) > 5:
                    msg += f"... and {len(roi_ready) - 5} more ready for exit\n\n"

            msg += f"Use `/force_roi_check` to exit ready trades"

        except RPCException as e:
            msg = f"❌ Error checking ROI targets: {str(e)}"

        await self._send_msg(msg, ParseMode.MARKDOWN)

    @authorized_only
    async def _auto_roi_mode(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /auto_roi_mode on/off - Enable/disable automatic ROI checking
        """
        if not context.args or context.args[0] not in ["on", "off"]:
            await self._send_msg(
                "Usage: `/auto_roi_mode on` or `/auto_roi_mode off`", ParseMode.MARKDOWN
            )
            return

        enable = context.args[0] == "on"

        if "auto_roi" not in self._config:
            self._config["auto_roi"] = {}

        self._config["auto_roi"]["enabled"] = enable

        if enable:
            msg = "🔄 **Automatic ROI checking ENABLED**\n\n"
            msg += "Bot will automatically exit trades when they reach ROI targets\n"
            msg += "Use `/check_roi_targets` to see current status"
        else:
            msg = "⏸️ **Automatic ROI checking DISABLED**\n\n"
            msg += "Use `/force_roi_check` to manually check and exit trades"

        await self._send_msg(msg, ParseMode.MARKDOWN)

    # ====== TREND CONTROL COMMANDS ======

    @authorized_only
    async def _trend_set(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /trendset <direction> [hours] - Set trend direction
        Usage: /trendset bull 24   - Set bullish trend for 24 hours
               /trendset bear 12   - Set bearish trend for 12 hours
               /trendset neutral 6 - Set neutral for 6 hours
        """
        if not context.args or len(context.args) < 1:
            msg = "Usage: `/trendset <direction> [hours]`\n\n"
            msg += "**Directions:**\n"
            msg += "• `bull` - Only long positions\n"
            msg += "• `bear` - Only short positions\n"
            msg += "• `neutral` - Both directions allowed\n\n"
            msg += "**Examples:**\n"
            msg += "• `/trendset bull 24` - Bull trend for 24h\n"
            msg += "• `/trendset bear 12` - Bear trend for 12h\n"
            msg += "• `/trendset neutral` - Neutral (default 24h)"
            await self._send_msg(msg, ParseMode.MARKDOWN)
            return

        direction = context.args[0].lower()

        if direction not in ["bull", "bear", "neutral"]:
            await self._send_msg(
                "❌ Invalid direction. Use: `bull`, `bear`, or `neutral`", ParseMode.MARKDOWN
            )
            return

        # Parse hours (default 24)
        hours = 24
        if len(context.args) > 1:
            try:
                hours = int(context.args[1])
                if hours <= 0 or hours > 168:  # Max 1 week
                    await self._send_msg("❌ Hours must be between 1 and 168 (1 week)")
                    return
            except ValueError:
                await self._send_msg("❌ Invalid hours. Please provide a number.")
                return

        # Get trend controller and set direction
        controller = get_trend_controller()
        success = controller.set_trend_direction(direction, hours)

        if success:
            direction_emoji = {"bull": "📈", "bear": "📉", "neutral": "⚖️"}

            direction_desc = {
                "bull": "BULLISH (Longs only)",
                "bear": "BEARISH (Shorts only)",
                "neutral": "NEUTRAL (Both directions)",
            }

            msg = f"{direction_emoji[direction]} **Trend set to {direction_desc[direction]}**\n\n"
            msg += f"⏰ Duration: {hours} hours\n"
            msg += f"⏱ Expires: {datetime.now() + timedelta(hours=hours)}\n\n"
            msg += "Use `/trendstatus` to check current settings\n"
            msg += "Use `/trendclear` to clear before expiration"

            await self._send_msg(msg, ParseMode.MARKDOWN)
        else:
            await self._send_msg("❌ Failed to set trend direction. Check logs for details.")

    @authorized_only
    async def _trend_get(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /trendget - Get current trend direction
        """
        controller = get_trend_controller()
        direction = controller.get_trend_direction()

        if direction:
            direction_emoji = {"bull": "📈", "bear": "📉", "neutral": "⚖️"}

            direction_desc = {
                "bull": "BULLISH (Longs only)",
                "bear": "BEARISH (Shorts only)",
                "neutral": "NEUTRAL (Both directions)",
            }

            cache_info = controller.get_cache_info()

            msg = f"{direction_emoji[direction]} **Current Trend: {direction_desc[direction]}**\n\n"

            if cache_info.get("time_remaining"):
                msg += f"⏱ Time remaining: {cache_info['time_remaining']}\n"

            msg += "\nUse `/trendstatus` for detailed info"
        else:
            msg = "ℹ️ **No trend override set**\n\n"
            msg += "Strategy is using its normal logic.\n\n"
            msg += "Use `/trendset <direction> [hours]` to set a trend"

        await self._send_msg(msg, ParseMode.MARKDOWN)

    @authorized_only
    async def _trend_clear(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /trendclear - Clear current trend direction
        """
        controller = get_trend_controller()
        success = controller.clear_trend_direction()

        if success:
            msg = "✅ **Trend direction cleared**\n\n"
            msg += "Strategy will now use its normal logic.\n\n"
            msg += "Use `/trendset` to set a new trend"
            await self._send_msg(msg, ParseMode.MARKDOWN)
        else:
            await self._send_msg("❌ Failed to clear trend direction. Check logs for details.")

    @authorized_only
    async def _trend_status(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /trendstatus - Show detailed trend status
        """
        controller = get_trend_controller()
        cache_info = controller.get_cache_info()

        if cache_info["status"] == "empty":
            msg = "ℹ️ **No Trend Override Active**\n\n"
            msg += "All settings use strategy defaults.\n\n"
            msg += "**Available Commands:**\n"
            msg += "• `/trendset bull|bear|neutral [hours]`\n"
            msg += "• `/longall` - Quick bull trend (24h)\n"
            msg += "• `/shortall` - Quick bear trend (24h)\n"
            msg += "• `/trendhold on|off [hours]`"
        elif cache_info["status"] == "expired":
            msg = "⚠️ **Trend Settings Expired**\n\n"
            msg += "Cache has expired and been cleared.\n\n"
            msg += "Use `/trendset` to set a new trend"
        else:
            msg = "📊 **Trend Control Status**\n\n"

            # Direction
            if cache_info.get("direction"):
                direction_emoji = {"bull": "📈", "bear": "📉", "neutral": "⚖️"}
                msg += f"**Direction:** {direction_emoji[cache_info['direction']]} {cache_info['direction'].upper()}\n"
            else:
                msg += "**Direction:** None\n"

            # Stoploss
            if cache_info.get("stoploss") is not None:
                msg += f"**Stop Loss:** {cache_info['stoploss']:.2%}\n"
            else:
                msg += "**Stop Loss:** Default\n"

            # Hold on trend
            if cache_info.get("hold_on_trend") is not None:
                hold_status = "ON" if cache_info["hold_on_trend"] else "OFF"
                msg += f"**Hold on Trend:** {hold_status}\n"
            else:
                msg += "**Hold on Trend:** OFF\n"

            # Timing
            if cache_info.get("time_remaining"):
                msg += f"\n⏱ **Time Remaining:** {cache_info['time_remaining']}\n"

            if cache_info.get("set_at"):
                try:
                    set_time = datetime.fromisoformat(cache_info["set_at"])
                    msg += f"⏰ **Set At:** {set_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                except:
                    pass

            msg += "\n**Quick Actions:**\n"
            msg += "• `/trendclear` - Clear all settings\n"
            msg += "• `/trendset <dir> [hrs]` - Change trend"

        await self._send_msg(msg, ParseMode.MARKDOWN)

    @authorized_only
    async def _trend_hold(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /trendhold on|off [hours] - Enable/disable hold on trend
        """
        if not context.args or len(context.args) < 1:
            msg = "Usage: `/trendhold on|off [hours]`\n\n"
            msg += "**Hold on Trend:**\n"
            msg += "When enabled, positions aligned with trend won't take profits.\n\n"
            msg += "**Examples:**\n"
            msg += "• `/trendhold on 24` - Hold for 24h\n"
            msg += "• `/trendhold off` - Disable hold"
            await self._send_msg(msg, ParseMode.MARKDOWN)
            return

        mode = context.args[0].lower()

        if mode not in ["on", "off"]:
            await self._send_msg("❌ Invalid mode. Use: `on` or `off`", ParseMode.MARKDOWN)
            return

        controller = get_trend_controller()

        if mode == "off":
            # Clear hold setting by setting to False with 0 hours
            success = controller.set_hold_on_trend(False, 1)
            if success:
                msg = "✅ **Hold on Trend DISABLED**\n\n"
                msg += "Positions will take profits normally."
                await self._send_msg(msg, ParseMode.MARKDOWN)
            else:
                await self._send_msg("❌ Failed to disable hold on trend.")
            return

        # Parse hours (default 24)
        hours = 24
        if len(context.args) > 1:
            try:
                hours = int(context.args[1])
                if hours <= 0 or hours > 168:
                    await self._send_msg("❌ Hours must be between 1 and 168 (1 week)")
                    return
            except ValueError:
                await self._send_msg("❌ Invalid hours. Please provide a number.")
                return

        success = controller.set_hold_on_trend(True, hours)

        if success:
            msg = "✅ **Hold on Trend ENABLED**\n\n"
            msg += f"⏰ Duration: {hours} hours\n\n"
            msg += "Positions aligned with trend will be held without profit exits.\n"
            msg += "Counter-trend positions will take profits normally."
            await self._send_msg(msg, ParseMode.MARKDOWN)
        else:
            await self._send_msg("❌ Failed to enable hold on trend.")

    @authorized_only
    async def _trend_longall(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /longall - Quick set to bull trend for 24h
        """
        # Show confirmation dialog
        msg = "⚠️ **Confirm Bull Trend**\n\n"
        msg += "Set trend to **BULL** for 24 hours?\n"
        msg += "Only LONG positions will be allowed."

        keyboard = [
            [
                InlineKeyboardButton(
                    text="✅ Yes", callback_data="confirm_trend_longall__yes"
                ),
                InlineKeyboardButton(text="❌ No", callback_data="confirm_trend_longall__no"),
            ]
        ]
        await self._send_msg(msg, keyboard=keyboard, parse_mode=ParseMode.MARKDOWN)

    async def _trend_longall_callback(
        self, update: Update, context: CallbackContext
    ) -> None:
        """Callback handler for /trend_longall confirmation"""
        if update.callback_query:
            query = update.callback_query
            await query.answer()

            if query.data and "__" in query.data:
                action = query.data.split("__")[1]

                if action == "no":
                    await query.edit_message_text(text="❌ Bull trend canceled.")
                    return

                # User confirmed - execute trend_longall
                controller = get_trend_controller()
                success = controller.set_trend_direction("bull", 24)

                if success:
                    msg = "📈 **BULL TREND ACTIVATED**\n\n"
                    msg += "✅ Only LONG positions allowed\n"
                    msg += "⏰ Duration: 24 hours\n\n"
                    msg += "Use `/trendstatus` to check settings"
                    await query.edit_message_text(text=msg)
                else:
                    await query.edit_message_text(text="❌ Failed to set bull trend.")

    @authorized_only
    async def _trend_shortall(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /shortall - Quick set to bear trend for 24h
        """
        # Show confirmation dialog
        msg = "⚠️ **Confirm Bear Trend**\n\n"
        msg += "Set trend to **BEAR** for 24 hours?\n"
        msg += "Only SHORT positions will be allowed."

        keyboard = [
            [
                InlineKeyboardButton(
                    text="✅ Yes", callback_data="confirm_trend_shortall__yes"
                ),
                InlineKeyboardButton(text="❌ No", callback_data="confirm_trend_shortall__no"),
            ]
        ]
        await self._send_msg(msg, keyboard=keyboard, parse_mode=ParseMode.MARKDOWN)

    async def _trend_shortall_callback(
        self, update: Update, context: CallbackContext
    ) -> None:
        """Callback handler for /trend_shortall confirmation"""
        if update.callback_query:
            query = update.callback_query
            await query.answer()

            if query.data and "__" in query.data:
                action = query.data.split("__")[1]

                if action == "no":
                    await query.edit_message_text(text="❌ Bear trend canceled.")
                    return

                # User confirmed - execute trend_shortall
                controller = get_trend_controller()
                success = controller.set_trend_direction("bear", 24)

                if success:
                    msg = "📉 **BEAR TREND ACTIVATED**\n\n"
                    msg += "✅ Only SHORT positions allowed\n"
                    msg += "⏰ Duration: 24 hours\n\n"
                    msg += "Use `/trendstatus` to check settings"
                    await query.edit_message_text(text=msg)
                else:
                    await query.edit_message_text(text="❌ Failed to set bear trend.")

    @authorized_only
    async def _trend_neutral(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /trendneutral - Quick set to neutral trend for 24h
        """
        controller = get_trend_controller()
        success = controller.set_trend_direction("neutral", 24)

        if success:
            msg = "⚖️ **NEUTRAL TREND ACTIVATED**\n\n"
            msg += "✅ Both LONG and SHORT positions allowed\n"
            msg += "⏰ Duration: 24 hours\n\n"
            msg += "Use `/trendstatus` to check settings"
            await self._send_msg(msg, ParseMode.MARKDOWN)
        else:
            await self._send_msg("❌ Failed to set neutral trend.")

    @authorized_only
    async def _logs(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /logs
        Shows the latest logs
        """
        try:
            limit = int(context.args[0]) if context.args else 10
        except (TypeError, ValueError, IndexError):
            limit = 10
        logs = RPC._rpc_get_logs(limit)["logs"]
        msgs = ""
        msg_template = "*{}* {}: {} \\- `{}`"
        for logrec in logs:
            msg = msg_template.format(
                escape_markdown(logrec[0], version=2),
                escape_markdown(logrec[2], version=2),
                escape_markdown(logrec[3], version=2),
                escape_markdown(logrec[4], version=2),
            )
            if len(msgs + msg) + 10 >= MAX_MESSAGE_LENGTH:
                # Send message immediately if it would become too long
                await self._send_msg(msgs, parse_mode=ParseMode.MARKDOWN_V2)
                msgs = msg + "\n"
            else:
                # Append message to messages to send
                msgs += msg + "\n"

        if msgs:
            await self._send_msg(msgs, parse_mode=ParseMode.MARKDOWN_V2)

    @authorized_only
    async def _help(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /help.
        Show commands of the bot
        :param bot: telegram bot
        :param update: message update
        :return: None
        """
        force_enter_text = (
            "*/forcelong <pair> [<rate>]:* `Instantly buys the given pair. "
            "Optionally takes a rate at which to buy "
            "(only applies to limit orders).` \n"
        )
        if self._rpc._freqtrade.trading_mode != TradingMode.SPOT:
            force_enter_text += (
                "*/forceshort <pair> [<rate>]:* `Instantly shorts the given pair. "
                "Optionally takes a rate at which to sell "
                "(only applies to limit orders).` \n"
            )

        # Split help message into chunks to avoid Telegram's character limit
        help_sections = [
            # Section 1: Bot Control
            (
                "_Bot Control_\n"
                "------------\n"
                "*/start:* `Starts the trader`\n"
                "*/pause:* `Pause the new entries for trader, but handles open trades gracefully`\n"
                "*/stop:* `Stops the trader`\n"
                "*/stopentry:* `Stops entering, but handles open trades gracefully` \n"
                "*/forceexit <trade_id>|all:* `Instantly exits the given trade or all trades, "
                "regardless of profit`\n"
                "*/fx <trade_id>|all:* `Alias to /forceexit`\n"
                f"{force_enter_text if self._config.get('force_entry_enable', False) else ''}"
                "*/longall:* `Open long positions for all pairs in whitelist`\n"
                "*/shortall:* `Open short positions for all pairs in whitelist`\n"
                "*/closelong:* `Close all open long trades`\n"
                "*/closeshort:* `Close all open short trades`\n"
                "*/delete <trade_id>:* `Instantly delete the given trade in the database`\n"
                "*/reload_trade <trade_id>:* `Reload trade from exchange Orders`\n"
                "*/cancel_open_order <trade_id>:* `Cancels open orders for trade. "
                "Only valid when the trade has open orders.`\n"
                "*/coo <trade_id>|all:* `Alias to /cancel_open_order`\n"
                "*/whitelist [sorted] [baseonly]:* `Show current whitelist. Optionally in "
                "order and/or only displaying the base currency of each pairing.`\n"
                "*/blacklist [pair]:* `Show current blacklist, or adds one or more pairs "
                "to the blacklist.` \n"
                "*/blacklist_delete [pairs]| /bl_delete [pairs]:* "
                "`Delete pair / pattern from blacklist. Will reset on reload_conf.` \n"
                "*/enable_pairs [pairs]| /enable [pairs]:* "
                "`Enable trading for specified pairs (remove from blacklist).` \n"
                "*/disable_pairs [pairs]| /disable [pairs]:* "
                "`Disable trading for specified pairs (add to blacklist).` \n"
                "*/reload_config:* `Reload configuration file` \n"
                "*/unlock <pair|id>:* `Unlock this Pair (or this lock id if it's numeric)`\n"
            ),
            # Section 2: Current State & Statistics
            (
                "_Current State & Statistics_\n"
                "----------------------------\n"
                "*/show_config:* `Show running configuration` \n"
                "*/locks:* `Show currently locked pairs`\n"
                "*/balance:* `Show bot managed balance per currency`\n"
                "*/balance total:* `Show account balance per currency`\n"
                "*/logs [limit]:* `Show latest logs - defaults to 10` \n"
                "*/count:* `Show number of active trades compared to allowed number of trades`\n"
                "*/health* `Show latest process timestamp - defaults to 1970-01-01 00:00:00` \n"
                "*/marketdir [long | short | even | none]:* `Updates the user managed variable "
                "that represents the current market direction. If no direction is provided "
                "the currently set market direction will be output.` \n"
                "*/list_custom_data <trade_id> <key>:* `List custom_data for Trade ID & Key combo. "
                "If no Key is supplied it will list all key-value pairs found for that Trade ID.`\n"
                "*/status <trade_id>|[table]:* `Lists all open trades`\n"
                "         *<trade_id> :* `Lists one or more specific trades.`\n"
                "                        `Separate multiple <trade_id> with a blank space.`\n"
                "         *table :* `will display trades in a table`\n"
                "                `pending buy orders are marked with an asterisk (*)`\n"
                "                `pending sell orders are marked with a double asterisk (**)`\n"
                "*/entries <pair|none>:* `Shows the enter_tag performance`\n"
                "*/exits <pair|none>:* `Shows the exit reason performance`\n"
                "*/mix_tags <pair|none>:* `Shows combined entry tag + exit reason performance`\n"
                "*/trades [limit]:* `Lists last closed trades (limited to 10 by default)`\n"
            ),
            # Section 3: Profit & Performance
            (
                "_Profit & Performance_\n"
                "---------------------\n"
                "*/profit [<n>]:* `Lists cumulative profit from all finished trades, "
                "over the last n days`\n"
                "*/profit_long [<n>]:* `Lists cumulative profit from all finished long trades, "
                "over the last n days`\n"
                "*/profit_short [<n>]:* `Lists cumulative profit from all finished short trades, "
                "over the last n days`\n"
                "*/performance:* `Show performance of each finished trade grouped by pair`\n"
                "*/daily <n>:* `Shows profit or loss per day, over the last n days`\n"
                "*/weekly <n>:* `Shows statistics per week, over the last n weeks`\n"
                "*/monthly <n>:* `Shows statistics per month, over the last n months`\n"
                "*/stats:* `Shows Wins / losses by Sell reason as well as "
                "Avg. holding durations for buys and sells.`\n"
                "*/qs:* `Quick status - compact one-line summary`\n"
                "*/pnl [today/week/month]:* `P&L summary for period`\n"
                "*/risk_status:* `Current risk exposure metrics`\n"
                "*/watchlist:* `Show pairs available for entry`\n"
                "*/top_gainers [n]:* `Best performing pairs`\n"
                "*/top_losers [n]:* `Worst performing pairs`\n"
                "*/market_sentiment:* `Overall market direction`\n"
            ),
            # Section 4: Risk Management & Profit Control
            (
                "_Risk Management & Profit Control_\n"
                "----------------------------------\n"
                "*/setmaxopen <n>:* `Set max open trades`\n"
                "*/emergency_stop:* `Stop bot and close all trades`\n"
                "*/pausefor <min>:* `Pause bot for X minutes`\n"
                "*/set_stoploss <pct>:* `Update stop-loss percentage`\n"
                "*/close_profitable:* `Close all profitable trades`\n"
                "*/close_losing:* `Close all losing trades`\n"
                "*/disable_roi:* `Disable profit exits - let trends run`\n"
                "*/enable_roi:* `Re-enable profit-taking exits`\n"
                "*/profit_mode <mode>:* `Set profit behavior (disabled/normal/aggressive)`\n"
                "*/profit_status:* `Show current profit settings`\n"
                "*/set_roi <pct>:* `Set simple profit target percentage`\n"
                "*/set_roi_long <pct>:* `Set profit target for long trades only`\n"
                "*/set_roi_short <pct>:* `Set profit target for short trades only`\n"
                "*/disable_roi_long:* `Disable profit exits for longs only`\n"
                "*/disable_roi_short:* `Disable profit exits for shorts only`\n"
                "*/roi_status:* `Show separate long/short ROI settings`\n"
                "*/menu_trade:* `Show trading commands menu`\n"
                "*/menu_risk:* `Show risk management menu`\n"
                "*/menu_status:* `Show monitoring commands menu`\n"
            ),
            # Section 5: Trend Control
            (
                "_Trend Control_\n"
                "---------------\n"
                "*/trendset <bull|bear|neutral> [hrs]:* `Set trend direction (blocks opposite entries)`\n"
                "*/longall:* `Quick: Bull trend for 24h (longs only)`\n"
                "*/shortall:* `Quick: Bear trend for 24h (shorts only)`\n"
                "*/trendneutral:* `Quick: Neutral trend for 24h (both directions)`\n"
                "*/trendget:* `Show current active trend`\n"
                "*/trendstatus:* `Detailed trend control status`\n"
                "*/trendhold <on|off> [hrs]:* `Hold positions aligned with trend (no profit exits)`\n"
                "*/trendclear:* `Clear all trend settings`\n"
                "\n"
                "_Other Commands_\n"
                "----------------\n"
                "*/help:* `This help message`\n"
                "*/version:* `Show version`\n"
            ),
        ]

        # Send each section as a separate message
        for i, section in enumerate(help_sections, 1):
            header = f"*Help ({i}/{len(help_sections)})*\n\n" if len(help_sections) > 1 else ""
            message = header + section
            await self._send_msg(message, parse_mode=ParseMode.MARKDOWN)

    @authorized_only
    async def _health(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /health
        Shows the last process timestamp
        """
        health = self._rpc.health()
        message = f"Last process: `{health['last_process_loc']}`\n"
        message += f"Initial bot start: `{health['bot_start_loc']}`\n"
        message += f"Last bot restart: `{health['bot_startup_loc']}`"
        await self._send_msg(message)

    @authorized_only
    async def _version(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /version.
        Show version information
        :param bot: telegram bot
        :param update: message update
        :return: None
        """
        strategy_version = self._rpc._freqtrade.strategy.version()
        version_string = f"*Version:* `{__version__}`"
        if strategy_version is not None:
            version_string += f"\n*Strategy version: * `{strategy_version}`"

        await self._send_msg(version_string)

    @authorized_only
    async def _show_config(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /show_config.
        Show config information information
        :param bot: telegram bot
        :param update: message update
        :return: None
        """
        val = RPC._rpc_show_config(self._config, self._rpc._freqtrade.state)

        if val["trailing_stop"]:
            sl_info = (
                f"*Initial Stoploss:* `{val['stoploss']}`\n"
                f"*Trailing stop positive:* `{val['trailing_stop_positive']}`\n"
                f"*Trailing stop offset:* `{val['trailing_stop_positive_offset']}`\n"
                f"*Only trail above offset:* `{val['trailing_only_offset_is_reached']}`\n"
            )

        else:
            sl_info = f"*Stoploss:* `{val['stoploss']}`\n"

        if val["position_adjustment_enable"]:
            pa_info = (
                f"*Position adjustment:* On\n"
                f"*Max enter position adjustment:* `{val['max_entry_position_adjustment']}`\n"
            )
        else:
            pa_info = "*Position adjustment:* Off\n"

        await self._send_msg(
            f"*Mode:* `{'Dry-run' if val['dry_run'] else 'Live'}`\n"
            f"*Exchange:* `{val['exchange']}`\n"
            f"*Market: * `{val['trading_mode']}`\n"
            f"*Stake per trade:* `{val['stake_amount']} {val['stake_currency']}`\n"
            f"*Max open Trades:* `{val['max_open_trades']}`\n"
            f"*Minimum ROI:* `{val['minimal_roi']}`\n"
            f"*Entry strategy:* ```\n{json.dumps(val['entry_pricing'])}```\n"
            f"*Exit strategy:* ```\n{json.dumps(val['exit_pricing'])}```\n"
            f"{sl_info}"
            f"{pa_info}"
            f"*Timeframe:* `{val['timeframe']}`\n"
            f"*Strategy:* `{val['strategy']}`\n"
            f"*Current state:* `{val['state']}`"
        )

    @authorized_only
    async def _list_custom_data(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /list_custom_data <id> <key>.
        List custom_data for specified trade (and key if supplied).
        :param bot: telegram bot
        :param update: message update
        :return: None
        """
        try:
            if not context.args or len(context.args) == 0:
                raise RPCException("Trade-id not set.")
            trade_id = int(context.args[0])
            key = None if len(context.args) < 2 else str(context.args[1])

            results = self._rpc._rpc_list_custom_data(trade_id, key)
            messages = []
            if len(results) > 0:
                trade_custom_data = results[0]["custom_data"]
                messages.append(
                    "Found custom-data entr" + ("ies: " if len(trade_custom_data) > 1 else "y: ")
                )
                for custom_data in trade_custom_data:
                    lines = [
                        f"*Key:* `{custom_data['key']}`",
                        f"*Type:* `{custom_data['type']}`",
                        f"*Value:* `{custom_data['value']}`",
                        f"*Create Date:* `{format_date(custom_data['created_at'])}`",
                        f"*Update Date:* `{format_date(custom_data['updated_at'])}`",
                    ]
                    # Filter empty lines using list-comprehension
                    messages.append("\n".join([line for line in lines if line]))
                for msg in messages:
                    if len(msg) > MAX_MESSAGE_LENGTH:
                        msg = "Message dropped because length exceeds "
                        msg += f"maximum allowed characters: {MAX_MESSAGE_LENGTH}"
                        logger.warning(msg)
                    await self._send_msg(msg)
            else:
                message = f"Didn't find any custom-data entries for Trade ID: `{trade_id}`"
                message += f" and Key: `{key}`." if key is not None else ""
                await self._send_msg(message)

        except RPCException as e:
            await self._send_msg(str(e))

    async def _update_msg(
        self,
        query: CallbackQuery,
        msg: str,
        callback_path: str = "",
        reload_able: bool = False,
        parse_mode: str = ParseMode.MARKDOWN,
    ) -> None:
        if reload_able:
            reply_markup = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Refresh", callback_data=callback_path)],
                ]
            )
        else:
            reply_markup = InlineKeyboardMarkup([[]])
        msg += f"\nUpdated: {datetime.now().ctime()}"
        if not query.message:
            return

        try:
            await query.edit_message_text(
                text=msg, parse_mode=parse_mode, reply_markup=reply_markup
            )
        except BadRequest as e:
            if "not modified" in e.message.lower():
                pass
            else:
                logger.warning("TelegramError: %s", e.message)
        except TelegramError as telegram_err:
            logger.warning("TelegramError: %s! Giving up on that message.", telegram_err.message)

    async def _send_msg(
        self,
        msg: str,
        parse_mode: str = ParseMode.MARKDOWN,
        disable_notification: bool = False,
        keyboard: list[list[InlineKeyboardButton]] | None = None,
        callback_path: str = "",
        reload_able: bool = False,
        query: CallbackQuery | None = None,
    ) -> None:
        """
        Send given markdown message
        :param msg: message
        :param bot: alternative bot
        :param parse_mode: telegram parse mode
        :return: None
        """
        reply_markup: InlineKeyboardMarkup | ReplyKeyboardMarkup
        if query:
            await self._update_msg(
                query=query,
                msg=msg,
                parse_mode=parse_mode,
                callback_path=callback_path,
                reload_able=reload_able,
            )
            return
        if reload_able and self._config["telegram"].get("reload", True):
            reply_markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton("Refresh", callback_data=callback_path)]]
            )
        else:
            if keyboard is not None:
                reply_markup = InlineKeyboardMarkup(keyboard)
            else:
                reply_markup = ReplyKeyboardMarkup(self._keyboard, resize_keyboard=True)
        try:
            try:
                await self._app.bot.send_message(
                    self._config["telegram"]["chat_id"],
                    text=msg,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                    disable_notification=disable_notification,
                    message_thread_id=self._config["telegram"].get("topic_id"),
                )
            except NetworkError as network_err:
                # Sometimes the telegram server resets the current connection,
                # if this is the case we send the message again.
                logger.warning(
                    "Telegram NetworkError: %s! Trying one more time.", network_err.message
                )
                await self._app.bot.send_message(
                    self._config["telegram"]["chat_id"],
                    text=msg,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                    disable_notification=disable_notification,
                    message_thread_id=self._config["telegram"].get("topic_id"),
                )
        except TelegramError as telegram_err:
            logger.warning("TelegramError: %s! Giving up on that message.", telegram_err.message)

    @authorized_only
    async def _changemarketdir(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /marketdir.
        Updates the bot's market_direction
        :param bot: telegram bot
        :param update: message update
        :return: None
        """
        if context.args and len(context.args) == 1:
            new_market_dir_arg = context.args[0]
            old_market_dir = self._rpc._get_market_direction()
            new_market_dir = None
            if new_market_dir_arg == "long":
                new_market_dir = MarketDirection.LONG
            elif new_market_dir_arg == "short":
                new_market_dir = MarketDirection.SHORT
            elif new_market_dir_arg == "even":
                new_market_dir = MarketDirection.EVEN
            elif new_market_dir_arg == "none":
                new_market_dir = MarketDirection.NONE

            if new_market_dir is not None:
                self._rpc._update_market_direction(new_market_dir)
                await self._send_msg(
                    "Successfully updated market direction"
                    f" from *{old_market_dir}* to *{new_market_dir}*."
                )
            else:
                raise RPCException(
                    "Invalid market direction provided. \n"
                    "Valid market directions: *long, short, even, none*"
                )
        elif context.args is not None and len(context.args) == 0:
            old_market_dir = self._rpc._get_market_direction()
            await self._send_msg(f"Currently set market direction: *{old_market_dir}*")
        else:
            raise RPCException(
                "Invalid usage of command /marketdir. \n"
                "Usage: */marketdir [short |  long | even | none]*"
            )

    @authorized_only
    async def _enablelong(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /enablelong.
        Enables long positions.
        """
        current_dir = self._rpc._get_market_direction()
        
        if current_dir == MarketDirection.LONG or current_dir == MarketDirection.EVEN:
            await self._send_msg("✅ Long positions are already enabled.")
            return
        
        # If currently SHORT, switch to EVEN (both enabled)
        # If currently NONE, switch to LONG (only long enabled)
        if current_dir == MarketDirection.SHORT:
            new_dir = MarketDirection.EVEN
        else:  # NONE
            new_dir = MarketDirection.LONG
        
        self._rpc._update_market_direction(new_dir)
        await self._send_msg(
            f"✅ Long positions enabled.\n"
            f"Market direction: *{current_dir}* → *{new_dir}*"
        )

    @authorized_only
    async def _enableshort(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /enableshort.
        Enables short positions.
        """
        current_dir = self._rpc._get_market_direction()
        
        if current_dir == MarketDirection.SHORT or current_dir == MarketDirection.EVEN:
            await self._send_msg("✅ Short positions are already enabled.")
            return
        
        # If currently LONG, switch to EVEN (both enabled)
        # If currently NONE, switch to SHORT (only short enabled)
        if current_dir == MarketDirection.LONG:
            new_dir = MarketDirection.EVEN
        else:  # NONE
            new_dir = MarketDirection.SHORT
        
        self._rpc._update_market_direction(new_dir)
        await self._send_msg(
            f"✅ Short positions enabled.\n"
            f"Market direction: *{current_dir}* → *{new_dir}*"
        )

    @authorized_only
    async def _disablelong(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /disablelong.
        Disables long positions.
        """
        current_dir = self._rpc._get_market_direction()
        
        if current_dir == MarketDirection.SHORT or current_dir == MarketDirection.NONE:
            await self._send_msg("✅ Long positions are already disabled.")
            return
        
        # If currently LONG, switch to NONE (all disabled)
        # If currently EVEN, switch to SHORT (only short enabled)
        if current_dir == MarketDirection.LONG:
            new_dir = MarketDirection.NONE
        else:  # EVEN
            new_dir = MarketDirection.SHORT
        
        self._rpc._update_market_direction(new_dir)
        await self._send_msg(
            f"⛔ Long positions disabled.\n"
            f"Market direction: *{current_dir}* → *{new_dir}*"
        )

    @authorized_only
    async def _disableshort(self, update: Update, context: CallbackContext) -> None:
        """
        Handler for /disableshort.
        Disables short positions.
        """
        current_dir = self._rpc._get_market_direction()
        
        if current_dir == MarketDirection.LONG or current_dir == MarketDirection.NONE:
            await self._send_msg("✅ Short positions are already disabled.")
            return
        
        # If currently SHORT, switch to NONE (all disabled)
        # If currently EVEN, switch to LONG (only long enabled)
        if current_dir == MarketDirection.SHORT:
            new_dir = MarketDirection.NONE
        else:  # EVEN
            new_dir = MarketDirection.LONG
        
        self._rpc._update_market_direction(new_dir)
        await self._send_msg(
            f"⛔ Short positions disabled.\n"
            f"Market direction: *{current_dir}* → *{new_dir}*"
        )

    async def _tg_info(self, update: Update, context: CallbackContext) -> None:
        """
        Intentionally unauthenticated Handler for /tg_info.
        Returns information about the current telegram chat - even if chat_id does not
        correspond to this chat.

        :param update: message update
        :return: None
        """
        if not update.message:
            return
        chat_id = update.message.chat_id
        topic_id = update.message.message_thread_id
        user_id = (
            update.effective_user.id if topic_id is not None and update.effective_user else None
        )

        msg = f"""Freqtrade Bot Info:
        ```json
            {{
                "enabled": true,
                "token": "********",
                "chat_id": "{chat_id}",
                {f'"topic_id": "{topic_id}",' if topic_id else ""}
                {f'//"authorized_users": ["{user_id}"]' if topic_id and user_id else ""}
            }}
        ```
        """
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=msg,
                parse_mode=ParseMode.MARKDOWN_V2,
                message_thread_id=topic_id,
            )
        except TelegramError as telegram_err:
            logger.warning("TelegramError: %s! Giving up on that message.", telegram_err.message)
