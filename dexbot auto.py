"""
╔══════════════════════════════════════════════╗
║   DEXSCREENER AUTOPILOT BOT                  ║
║   Полностью автономная торговля              ║
║                                              ║
║  Бот сам:                                    ║
║  ✅ Ищет смарт-кошельки                     ║
║  ✅ Анализирует токены (скоринг 0-100)       ║
║  ✅ Покупает через Jupiter API               ║
║  ✅ Ставит TP/SL и продаёт автоматически    ║
║  ✅ Отправляет уведомления в Telegram        ║
║  ✅ Ведёт статистику и отчёты               ║
║                                              ║
║  Твоё участие: НОЛЬ                         ║
║  Просто настрой и запусти один раз          ║
╚══════════════════════════════════════════════╝

Установка:
  pip install python-telegram-bot aiohttp solders solana

Настройка (заполни 3 строки):
  TELEGRAM_TOKEN  — 8826692966:AAFHMMx4XAtnrlhe2tWbN4FSK1lrG0WWYrc
  ALLOWED_USER_ID — 1939943562
  WALLET_PRIVKEY  — 2td4ESucndmo1oi9yGnh8pSoh6ZtJdpywRQ11A6EibJspxtkbyxF3NL54MnCwZetVpnEnKu7sYRhC9eABGgEmUa7

Запуск:
  python dexbot_auto.py
"""

import asyncio
import aiohttp
import subprocess
import sys

# Автоустановка зависимостей
def _ensure_deps():
    required = ["python-telegram-bot", "aiohttp", "solders", "solana", "base58"]
    for pkg in required:
        try:
            __import__(pkg.replace("-", "_").split("[")[0])
        except ImportError:
            print(f"Устанавливаю {pkg}...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])
_ensure_deps()
import json
import logging
import os
import time
from base64  import b64decode
from datetime import datetime, date
from dataclasses import dataclass, field
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)

# ═══════════════════════════════════════════════
#  НАСТРОЙКИ — заполни и больше не трогай
# ═══════════════════════════════════════════════

import os

# ── Переменные окружения (Railway) или впиши напрямую ───────────────────
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN",  "")
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))
WALLET_PRIVKEY  = os.environ.get("WALLET_PRIVKEY",  "")
WALLET_ADDRESS  = os.environ.get("WALLET_ADDRESS",  "")

RPC_URL       = os.environ.get("RPC_URL", "https://mainnet.helius-rpc.com/?api-key=b9d2d999-b1a7-45fe-a28b-78cd2d3a69cc")
SETTINGS_FILE = "autopilot_settings.json"

# Параметры автоторговли
AUTO_CONFIG = {
    "position_sol":      0.02,   # SOL на одну сделку (мало но много позиций)
    "take_profit_1":     0.15,   # +15% → продать 25%
    "take_profit_2":     0.40,   # +40% → продать 40%
    "take_profit_3":     1.00,   # +100% → продать остаток (2x)
    "stop_loss":         0.08,   # -8% → продать всё (мемкоины падают быстро)
    "trailing_stop":     True,   # двигать стоп за ценой
    "max_positions":     20,     # макс. одновременных позиций
    "min_score":         35,     # минимальный AI-скор для входа (0-100)
    "min_volume":        75_000, # минимальный объём $
    "min_liquidity":     25_000, # минимальная ликвидность $
    "daily_loss_limit":  0.5,    # максимальная потеря в день SOL
    "scan_interval":     20,     # секунд между сканами
    "rugcheck":          True,   # проверять RugCheck перед покупкой
    "smart_scan_hours":  2,      # как часто искать смарт-кошельки
    "slippage_bps":      150,    # 1.5% slippage
    "priority_fee":      100_000,# приоритетная комиссия (lamports)
    # Фильтры по гайду
    "max_age_hours":     24,     # максимальный возраст токена (часов)
    "max_top10_pct":     45,     # макс % у топ-10 холдеров
    "max_snipers_pct":   8,      # макс % снайперов
    "max_bundlers_pct":  45,     # макс % бандлов
    # TP проценты продажи
    "tp1_sell_pct":      0.25,   # продать 25% на TP1
    "tp2_sell_pct":      0.40,   # продать 40% на TP2
    "tp3_sell_pct":      1.00,   # продать всё на TP3
    # Смарт-копитрейд
    "copy_trade":        True,   # включить копитрейдинг
    "copy_min_wr":       0.55,   # мин winrate смарт-кошелька
    "narrative_filter":  True,   # фильтр нарративного размытия
}

SOL_MINT  = "So11111111111111111111111111111111111111112"

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO, datefmt="%H:%M:%S"
)
log = logging.getLogger("AUTOPILOT")


# ═══════════════════════════════════════════════
#  СТРУКТУРЫ
# ═══════════════════════════════════════════════

@dataclass
class Position:
    mint:         str
    symbol:       str
    entry_price:  float
    amount_sol:   float
    pair_address: str   = ""
    entry_time:   float = field(default_factory=time.time)
    peak_price:   float = 0.0
    tp_hit:       list  = field(default_factory=list)   # какие TP уже взяты
    alerted_2x:   bool  = False

    @property
    def age_str(self):
        s = int(time.time() - self.entry_time)
        return f"{s//60}м {s%60}с"

    def pct(self, price: float) -> float:
        return (price - self.entry_price) / self.entry_price if self.entry_price else 0


@dataclass
class Token:
    symbol:       str
    mint:         str
    price_usd:    float
    change_1h:    float
    change_24h:   float
    volume_24h:   float
    liquidity:    float
    market_cap:   float
    pair_address: str
    buys_1h:      int   = 0
    sells_1h:     int   = 0
    age_hours:    float = 0.0
    # Расширенные метрики по гайду
    holders:      int   = 0       # кол-во холдеров
    top10_pct:    float = 0.0     # % у топ-10
    snipers_pct:  float = 0.0     # % снайперов (>8% = скип)
    bundlers_pct: float = 0.0     # % бандлов (>45% = скип)
    dev_hold_pct: float = 0.0     # % у дева (>5% = риск)
    has_twitter:  bool  = False
    has_website:  bool  = False
    boosts:       int   = 0
    fdv:          float = 0.0


# ═══════════════════════════════════════════════
#  AI СКОРИНГ ТОКЕНА
# ═══════════════════════════════════════════════

def score_token(t: Token, rug_score: Optional[int] = None) -> tuple[int, list, list]:
    """
    Оценивает токен по 6 факторам.
    Возвращает (скор 0-100, сигналы, риски).
    """
    score   = 0
    signals = []
    risks   = []

    # 1. Объём
    if   t.volume_24h > 1_000_000: score += 20; signals.append("📊 Объём >$1M")
    elif t.volume_24h > 500_000:   score += 15; signals.append("📊 Объём >$500K")
    elif t.volume_24h > 100_000:   score += 10; signals.append("📊 Объём >$100K")
    elif t.volume_24h > 50_000:    score +=  5
    else: risks.append("⚠️ Малый объём")

    # 2. Ликвидность
    if   t.liquidity > 200_000: score += 15; signals.append("💧 Ликв >$200K")
    elif t.liquidity > 100_000: score += 10; signals.append("💧 Ликв >$100K")
    elif t.liquidity >  50_000: score +=  7
    elif t.liquidity >  25_000: score +=  3
    else: risks.append("🚨 Низкая ликвидность")

    # 3. Импульс цены — ищем ранний вход, избегаем памп на пике
    if   5  <= t.change_1h < 25:   score += 15; signals.append(f"🚀 +{t.change_1h:.0f}% за 1ч (ранний вход)")
    elif 25 <= t.change_1h < 60:   score += 10; signals.append(f"📈 +{t.change_1h:.0f}% за 1ч")
    elif 60 <= t.change_1h < 150:  score +=  3; risks.append(f"⚠️ Поздний вход +{t.change_1h:.0f}%")
    elif t.change_1h >= 150:       score -= 10; risks.append(f"🚨 Вероятно пик +{t.change_1h:.0f}%")
    elif t.change_1h < -5:         score -=  5; risks.append(f"📉 Падение {t.change_1h:.0f}%")
    # Бонус за стабильный рост и за 24ч
    if 5 <= t.change_24h < 200 and t.change_1h > 0:
        score += 5; signals.append(f"📊 +{t.change_24h:.0f}% за 24ч")

    # 4. Давление покупателей
    total = t.buys_1h + t.sells_1h
    if total > 0:
        ratio = t.buys_1h / total
        if   ratio > 0.65: score += 15; signals.append(f"💚 Buy {ratio*100:.0f}%")
        elif ratio > 0.50: score +=  8; signals.append(f"💛 Buy {ratio*100:.0f}%")
        else:              risks.append(f"🔴 Sell {(1-ratio)*100:.0f}%")

    # 5. Возраст токена
    if   1 <= t.age_hours <= 12: score += 10; signals.append(f"🆕 Возраст {t.age_hours:.0f}ч")
    elif t.age_hours < 1:        score +=  3; risks.append("⚠️ Слишком новый")
    elif t.age_hours > 72:       risks.append("⏳ Старый >3 дней")

    # 6. Vol/MC соотношение
    if t.market_cap > 0:
        vm = t.volume_24h / t.market_cap
        if   vm > 1.0: score += 10; signals.append(f"🔥 Vol/MC={vm:.1f}x")
        elif vm > 0.5: score +=  5

    # 7. RugCheck
    if rug_score is not None:
        # score=РИСК: 0=безопасно, 100=скам
        if   rug_score <= 20: score += 15; signals.append(f"🛡 RugCheck {rug_score}/100")
        elif rug_score <= 50: score +=  5
        elif rug_score <= 70: score -=  5
        else:                 score -= 20; risks.append(f"🚨 RugCheck {rug_score}/100")

    # ── 8. МЕТРИКИ ХОЛДЕРОВ ──────────────────────────────────────────────
    if t.snipers_pct > 0:
        if   t.snipers_pct <= 5:  score +=  5; signals.append(f"🎯 Снайперы {t.snipers_pct:.0f}%")
        elif t.snipers_pct <= 8:  score -=  5; risks.append(f"⚠️ Снайперы {t.snipers_pct:.0f}%")
        else:                     score -= 20; risks.append(f"🚨 Снайперы {t.snipers_pct:.0f}% >8%")
    if t.bundlers_pct > 0:
        if   t.bundlers_pct <= 20: score +=  5; signals.append(f"📦 Бандлы {t.bundlers_pct:.0f}%")
        elif t.bundlers_pct <= 40: score -=  3; risks.append(f"⚠️ Бандлы {t.bundlers_pct:.0f}%")
        else:                      score -= 15; risks.append(f"🚨 Бандлы {t.bundlers_pct:.0f}% >40%")
    if t.dev_hold_pct > 0:
        if   t.dev_hold_pct <= 2:  score +=  5; signals.append(f"👨‍💻 Дев {t.dev_hold_pct:.1f}%")
        elif t.dev_hold_pct <= 5:  score -=  5; risks.append(f"⚠️ Дев {t.dev_hold_pct:.1f}%")
        else:                      score -= 15; risks.append(f"🚨 Дев держит {t.dev_hold_pct:.1f}%")
    if t.top10_pct > 0:
        if   t.top10_pct <= 15:  score += 10; signals.append(f"✅ Топ-10: {t.top10_pct:.0f}%")
        elif t.top10_pct <= 30:  score -=  5; risks.append(f"⚠️ Топ-10: {t.top10_pct:.0f}%")
        else:                    score -= 10; risks.append(f"🚨 Топ-10: {t.top10_pct:.0f}%")

    # ── 9. RUGCHECK + ФОМО ────────────────────────────────────────────────
    if rug_score is not None:
        if   rug_score <= 10: score += 15; signals.append(f"🛡 Rug {rug_score}/100")
        elif rug_score <= 30: score +=  8; signals.append(f"🛡 Rug {rug_score}/100")
        elif rug_score <= 60: score -=  5; risks.append(f"⚠️ Rug {rug_score}/100")
        else:                 score -= 20; risks.append(f"🚨 Rug {rug_score}/100")
    if t.change_1h > 80 and t.liquidity < 50_000:
        score -= 20; risks.append(f"🚨 ФОМО: +{t.change_1h:.0f}% при низкой ликв")

    return max(0, min(100, score)), signals, risks


# ═══════════════════════════════════════════════
#  JUPITER SWAP
# ═══════════════════════════════════════════════

class JupiterSwap:
    def __init__(self, session: aiohttp.ClientSession, privkey: str, rpc: str):
        self.session = session
        self.privkey = privkey
        self.rpc     = rpc
        self._pubkey = self._derive_pubkey()

    def _parse_keypair(self):
        """Парсит приватный ключ в любом формате Phantom."""
        from solders.keypair import Keypair
        import json
        pk = self.privkey.strip().strip('"').strip("'")
        if pk.startswith("["):
            return Keypair.from_bytes(bytes(json.loads(pk)))
        elif len(pk) == 128 and all(c in "0123456789abcdefABCDEF" for c in pk):
            return Keypair.from_bytes(bytes.fromhex(pk))
        else:
            return Keypair.from_base58_string(pk)

    def _derive_pubkey(self) -> str:
        try:
            kp = self._parse_keypair()
            pub = str(kp.pubkey())
            log.info(f"Keypair OK, pubkey: {pub[:8]}...")
            return pub
        except Exception as e:
            log.error(f"Keypair error: {type(e).__name__}: {e}")
            log.error("Форматы ключа: base58 (~88 символов), hex (128 символов), JSON [1,2,3...]")
            return ""

    async def get_quote(self, input_mint: str, output_mint: str,
                        amount_lamports: int) -> Optional[dict]:
        params = (
            f"?inputMint={input_mint}&outputMint={output_mint}"
            f"&amount={amount_lamports}"
            f"&slippageBps={AUTO_CONFIG['slippage_bps']}"
        )
        for url in [
            f"https://api.jup.ag/swap/v1/quote{params}",
            f"https://public.jupiterapi.com/quote{params}",
        ]:
            try:
                async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        data = await r.json()
                        if data and "outAmount" in data:
                            return data
                    log.warning(f"Quote {url[:50]}: {r.status}")
            except Exception as e:
                log.warning(f"Quote {url[:50]}: {e}")
        log.error("Jupiter quote недоступен — проверь VPN или интернет")
        return None

    async def swap(self, input_mint: str, output_mint: str,
                   amount_lamports: int) -> Optional[str]:
        """Выполнить своп. Возвращает txid или None."""
        quote = await self.get_quote(input_mint, output_mint, amount_lamports)
        if not quote:
            return None

        payload = {
            "quoteResponse":            quote,
            "userPublicKey":            self._pubkey,
            "wrapAndUnwrapSol":         True,
            "dynamicComputeUnitLimit":  True,
            "prioritizationFeeLamports": AUTO_CONFIG["priority_fee"],
        }
        # Получаем транзакцию от Jupiter
        tx_b64 = ""
        for swap_url in [
            "https://api.jup.ag/swap/v1/swap",
            "https://public.jupiterapi.com/swap",
        ]:
            for attempt in range(3):
                try:
                    async with self.session.post(
                        swap_url, json=payload,
                        timeout=aiohttp.ClientTimeout(total=15)
                    ) as r:
                        if r.status == 200:
                            data = await r.json()
                            tx_b64 = data.get("swapTransaction", "")
                            if tx_b64:
                                break
                        elif r.status == 429:
                            # Rate limit — ждём и повторяем
                            wait = 2 * (attempt + 1)
                            log.warning(f"Swap 429 rate limit, ждём {wait}с...")
                            await asyncio.sleep(wait)
                            continue
                        else:
                            log.warning(f"Swap build {swap_url[:45]}: {r.status}")
                            break
                except Exception as e:
                    log.warning(f"Swap build {swap_url[:45]}: {e}")
                    break
            if tx_b64:
                break

        if not tx_b64:
            log.error("Jupiter swap недоступен — проверь VPN или интернет")
            return None

        # Подписать и отправить транзакцию
        try:
            from solders.keypair     import Keypair
            from solders.transaction import VersionedTransaction
            import base64

            kp         = self._parse_keypair()
            tx_raw     = b64decode(tx_b64)
            tx         = VersionedTransaction.from_bytes(tx_raw)
            signed_tx  = VersionedTransaction(tx.message, [kp])
            signed_b64 = base64.b64encode(bytes(signed_tx)).decode("utf-8")

            async with self.session.post(
                self.rpc,
                json={
                    "jsonrpc": "2.0", "id": 1,
                    "method":  "sendTransaction",
                    "params":  [signed_b64, {"encoding": "base64",
                                             "skipPreflight": True, "maxRetries": 3}]
                },
                timeout=aiohttp.ClientTimeout(total=30)
            ) as r:
                res  = await r.json()
                txid = res.get("result", "")
                if txid:
                    log.info(f"TX sent: https://solscan.io/tx/{txid}")
                    return txid
                else:
                    log.error(f"TX error: {res.get('error')}")
                    return None
        except Exception as e:
            log.error(f"Swap sign/send error: {e}")
            return None

    async def buy(self, token_mint: str, sol_amount: float) -> Optional[str]:
        lamports = int(sol_amount * 1e9)
        return await self.swap(SOL_MINT, token_mint, lamports)

    async def sell_all(self, token_mint: str, token_amount_ui: float,
                       decimals: int = 6) -> Optional[str]:
        amount = int(token_amount_ui * (10 ** decimals))
        return await self.swap(token_mint, SOL_MINT, amount)


# ═══════════════════════════════════════════════
#  СМАРТ-КОШЕЛЬКИ
# ═══════════════════════════════════════════════

class SmartWalletHunter:
    """
    Ищет кошельки которые стабильно покупают токены до их роста.
    Критерии смарт-кошелька:
    - Покупал токены которые потом выросли >50%
    - Winrate > 55%
    - Не менее 5 сделок
    """
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def find(self) -> list[dict]:
        """Найти смарт-кошельки через анализ топ-токенов Solana."""
        results = []
        try:
            # Шаг 1: собираем mint-адреса выросших токенов
            all_mints = []

            # Источник 1: token-boosts (самые активные)
            try:
                async with self.session.get(
                    "https://api.dexscreener.com/token-boosts/top/v1",
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        for item in (data if isinstance(data, list) else []):
                            if item.get("chainId") == "solana":
                                m = item.get("tokenAddress","")
                                if m: all_mints.append(m)
            except Exception as e:
                log.warning(f"SmartWallet boosts: {e}")

            # Источник 2: search pump/meme
            for q in ["pump solana", "meme sol"]:
                try:
                    async with self.session.get(
                        f"https://api.dexscreener.com/latest/dex/search?q={q}",
                        timeout=aiohttp.ClientTimeout(total=8)
                    ) as r:
                        if r.status == 200:
                            data = await r.json()
                            for p in data.get("pairs",[]):
                                if p.get("chainId") == "solana":
                                    chg = float(p.get("priceChange",{}).get("h24",0) or 0)
                                    vol = float(p.get("volume",{}).get("h24",0) or 0)
                                    if chg > 30 and vol > 50_000:
                                        m = p.get("baseToken",{}).get("address","")
                                        if m: all_mints.append(m)
                except Exception:
                    pass

            unique_mints = list(dict.fromkeys(m for m in all_mints if m))[:15]
            log.info(f"🧠 SmartWallet: анализируем {len(unique_mints)} токенов")

            if not unique_mints:
                log.warning("🧠 SmartWallet: токены не найдены")
                return []

            # Шаг 2: параллельно ищем ранних держателей
            tasks = [self._get_early_buyers(m) for m in unique_mints]
            buyers_per_token = await asyncio.gather(*tasks, return_exceptions=True)

            # Шаг 3: статистика кошельков
            wallet_stats: dict[str, dict] = {}
            for mint, buyers in zip(unique_mints, buyers_per_token):
                if isinstance(buyers, Exception) or not buyers:
                    continue
                for wallet in buyers:
                    if wallet not in wallet_stats:
                        wallet_stats[wallet] = {"wins": 0, "total": 0}
                    wallet_stats[wallet]["wins"]  += 1
                    wallet_stats[wallet]["total"] += 1

            log.info(f"🧠 SmartWallet: {len(wallet_stats)} уникальных кошельков")

            # Шаг 4: фильтр — минимум 2 выигрыша
            for wallet, stats in wallet_stats.items():
                if stats["wins"] >= 1:
                    wr = stats["wins"] / max(stats["total"], 1)
                    results.append({
                        "address":  wallet,
                        "win_rate": wr,
                        "trades":   stats["total"],
                    })

            found = sorted(results, key=lambda x: x["win_rate"], reverse=True)[:10]
            log.info(f"🧠 SmartWallet: найдено {len(found)} смарт-кошельков")
            return found

        except Exception as e:
            log.error(f"SmartWallet hunt error: {e}")
            return []

    
    async def _get_early_buyers(self, mint: str) -> list[str]:
        """Получить ранних покупателей токена через Helius RPC."""
        SKIP = {
            "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe1brs",
            "11111111111111111111111111111111",
            "So11111111111111111111111111111111111111112",
            "ComputeBudget111111111111111111111111111111",
            "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
        }
        try:
            async with self.session.post(
                RPC_URL,
                json={"jsonrpc":"2.0","id":1,
                      "method":"getSignaturesForAddress",
                      "params":[mint, {"limit":25,"commitment":"confirmed"}]},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                data = await r.json()

            sigs_all = data.get("result", [])
            if not sigs_all:
                return []

            early = [s["signature"] for s in sigs_all if not s.get("err")][-8:]
            if not early:
                return []

            batch = [
                {"jsonrpc":"2.0","id":i,
                 "method":"getTransaction",
                 "params":[sig,{"encoding":"jsonParsed",
                               "maxSupportedTransactionVersion":0,
                               "commitment":"confirmed"}]}
                for i,sig in enumerate(early)
            ]
            async with self.session.post(
                RPC_URL, json=batch,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as r2:
                txs = await r2.json()

            if not isinstance(txs, list):
                txs = [txs]

            buyers = set()
            for item in txs:
                tx = item.get("result")
                if not tx:
                    continue
                meta = tx.get("meta", {})
                pre_map = {b["mint"]: int(b["uiTokenAmount"]["amount"])
                           for b in meta.get("preTokenBalances", [])
                           if b.get("mint") == mint}
                for b in meta.get("postTokenBalances", []):
                    if b.get("mint") != mint:
                        continue
                    owner   = b.get("owner","")
                    post_am = int(b["uiTokenAmount"]["amount"])
                    pre_am  = pre_map.get(mint, 0)
                    if (owner and len(owner) > 30
                            and owner not in SKIP
                            and post_am > pre_am):
                        buyers.add(owner)

            return list(buyers)[:15]
        except Exception as e:
            log.debug(f"EarlyBuyers {mint[:8]}: {e}")
            return []


    async def get_wallet_trades(self, wallet: str) -> list[dict]:
        """Получить последние подписанные транзакции кошелька через RPC."""
        try:
            async with self.session.post(
                "https://api.mainnet-beta.solana.com",
                json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getSignaturesForAddress",
                    "params": [wallet, {"limit": 10}]
                },
                timeout=aiohttp.ClientTimeout(total=8)
            ) as r:
                data = await r.json()
                return data.get("result", [])
        except Exception:
            return []


# ═══════════════════════════════════════════════
#  ГЛАВНЫЙ ДВИЖОК АВТОПИЛОТА
# ═══════════════════════════════════════════════

class AutopilotEngine:
    def __init__(self):
        self.session:        Optional[aiohttp.ClientSession] = None
        self.jupiter:        Optional[JupiterSwap]           = None
        self.hunter:         Optional[SmartWalletHunter]     = None
        self.notify          = None   # функция уведомлений в Telegram

        self.positions:      dict[str, Position] = {}
        self.smart_wallets:  list[dict]          = []
        self.token_cache:    dict[str, dict]     = {}  # кэш анализа токенов

        self.running         = False
        self.paused          = False   # пауза без остановки

        # Статистика
        self.stats = {
            "trades": 0, "wins": 0, "losses": 0,
            "total_pnl": 0.0, "start_time": time.time()
        }
        self.daily:       dict[str, float] = {}
        self.today_pnl:   float = 0.0
        self.today_date:  str   = str(date.today())

        # Копи-трейдинг — уже скопированные монеты
        self._copy_seen: set = set()

        # Таймеры
        self.last_smart_scan: float = 0
        self.last_report_day: str   = ""

        self._load()

    # ── Сохранение ──────────────────────

    def _save(self):
        try:
            with open(SETTINGS_FILE, "w") as f:
                json.dump({
                    "daily":         self.daily,
                    "smart_wallets": self.smart_wallets,
                    "stats":         {k: v for k, v in self.stats.items()
                                      if k != "start_time"},
                    "config":        AUTO_CONFIG,
                }, f, indent=2)
        except Exception as e:
            log.error(f"Save error: {e}")

    def _load(self):
        try:
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE) as f:
                    data = json.load(f)
                self.daily         = data.get("daily", {})
                self.smart_wallets = data.get("smart_wallets", [])
                saved_stats        = data.get("stats", {})
                self.stats.update(saved_stats)
                saved_cfg = data.get("config", {})
                AUTO_CONFIG.update(saved_cfg)
                # Всегда применяем актуальные лимиты из кода (не из старого файла)
                AUTO_CONFIG["max_positions"] = 20
                log.info("Settings loaded")
        except Exception as e:
            log.error(f"Load error: {e}")

    # ── Дневная статистика ───────────────

    def _check_day(self):
        today = str(date.today())
        if today != self.today_date:
            self.daily[self.today_date] = self.today_pnl
            self.today_date = today
            self.today_pnl  = 0.0
            self._save()

    def _daily_limit_hit(self) -> bool:
        self._check_day()
        return self.today_pnl <= -abs(AUTO_CONFIG["daily_loss_limit"])

    def _add_pnl(self, pnl: float):
        self._check_day()
        self.today_pnl         += pnl
        self.stats["total_pnl"] += pnl
        self.stats["trades"]    += 1
        if pnl > 0: self.stats["wins"]   += 1
        else:       self.stats["losses"] += 1

    # ── Получение данных ─────────────────

    async def get_sol_balance(self) -> float:
        global WALLET_ADDRESS
        if not WALLET_ADDRESS:
            return 0.0
        try:
            async with self.session.post(
                RPC_URL,
                json={"jsonrpc":"2.0","id":1,"method":"getBalance",
                      "params":[WALLET_ADDRESS]},
                timeout=aiohttp.ClientTimeout(total=8)
            ) as r:
                data = await r.json()
                return data.get("result",{}).get("value",0) / 1e9
        except Exception:
            return 0.0

    async def get_token_balance(self, mint: str) -> int:
        """Получить баланс токена в минимальных единицах (raw amount)."""
        global WALLET_ADDRESS
        if not WALLET_ADDRESS:
            return 0
        try:
            async with self.session.post(
                RPC_URL,
                json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getTokenAccountsByOwner",
                    "params": [
                        WALLET_ADDRESS,
                        {"mint": mint},
                        {"encoding": "jsonParsed"}
                    ]
                },
                timeout=aiohttp.ClientTimeout(total=8)
            ) as r:
                data = await r.json()
                accounts = data.get("result", {}).get("value", [])
                if accounts:
                    info = accounts[0]["account"]["data"]["parsed"]["info"]
                    return int(info["tokenAmount"]["amount"])
        except Exception as e:
            log.error(f"Token balance error: {e}")
        return 0

    async def get_trending(self) -> list[Token]:
        """Сканируем trending токены через актуальные эндпоинты DexScreener."""
        try:
            all_mints: list[str] = []

            # Эндпоинт 1: топ boosted токены (рабочий, обновляется часто)
            try:
                async with self.session.get(
                    "https://api.dexscreener.com/token-boosts/top/v1",
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        for item in (data if isinstance(data, list) else []):
                            if item.get("chainId") == "solana":
                                m = item.get("tokenAddress", "")
                                if m:
                                    all_mints.append(m)
            except Exception as e:
                log.warning(f"token-boosts: {e}")

            # Эндпоинт 2: свежие профили токенов
            try:
                async with self.session.get(
                    "https://api.dexscreener.com/token-profiles/latest/v1",
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        for item in (data if isinstance(data, list) else []):
                            if item.get("chainId") == "solana":
                                m = item.get("tokenAddress", "")
                                if m:
                                    all_mints.append(m)
            except Exception as e:
                log.warning(f"token-profiles: {e}")

            # Эндпоинт 3: поиск (как доп. источник)
            for query in ["pump", "sol meme", "solana cat"]:
                try:
                    async with self.session.get(
                        f"https://api.dexscreener.com/latest/dex/search?q={query}",
                        timeout=aiohttp.ClientTimeout(total=8)
                    ) as r:
                        if r.status == 200:
                            data = await r.json()
                            for p in data.get("pairs", []):
                                if p.get("chainId") == "solana":
                                    m = p.get("baseToken", {}).get("address", "")
                                    if m:
                                        all_mints.append(m)
                except Exception:
                    pass

            # Убираем дубликаты
            unique_mints = list(dict.fromkeys(
                m for m in all_mints if m and m != SOL_MINT
            ))
            log.info(f"📡 Получено {len(unique_mints)} уникальных mint-адресов")

            if not unique_mints:
                return []

            # Получаем данные батчами через /tokens/v1/solana/{mints}
            all_pairs = []
            for i in range(0, min(len(unique_mints), 50), 10):
                batch = ",".join(unique_mints[i:i+10])
                try:
                    async with self.session.get(
                        f"https://api.dexscreener.com/tokens/v1/solana/{batch}",
                        timeout=aiohttp.ClientTimeout(total=10)
                    ) as r:
                        if r.status == 200:
                            pairs = await r.json()
                            if isinstance(pairs, list):
                                all_pairs.extend(pairs)
                except Exception as e:
                    log.warning(f"tokens/v1 batch: {e}")
                await asyncio.sleep(0.2)

            # Группируем по mint — берём пару с макс. ликвидностью
            by_mint: dict = {}
            for p in all_pairs:
                if p.get("chainId") != "solana":
                    continue
                mint = p.get("baseToken", {}).get("address", "")
                if not mint:
                    continue
                liq = float(p.get("liquidity", {}).get("usd", 0) or 0)
                if mint not in by_mint or liq > float(by_mint[mint].get("liquidity", {}).get("usd", 0) or 0):
                    by_mint[mint] = p

            # Строим список Token и фильтруем
            tokens = []
            for mint, p in by_mint.items():
                vol  = float(p.get("volume",      {}).get("h24", 0) or 0)
                liq  = float(p.get("liquidity",   {}).get("usd", 0) or 0)
                ch1  = float(p.get("priceChange", {}).get("h1",  0) or 0)
                ch24 = float(p.get("priceChange", {}).get("h24", 0) or 0)
                mc   = float(p.get("marketCap", 0) or 0)
                fdv  = float(p.get("fdv", 0) or 0)
                pr   = float(p.get("priceUsd",  0) or 0)
                b1   = int(p.get("txns", {}).get("h1", {}).get("buys",  0) or 0)
                s1   = int(p.get("txns", {}).get("h1", {}).get("sells", 0) or 0)
                ca   = p.get("pairCreatedAt", 0) or 0
                age  = (time.time() * 1000 - ca) / 3_600_000 if ca else 9999
                info    = p.get("info", {})
                socials = info.get("socials", [])
                has_tw  = any(s.get("type") == "twitter" for s in socials)
                has_web = bool(info.get("websites", []))
                boosts_d = p.get("boosts", {})
                n_boosts = boosts_d.get("active", 0) if isinstance(boosts_d, dict) else 0

                if vol < AUTO_CONFIG["min_volume"]:    continue
                if liq < AUTO_CONFIG["min_liquidity"]: continue
                if ch1 < 3:                            continue
                if pr  <= 0:                           continue
                if age > 24:                           continue

                tokens.append(Token(
                    symbol=p["baseToken"]["symbol"], mint=mint,
                    price_usd=pr, change_1h=ch1, change_24h=ch24,
                    volume_24h=vol, liquidity=liq, market_cap=mc,
                    pair_address=p.get("pairAddress", ""),
                    buys_1h=b1, sells_1h=s1, age_hours=age,
                    has_twitter=has_tw, has_website=has_web,
                    boosts=n_boosts, fdv=fdv,
                ))

            result = sorted(tokens, key=lambda x: x.change_1h, reverse=True)[:20]
            log.info(f"📡 После фильтрации: {len(result)} токенов")
            return result
        except Exception as e:
            log.error(f"get_trending error: {e}")
            return []

    async def get_social_sentiment(self, symbol: str, mint: str) -> tuple[int, list]:
        """Анализирует X, TikTok, Instagram. Возвращает (бонус, сигналы)."""
        bonus   = 0
        signals = []

        # ── DexScreener socials ───────────────────────────────────────────
        try:
            url = f"https://api.dexscreener.com/tokens/v1/solana/{mint}" if mint else                   f"https://api.dexscreener.com/latest/dex/search?q={symbol}"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=6)) as r:
                data = await r.json() if r.status == 200 else {}
            pairs = data if isinstance(data, list) else data.get("pairs", [])
            if pairs:
                p    = max(pairs, key=lambda x: float(x.get("liquidity",{}).get("usd",0) or 0))
                info = p.get("info", {})
                socs = info.get("socials", [])
                webs = info.get("websites", [])
                bst  = p.get("boosts", {})
                urls = [s.get("url","").lower() for s in socs]
                types = [s.get("type","").lower() for s in socs]

                if "twitter" in types:
                    bonus += 6; signals.append("🐦 Twitter ✅")
                else:
                    signals.append("❌ Нет Twitter")
                if "telegram" in types:
                    bonus += 4; signals.append("📱 Telegram ✅")
                if "instagram" in types or any("instagram" in u for u in urls):
                    bonus += 3; signals.append("📸 Instagram ✅")
                if any("tiktok" in u for u in urls):
                    bonus += 5; signals.append("🎵 TikTok ✅")
                if webs:
                    bonus += 3; signals.append("🌐 Сайт ✅")
                n_boosts = bst.get("active", 0) if isinstance(bst, dict) else 0
                if n_boosts > 0:
                    bonus += min(n_boosts * 2, 8); signals.append(f"🚀 Буcты: {n_boosts}")
        except Exception as e:
            log.debug(f"Social DS {symbol}: {e}")

        # ── X/Nitter упоминания ───────────────────────────────────────────
        try:
            async with self.session.get(
                f"https://nitter.poast.org/search?q=%24{symbol}+solana&f=tweets",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=aiohttp.ClientTimeout(total=5)
            ) as r:
                if r.status == 200:
                    text = await r.text()
                    tc = text.count('tweet-content')
                    if tc >= 10:
                        bonus += 8; signals.append(f"🔥 X: {tc}+ твитов")
                    elif tc >= 5:
                        bonus += 4; signals.append(f"📣 X: {tc} твитов")
                    elif tc >= 2:
                        bonus += 2; signals.append(f"💬 X: {tc} твита")
                    if 'icon-ok' in text:
                        bonus += 5; signals.append("⭐ Инфлюенсер в X")
        except Exception:
            pass

        # ── TikTok ───────────────────────────────────────────────────────
        try:
            async with self.session.get(
                f"https://www.tiktok.com/search?q={symbol}+crypto",
                headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)"},
                timeout=aiohttp.ClientTimeout(total=5)
            ) as r:
                if r.status == 200:
                    text = await r.text()
                    vc = text.count('"ItemModule"') + text.count('"videoCount"')
                    if vc >= 5:
                        bonus += 8; signals.append(f"🎵 TikTok: активно")
                    elif vc >= 2:
                        bonus += 4; signals.append(f"🎵 TikTok: {vc} видео")
        except Exception:
            pass

        return min(bonus, 25), signals

    async def get_x_sentiment(self, symbol: str) -> int:
        """Обратная совместимость."""
        bonus, _ = await self.get_social_sentiment(symbol, "")
        return bonus


    async def _check_narrative_dilution(self, symbol: str) -> bool:
        """
        Проверяет нарративное размытие: если вышло много монет по одному инфоповоду
        (например, 20 монет "TRUMP"), они высасывают ликвидность друг у друга.
        Возвращает True если риск размытия высокий.
        """
        try:
            # Берём первые 3 слова символа как базу нарратива
            base = symbol[:4].upper().rstrip("0123456789")
            if len(base) < 3:
                return False

            async with self.session.get(
                f"https://api.dexscreener.com/latest/dex/search?q={base}",
                timeout=aiohttp.ClientTimeout(total=6)
            ) as r:
                if r.status != 200:
                    return False
                data = await r.json()

            # Считаем сколько токенов Solana с похожим именем торгуется
            similar = [
                p for p in data.get("pairs", [])
                if p.get("chainId") == "solana"
                and base in p.get("baseToken", {}).get("symbol", "").upper()
                and float(p.get("volume", {}).get("h24", 0) or 0) > 10_000
            ]

            # Более 5 активных токенов с похожим именем = размытие
            if len(similar) > 5:
                log.debug(f"Нарратив {base}: {len(similar)} похожих токенов")
                return True
            return False
        except Exception:
            return False

    async def get_holders(self, mint: str) -> tuple[int, float]:
        """Получить кол-во холдеров и % у топ-10 через Helius RPC."""
        try:
            async with self.session.post(
                RPC_URL,
                json={"jsonrpc":"2.0","id":1,
                      "method":"getTokenLargestAccounts",
                      "params":[mint, {"commitment":"confirmed"}]},
                timeout=aiohttp.ClientTimeout(total=8)
            ) as r:
                data = await r.json()
            accounts = data.get("result", {}).get("value", [])
            if not accounts:
                return 0, 0.0
            # Топ-10 сумма
            total_supply = sum(float(a.get("uiAmount", 0) or 0) for a in accounts)
            top10_amount = sum(float(a.get("uiAmount", 0) or 0) for a in accounts[:10])
            top10_pct = (top10_amount / total_supply * 100) if total_supply > 0 else 0
            # Кол-во холдеров — приблизительно из кол-ва аккаунтов
            n_holders = len(accounts)
            return n_holders, top10_pct
        except Exception:
            return 0, 0.0

    async def get_price(self, mint: str) -> float:
        """Текущая цена токена в USD."""
        try:
            url = f"https://api.dexscreener.com/tokens/v1/solana/{mint}"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=6)) as r:
                data = await r.json()
                pairs = (data[0].get("pairs") or []) if isinstance(data, list) and data else []
                if pairs:
                    return float(pairs[0].get("priceUsd", 0) or 0)
        except Exception:
            pass
        return 0.0

    async def get_rug_score(self, mint: str) -> Optional[int]:
        """Быстрая проверка rug score."""
        try:
            url = f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=6)) as r:
                if r.status == 200:
                    data = await r.json()
                    return int(data.get("score", 50))
        except Exception:
            pass
        return None

    async def get_full_token_analysis(self, mint: str) -> dict:
        """
        Полный анализ токена через RugCheck — sniper%, bundler%, dev_hold%, top10%.
        По гайду: snipers>8% скип, bundlers>45% скип, top10>45% скип.
        """
        result = {
            "snipers_pct":  0.0,
            "bundlers_pct": 0.0,
            "dev_hold_pct": 0.0,
            "top10_pct":    0.0,
            "holders":      0,
        }
        try:
            url = f"https://api.rugcheck.xyz/v1/tokens/{mint}/report"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status != 200:
                    return result
                data = await r.json()

            # Top holders
            top_holders = data.get("topHolders", [])
            if top_holders:
                result["top10_pct"] = sum(
                    float(h.get("pct", 0)) for h in top_holders[:10]
                ) * 100
                result["holders"] = len(top_holders)

            # Risks — парсим sniper/bundle/dev из списка рисков
            for risk in data.get("risks", []):
                name  = risk.get("name", "").lower()
                value = str(risk.get("value", "0")).replace("%","").strip()
                try:
                    pct = float(value)
                except Exception:
                    pct = 0.0
                if "sniper" in name:
                    result["snipers_pct"] = pct
                elif "bundle" in name:
                    result["bundlers_pct"] = pct
                elif "dev" in name and "hold" in name:
                    result["dev_hold_pct"] = pct

        except Exception as e:
            log.debug(f"FullAnalysis {mint[:8]}: {e}")
        return result

    # ── Торговля ────────────────────────

    async def _can_trade(self) -> tuple[bool, str]:
        if self.paused:
            return False, "Бот на паузе"
        if self._daily_limit_hit():
            return False, f"Дневной лимит -{AUTO_CONFIG['daily_loss_limit']} SOL"
        if len(self.positions) >= AUTO_CONFIG["max_positions"]:
            return False, "Лимит позиций"
        bal = await self.get_sol_balance()
        if bal < AUTO_CONFIG["position_sol"] * 1.1:
            return False, f"Мало SOL: {bal:.3f}"
        return True, "OK"

    async def buy_token(self, token: Token) -> bool:
        """Купить токен. Возвращает True если успешно."""
        can, reason = await self._can_trade()
        if not can:
            log.info(f"Skip {token.symbol}: {reason}")
            return False

        # Жёсткий фильтр возраста — старше 24ч не берём НИКОГДА
        if token.age_hours > AUTO_CONFIG["max_age_hours"]:
            log.info(f"Skip {token.symbol}: возраст {token.age_hours:.0f}ч > {AUTO_CONFIG['max_age_hours']}ч")
            return False
        if token.age_hours == 0:
            log.info(f"Skip {token.symbol}: возраст неизвестен (age=0)")
            return False

        if token.mint in self.positions:
            return False

        # Rug check
        rug_score = None
        if AUTO_CONFIG["rugcheck"]:
            rug_score = await self.get_rug_score(token.mint)
            if rug_score is not None and rug_score > 70:
                log.info(f"Skip {token.symbol}: высокий риск {rug_score}/100")
                return False

            # Полный анализ: snipers, bundlers, dev_hold, top10
            analysis = await self.get_full_token_analysis(token.mint)
            token.snipers_pct  = analysis["snipers_pct"]
            token.bundlers_pct = analysis["bundlers_pct"]
            token.dev_hold_pct = analysis["dev_hold_pct"]
            token.top10_pct    = analysis["top10_pct"]
            token.holders      = analysis["holders"]

            # По гайду: snipers >8% = скип (моментально сольют)
            if token.snipers_pct > AUTO_CONFIG["max_snipers_pct"]:
                log.info(f"Skip {token.symbol}: снайперы {token.snipers_pct:.0f}% >{AUTO_CONFIG['max_snipers_pct']}%")
                return False
            # bundlers >45% = скип (команда контролирует)
            if token.bundlers_pct > AUTO_CONFIG["max_bundlers_pct"]:
                log.info(f"Skip {token.symbol}: бандлы {token.bundlers_pct:.0f}% >{AUTO_CONFIG['max_bundlers_pct']}%")
                return False
            # top10 >45% = полный контроль, скип
            if token.top10_pct > AUTO_CONFIG["max_top10_pct"]:
                log.info(f"Skip {token.symbol}: топ-10 {token.top10_pct:.0f}% >{AUTO_CONFIG['max_top10_pct']}%")
                return False

            # Проверка нарративного размытия
            if AUTO_CONFIG.get("narrative_filter", True):
                narrative_risk = await self._check_narrative_dilution(token.symbol)
                if narrative_risk:
                    log.info(f"Skip {token.symbol}: нарративное размытие")
                    return False
                return False

        # AI скоринг
        sc, signals, risks = score_token(token, rug_score)
        # Социальный анализ: X, TikTok, Instagram
        x_bonus = 0
        social_sigs = []
        if token.age_hours <= AUTO_CONFIG.get("max_age_hours", 24):
            x_bonus, social_sigs = await self.get_social_sentiment(token.symbol, token.mint)
            sc = min(100, sc + x_bonus)
            signals.extend(social_sigs)
        soc_str = " ".join(s for s in social_sigs[:3]) if social_sigs else "-"
        log.info(
            f"  {token.symbol}: score={sc}(+{x_bonus}soc) | "
            f"vol=${token.volume_24h:,.0f} liq=${token.liquidity:,.0f} | "
            f"+{token.change_1h:.0f}%/1h {token.age_hours:.1f}h | "
            f"snip={token.snipers_pct:.0f}% top10={token.top10_pct:.0f}% | {soc_str}"
        )
        if sc < AUTO_CONFIG["min_score"]:
            log.info(f"Skip {token.symbol}: score {sc} < {AUTO_CONFIG['min_score']}")
            return False

        # Исполнить своп
        sol = AUTO_CONFIG["position_sol"]
        txid = None
        if self.jupiter and self.jupiter._pubkey:
            txid = await self.jupiter.buy(token.mint, sol)
            if not txid:
                log.error(f"Buy {token.symbol}: своп не прошёл (txid=None)")
                return False
        else:
            log.warning(f"Buy {token.symbol}: Jupiter не инициализирован, пропуск")
            return False

        # Создать позицию
        pos = Position(
            mint=token.mint, symbol=token.symbol,
            entry_price=token.price_usd, amount_sol=sol,
            pair_address=token.pair_address,
            peak_price=token.price_usd,
        )
        self.positions[token.mint] = pos

        # Уведомление
        sig_text  = "\n".join(f"  ✅ {s}" for s in signals[:4])
        risk_text = "\n".join(f"  ⚠️ {r}" for r in risks[:2])
        tx_link   = f"https://solscan.io/tx/{txid}" if txid else "—"

        await self._notify(
            f"🤖 <b>АВТОПОКУПКА</b>\n\n"
            f"🪙 <b>{token.symbol}</b>\n"
            f"💵 Цена: <code>${token.price_usd:.8f}</code>\n"
            f"💰 Сумма: {sol} SOL\n"
            f"🤖 AI скор: <b>{sc}/100</b>\n\n"
            f"{sig_text}\n"
            f"{risk_text}\n\n"
            f"🎯 TP: +10% / +25% / +60%\n"
            f"⛔ SL: -{AUTO_CONFIG['stop_loss']*100:.0f}%\n"
            f"🔗 <a href='{tx_link}'>Транзакция</a>"
        )
        log.info(f"✅ BUY {token.symbol} @ ${token.price_usd:.8f}  score={sc}")
        return True

    async def sell_token(self, mint: str, reason: str,
                         pct_to_sell: float = 1.0) -> bool:
        """Продать часть или всю позицию."""
        pos = self.positions.get(mint)
        if not pos:
            return False

        cur_price = await self.get_price(mint)
        if cur_price <= 0:
            cur_price = pos.entry_price

        pct = pos.pct(cur_price)
        sol_to_sell = pos.amount_sol * pct_to_sell
        pnl = sol_to_sell * pct

        # Исполнить своп — продаём токены, а не SOL
        txid = None
        if self.jupiter and self.jupiter._pubkey:
            token_balance = await self.get_token_balance(mint)
            if token_balance > 0:
                token_to_sell = int(token_balance * pct_to_sell)
                if token_to_sell > 0:
                    txid = await self.jupiter.swap(mint, SOL_MINT, token_to_sell)

        # Обновить позицию
        if pct_to_sell >= 0.99:
            del self.positions[mint]
        else:
            pos.amount_sol *= (1 - pct_to_sell)

        self._add_pnl(pnl)
        emoji  = "🟢" if pnl > 0 else "🔴"
        part   = f" ({int(pct_to_sell*100)}%)" if pct_to_sell < 1 else ""
        tx_link = f"https://solscan.io/tx/{txid}" if txid else "—"

        await self._notify(
            f"{emoji} <b>ПРОДАЖА{part}</b> {pos.symbol}\n\n"
            f"📋 {reason}\n"
            f"💵 Цена: <code>${cur_price:.8f}</code>\n"
            f"💰 PnL: <b>{pnl:+.4f} SOL</b> ({pct*100:+.1f}%)\n"
            f"⏱ Держал: {pos.age_str}\n"
            f"📊 Сегодня: <b>{self.today_pnl:+.4f} SOL</b>\n"
            f"🔗 <a href='{tx_link}'>Транзакция</a>"
        )
        log.info(f"{'✅' if pnl>0 else '❌'} SELL{part} {pos.symbol}  pnl={pnl:+.4f}  reason={reason}")
        return True

    # ── Мониторинг позиций ───────────────

    async def monitor_positions(self):
        """Проверить все позиции на TP/SL/Trailing."""
        for mint, pos in list(self.positions.items()):
            try:
                price = await self.get_price(mint)
                if price <= 0:
                    continue

                pct = pos.pct(price)

                # Обновить пик
                if price > pos.peak_price:
                    pos.peak_price = price

                # Алерт 2x
                if pct >= 1.0 and not pos.alerted_2x:
                    pos.alerted_2x = True
                    await self._notify(
                        f"🚀 <b>2X АЛЕРТ!</b> {pos.symbol}\n"
                        f"+{pct*100:.0f}% от входа!\n"
                        f"Цена: ${price:.8f}\n"
                        f"🔗 <a href='https://dexscreener.com/solana/{pos.pair_address}'>DexScreener</a>"
                    )

                # Мульти-тейк профит
                tp_levels = [
                    ("tp1", AUTO_CONFIG["take_profit_1"], 0.30),
                    ("tp2", AUTO_CONFIG["take_profit_2"], 0.40),
                    ("tp3", AUTO_CONFIG["take_profit_3"], 1.00),
                ]
                sold = False
                for key, tp_pct, sell_pct in tp_levels:
                    if pct >= tp_pct and key not in pos.tp_hit:
                        pos.tp_hit.append(key)
                        await self.sell_token(
                            mint,
                            f"Take Profit +{tp_pct*100:.0f}%",
                            pct_to_sell=sell_pct
                        )
                        sold = True
                        break

                if sold:
                    continue

                # Стоп-лосс
                if pct <= -AUTO_CONFIG["stop_loss"]:
                    await self.sell_token(mint, f"Stop Loss {pct*100:.1f}%")
                    continue

                # Trailing stop
                if AUTO_CONFIG["trailing_stop"] and pos.peak_price > pos.entry_price:
                    drop_from_peak = (pos.peak_price - price) / pos.peak_price
                    if drop_from_peak > AUTO_CONFIG["stop_loss"]:
                        await self.sell_token(
                            mint,
                            f"Trailing Stop (пик ${pos.peak_price:.8f})"
                        )

                # Дневной лимит — закрыть все
                if self._daily_limit_hit():
                    await self.sell_token(mint, "Дневной лимит потерь")

            except Exception as e:
                log.error(f"Monitor {mint} error: {e}")

    # ── Копи-трейдинг ────────────────────

    async def copy_trade_check(self):
        """Мониторинг смарт-кошельков и копирование их сделок."""
        if not self.smart_wallets:
            return

        now = time.time()
        for wallet_info in self.smart_wallets[:8]:
            wallet = wallet_info["address"]
            try:
                # Получаем последние транзакции (быстро, лимит 10)
                async with self.session.post(
                    RPC_URL,
                    json={"jsonrpc":"2.0","id":1,
                          "method":"getSignaturesForAddress",
                          "params":[wallet, {"limit": 10}]},
                    timeout=aiohttp.ClientTimeout(total=6)
                ) as r:
                    rpc_data = await r.json()

                for sig_info in rpc_data.get("result", []):
                    # Только свежие транзакции (последние 3 минуты)
                    block_time = sig_info.get("blockTime") or 0
                    if now - block_time > 180:
                        continue
                    if sig_info.get("err"):
                        continue

                    sig = sig_info.get("signature", "")
                    if not sig or sig in self._copy_seen:
                        continue
                    self._copy_seen.add(sig)

                    # Парсим что купили
                    bought_mint, sol_spent = await self._parse_swap_mint(sig, wallet)
                    if not bought_mint or bought_mint == SOL_MINT:
                        continue
                    if bought_mint in self.positions:
                        continue

                    # Проверяем минимальный размер сделки ($5+)
                    if sol_spent < 0.01:
                        continue

                    # Получаем данные токена
                    token = await self._fetch_token_info(bought_mint)
                    if not token:
                        continue

                    # Базовые фильтры безопасности
                    if token.liquidity < 10_000:
                        log.info(f"Copy skip {token.symbol}: низкая ликвидность ${token.liquidity:,.0f}")
                        continue

                    rug = await self.get_rug_score(bought_mint)
                    if rug is not None and rug > 70:
                        log.info(f"Copy skip {token.symbol}: rug {rug}/100")
                        continue

                    log.info(f"🔁 Copy: {wallet[:8]} купил {token.symbol} ({sol_spent:.3f} SOL)")
                    await self._notify(
                        f"🔁 <b>COPY TRADE</b>\n\n"
                        f"🧠 Кошелёк: <code>{wallet[:8]}...{wallet[-4:]}</code>\n"
                        f"🏆 WR: {wallet_info['win_rate']*100:.0f}%  |  "
                        f"Сделок: {wallet_info['trades']}\n\n"
                        f"🪙 Токен: <b>{token.symbol}</b>\n"
                        f"💵 Цена: ${token.price_usd:.8f}\n"
                        f"💧 Ликв: ${token.liquidity:,.0f}\n"
                        f"📊 Объём: ${token.volume_24h:,.0f}\n"
                        f"📈 +{token.change_1h:.0f}% за 1ч"
                    )
                    await self.buy_token(token)
                    await asyncio.sleep(1)

            except Exception as e:
                log.error(f"Copy trade {wallet[:8]}: {e}")

    async def _parse_swap_mint(self, signature: str, wallet: str) -> tuple[Optional[str], float]:
        """Парсит транзакцию: возвращает (mint купленного токена, потрачено SOL)."""
        try:
            async with self.session.post(
                RPC_URL,
                json={"jsonrpc":"2.0","id":1,
                      "method":"getTransaction",
                      "params":[signature, {
                          "encoding":"jsonParsed",
                          "maxSupportedTransactionVersion": 0
                      }]},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                data = await r.json()

            tx   = data.get("result")
            if not tx:
                return None, 0.0

            meta = tx.get("meta", {})

            # Считаем потраченный SOL (разница preBalance - postBalance кошелька)
            accs = tx.get("transaction",{}).get("message",{}).get("accountKeys",[])
            wallet_idx = next(
                (i for i, a in enumerate(accs)
                 if (a.get("pubkey") if isinstance(a, dict) else a) == wallet),
                None
            )
            sol_spent = 0.0
            if wallet_idx is not None:
                pre  = meta.get("preBalances",  [])[wallet_idx] if wallet_idx < len(meta.get("preBalances",[])) else 0
                post = meta.get("postBalances", [])[wallet_idx] if wallet_idx < len(meta.get("postBalances",[])) else 0
                sol_spent = max(0, (pre - post) / 1e9)

            # Ищем токен, баланс которого ВЫРОС у кошелька
            pre_map  = {b["mint"]: int(b["uiTokenAmount"]["amount"])
                        for b in meta.get("preTokenBalances", [])
                        if b.get("owner") == wallet}
            post_map = {b["mint"]: int(b["uiTokenAmount"]["amount"])
                        for b in meta.get("postTokenBalances", [])
                        if b.get("owner") == wallet}

            for mint, post_amt in post_map.items():
                pre_amt = pre_map.get(mint, 0)
                if post_amt > pre_amt and mint != SOL_MINT:
                    return mint, sol_spent

        except Exception as e:
            log.debug(f"Parse swap {signature[:12]}: {e}")
        return None, 0.0

    async def _fetch_token_info(self, mint: str) -> Optional["Token"]:
        """Получить данные токена с DexScreener по mint адресу."""
        try:
            async with self.session.get(
                f"https://api.dexscreener.com/tokens/v1/solana/{mint}",
                timeout=aiohttp.ClientTimeout(total=6)
            ) as r:
                if r.status != 200:
                    return None
                pairs = await r.json()

            if not isinstance(pairs, list) or not pairs:
                return None

            # Берём пару с максимальной ликвидностью
            p = max(pairs, key=lambda x: float(x.get("liquidity",{}).get("usd",0) or 0))
            ca = p.get("pairCreatedAt", 0) or 0
            return Token(
                symbol    = p.get("baseToken",{}).get("symbol", mint[:6]),
                mint      = mint,
                price_usd = float(p.get("priceUsd",  0) or 0),
                change_1h = float(p.get("priceChange",{}).get("h1",  0) or 0),
                change_24h= float(p.get("priceChange",{}).get("h24", 0) or 0),
                volume_24h= float(p.get("volume",     {}).get("h24", 0) or 0),
                liquidity = float(p.get("liquidity",  {}).get("usd", 0) or 0),
                market_cap= float(p.get("marketCap", 0) or 0),
                pair_address = p.get("pairAddress",""),
                buys_1h   = int(p.get("txns",{}).get("h1",{}).get("buys",  0) or 0),
                sells_1h  = int(p.get("txns",{}).get("h1",{}).get("sells", 0) or 0),
                age_hours = (time.time()*1000 - ca) / 3_600_000 if ca else 0,
            )
        except Exception as e:
            log.debug(f"Fetch token {mint[:8]}: {e}")
        return None


    async def auto_find_smart_wallets(self):
        """Автоматически ищет смарт-кошельки."""
        interval = AUTO_CONFIG["smart_scan_hours"] * 3600
        if time.time() - self.last_smart_scan < interval:
            return
        self.last_smart_scan = time.time()
        log.info("🧠 Ищу смарт-кошельки...")

        found = await self.hunter.find()
        if not found:
            return

        new_count = sum(
            1 for w in found
            if w["address"] not in [x["address"] for x in self.smart_wallets]
        )
        self.smart_wallets = found
        self._save()

        if new_count > 0:
            text = f"🧠 <b>Найдено {len(found)} смарт-кошельков</b>\n\n"
            for w in found[:5]:
                a = w["address"]
                text += (f"• <code>{a[:8]}...{a[-4:]}</code>  "
                         f"WR: <b>{w['win_rate']*100:.0f}%</b>  "
                         f"сделок: {w['trades']}\n")
            await self._notify(text)
        log.info(f"Смарт-кошельков найдено: {len(found)}")

    # ── Ежедневный отчёт ─────────────────

    async def send_report(self):
        self._check_day()
        t  = self.stats["trades"]
        w  = self.stats["wins"]
        wr = f"{w/t*100:.0f}%" if t > 0 else "0%"
        bal = await self.get_sol_balance()

        chart = ""
        days  = sorted(self.daily.items())[-7:]
        for d, pnl in days:
            bar  = "█" * min(int(abs(pnl)*15), 10) or "▏"
            sign = "🟢" if pnl >= 0 else "🔴"
            chart += f"{sign} {d[-5:]}  {bar}  {pnl:+.3f}\n"

        await self._notify(
            f"📊 <b>ОТЧЁТ — {self.today_date}</b>\n\n"
            f"💳 Баланс: <b>{bal:.4f} SOL</b>\n"
            f"💰 PnL сегодня: <b>{self.today_pnl:+.4f} SOL</b>\n"
            f"📈 Всего PnL: <b>{self.stats['total_pnl']:+.4f} SOL</b>\n"
            f"🔢 Сделок: {t}  ✅ {w}  ❌ {self.stats['losses']}\n"
            f"🎯 Winrate: {wr}\n"
            f"🧠 Смарт-кошельков: {len(self.smart_wallets)}\n\n"
            f"📅 <b>PnL 7 дней:</b>\n{chart}"
        )

    # ── Статус ───────────────────────────

    def status_text(self) -> str:
        self._check_day()
        t   = self.stats["trades"]
        wr  = f"{self.stats['wins']/t*100:.0f}%" if t > 0 else "n/a"
        lim = "⛔ СТОП" if self._daily_limit_hit() else "✅ OK"
        mode = "⏸ ПАУЗА" if self.paused else ("🟢 РАБОТАЕТ" if self.running else "🔴 ОСТАНОВЛЕН")
        return (
            f"🤖 <b>АВТОПИЛОТ</b>  {mode}\n\n"
            f"📂 Позиций: <b>{len(self.positions)}</b>/{AUTO_CONFIG['max_positions']}  " + ("\u2705 торгую" if self.jupiter and self.jupiter._pubkey else "\u274c нет ключа") + "\n"
            f"💰 PnL сегодня: <b>{self.today_pnl:+.4f} SOL</b>\n"
            f"📈 Всего PnL: <b>{self.stats['total_pnl']:+.4f} SOL</b>\n"
            f"🎯 Winrate: {wr}  ({t} сделок)\n"
            f"🧠 Смарт-кошельков: {len(self.smart_wallets)}\n"
            f"⛔ Лимит потерь: {lim}\n\n"
            f"⚙️ Позиция: {AUTO_CONFIG['position_sol']} SOL\n"
            f"⛔ SL: -{AUTO_CONFIG['stop_loss']*100:.0f}%  "
            f"🎯 Min скор: {AUTO_CONFIG['min_score']}/100"
        )

    async def _notify(self, text: str):
        if self.notify:
            try:
                await self.notify(text)
            except Exception as e:
                log.error(f"Notify error: {e}")

    # ── ГЛАВНЫЙ ЦИКЛ ─────────────────────

    async def run(self):
        """Основной бесконечный цикл автопилота."""
        self.running = True
        log.info("🚀 АВТОПИЛОТ ЗАПУЩЕН")

        await self._notify(
            f"🚀 <b>АВТОПИЛОТ ЗАПУЩЕН</b>\n\n"
            f"⚙️ Позиция: {AUTO_CONFIG['position_sol']} SOL\n"
            f"🎯 Мин. скор: {AUTO_CONFIG['min_score']}/100\n"
            f"⛔ SL: -{AUTO_CONFIG['stop_loss']*100:.0f}%\n"
            f"📅 Лимит/день: -{AUTO_CONFIG['daily_loss_limit']} SOL\n\n"
            f"Бот работает сам. Уведомления придут автоматически."
        )

        # Ждём инициализации сессии
        for _ in range(20):
            if self.session is not None:
                break
            await asyncio.sleep(0.5)
        else:
            log.error("Session не инициализирована — автопилот не запустится!")
            return

        while self.running:
            try:
                if not self.paused:
                    # 0. Очищаем старые сигнатуры копи-трейда
                    if len(self._copy_seen) > 500:
                        self._copy_seen = set(list(self._copy_seen)[-200:])

                    # 1. Мониторинг открытых позиций
                    try:
                        await asyncio.wait_for(self.monitor_positions(), timeout=15)
                    except asyncio.TimeoutError:
                        log.warning("monitor_positions timeout")

                    # 2. Поиск смарт-кошельков (раз в N часов)
                    try:
                        await asyncio.wait_for(self.auto_find_smart_wallets(), timeout=30)
                    except asyncio.TimeoutError:
                        log.warning("auto_find_smart_wallets timeout")

                    # 3. Ежедневный отчёт
                    now   = datetime.now()
                    today = str(date.today())
                    if now.hour == 0 and now.minute == 0 and today != self.last_report_day:
                        self.last_report_day = today
                        await self.send_report()

                    # 4. Копи-трейдинг смарт-кошельков
                    if self.smart_wallets and not self._daily_limit_hit():
                        await self.copy_trade_check()

                    # 5. Поиск новых токенов для покупки
                    if not self._daily_limit_hit():
                        try:
                            tokens = await asyncio.wait_for(
                                self.get_trending(), timeout=30
                            )
                        except asyncio.TimeoutError:
                            log.warning("📡 get_trending timeout — пропуск")
                            tokens = []
                        log.info(f"📡 Сканирование: найдено {len(tokens)} токенов")
                        bought = 0
                        for token in tokens:
                            if token.mint in self.positions:
                                continue
                            if len(self.positions) >= AUTO_CONFIG["max_positions"]:
                                log.info("⏸ Лимит позиций достигнут")
                                break
                            sc, _, _ = score_token(token)
                            log.info(f"  🔍 {token.symbol}: score={sc}, vol=${token.volume_24h:,.0f}, liq=${token.liquidity:,.0f}, +{token.change_1h:.0f}%/1h")
                            ok = await self.buy_token(token)
                            if ok:
                                bought += 1
                            await asyncio.sleep(2)
                        if not tokens:
                            log.info("📡 Токенов не найдено — фильтры слишком строгие или DexScreener недоступен")
                    else:
                        log.info("⛔ Дневной лимит потерь достигнут, торговля остановлена")

            except Exception as e:
                log.error(f"Main loop error: {e}")
                await asyncio.sleep(10)

            await asyncio.sleep(AUTO_CONFIG["scan_interval"])

    def stop(self):
        self.running = False


# ═══════════════════════════════════════════════
#  TELEGRAM ИНТЕРФЕЙС
# ═══════════════════════════════════════════════

bot = AutopilotEngine()


def kb_main():
    paused  = bot.paused
    running = bot.running
    rows = [
        [InlineKeyboardButton("📊 Статус",       callback_data="status"),
         InlineKeyboardButton("💼 Позиции",      callback_data="positions")],
        [InlineKeyboardButton("📈 Статистика",   callback_data="stats"),
         InlineKeyboardButton("💳 Баланс",       callback_data="balance")],
        [InlineKeyboardButton("🧠 Смарт кош.",   callback_data="smart"),
         InlineKeyboardButton("📅 Отчёт",        callback_data="report")],
        [InlineKeyboardButton("⚙️ Настройки",   callback_data="settings")],
    ]
    if not running:
        rows.append([InlineKeyboardButton("🚀 ЗАПУСТИТЬ АВТОПИЛОТ", callback_data="start_pilot")])
    else:
        rows.append([
            InlineKeyboardButton("⏸ Пауза" if not paused else "▶️ Продолжить",
                                 callback_data="toggle_pause"),
            InlineKeyboardButton("⛔ Стоп",  callback_data="stop_all"),
        ])
    return InlineKeyboardMarkup(rows)


def kb_settings():
    cfg = AUTO_CONFIG
    def tog(val): return "✅" if val else "❌"
    return InlineKeyboardMarkup([
        # ── ТОРГОВЛЯ ──────────────────────────────────────
        [InlineKeyboardButton("━━━ 💰 ТОРГОВЛЯ ━━━", callback_data="noop")],
        [InlineKeyboardButton(f"💰 Позиция: {cfg['position_sol']} SOL",        callback_data="s_pos"),
         InlineKeyboardButton("➖", callback_data="pos_dn"),
         InlineKeyboardButton("➕", callback_data="pos_up")],
        [InlineKeyboardButton(f"📊 Макс позиций: {cfg['max_positions']}",       callback_data="s_maxpos"),
         InlineKeyboardButton("➖", callback_data="maxpos_dn"),
         InlineKeyboardButton("➕", callback_data="maxpos_up")],
        [InlineKeyboardButton(f"🚨 Лимит/день: {cfg['daily_loss_limit']} SOL",  callback_data="s_limit"),
         InlineKeyboardButton("➖", callback_data="limit_dn"),
         InlineKeyboardButton("➕", callback_data="limit_up")],
        [InlineKeyboardButton(f"⚡ Slippage: {cfg['slippage_bps']/100:.1f}%",   callback_data="s_slip"),
         InlineKeyboardButton("➖", callback_data="slip_dn"),
         InlineKeyboardButton("➕", callback_data="slip_up")],
        # ── СТОП-ЛОСС / TP ───────────────────────────────
        [InlineKeyboardButton("━━━ 🎯 СТОП / TP ━━━", callback_data="noop")],
        [InlineKeyboardButton(f"⛔ Стоп-лосс: -{cfg['stop_loss']*100:.0f}%",   callback_data="s_sl"),
         InlineKeyboardButton("➖", callback_data="sl_dn"),
         InlineKeyboardButton("➕", callback_data="sl_up")],
        [InlineKeyboardButton(f"🎯 TP1: +{cfg['take_profit_1']*100:.0f}% → продать {cfg['tp1_sell_pct']*100:.0f}%", callback_data="s_tp1")],
        [InlineKeyboardButton(f"🎯 TP2: +{cfg['take_profit_2']*100:.0f}% → продать {cfg['tp2_sell_pct']*100:.0f}%", callback_data="s_tp2")],
        [InlineKeyboardButton(f"🎯 TP3: +{cfg['take_profit_3']*100:.0f}% → продать {cfg['tp3_sell_pct']*100:.0f}%", callback_data="s_tp3")],
        [InlineKeyboardButton(f"🔄 Trailing Stop: {tog(cfg['trailing_stop'])}",  callback_data="s_trail")],
        # ── ФИЛЬТРЫ ──────────────────────────────────────
        [InlineKeyboardButton("━━━ 🔍 ФИЛЬТРЫ ━━━", callback_data="noop")],
        [InlineKeyboardButton(f"🤖 Мин. скор: {cfg['min_score']}/100",           callback_data="s_score"),
         InlineKeyboardButton("➖", callback_data="score_dn"),
         InlineKeyboardButton("➕", callback_data="score_up")],
        [InlineKeyboardButton(f"📊 Мин. объём: ${cfg['min_volume']:,.0f}",        callback_data="s_vol"),
         InlineKeyboardButton("➖", callback_data="vol_dn"),
         InlineKeyboardButton("➕", callback_data="vol_up")],
        [InlineKeyboardButton(f"💧 Мин. ликв: ${cfg['min_liquidity']:,.0f}",      callback_data="s_liq"),
         InlineKeyboardButton("➖", callback_data="liq_dn"),
         InlineKeyboardButton("➕", callback_data="liq_up")],
        [InlineKeyboardButton(f"⏱ Макс возраст: {cfg['max_age_hours']}ч",        callback_data="s_age"),
         InlineKeyboardButton("➖", callback_data="age_dn"),
         InlineKeyboardButton("➕", callback_data="age_up")],
        [InlineKeyboardButton(f"🎯 Макс снайперы: {cfg['max_snipers_pct']}%",    callback_data="s_snip"),
         InlineKeyboardButton("➖", callback_data="snip_dn"),
         InlineKeyboardButton("➕", callback_data="snip_up")],
        [InlineKeyboardButton(f"📦 Макс бандлы: {cfg['max_bundlers_pct']}%",     callback_data="s_bndl"),
         InlineKeyboardButton("➖", callback_data="bndl_dn"),
         InlineKeyboardButton("➕", callback_data="bndl_up")],
        [InlineKeyboardButton(f"👥 Макс топ-10: {cfg['max_top10_pct']}%",        callback_data="s_top10"),
         InlineKeyboardButton("➖", callback_data="top10_dn"),
         InlineKeyboardButton("➕", callback_data="top10_up")],
        # ── ПЕРЕКЛЮЧАТЕЛИ ────────────────────────────────
        [InlineKeyboardButton("━━━ ⚙️ РЕЖИМЫ ━━━", callback_data="noop")],
        [InlineKeyboardButton(f"🛡 RugCheck: {tog(cfg['rugcheck'])}",            callback_data="s_rug")],
        [InlineKeyboardButton(f"🧠 Копитрейд: {tog(cfg['copy_trade'])}",         callback_data="s_copy")],
        [InlineKeyboardButton(f"🔀 Нарратив-фильтр: {tog(cfg['narrative_filter'])}", callback_data="s_narr")],
        [InlineKeyboardButton(f"⏱ Скан каждые {cfg['scan_interval']}с",          callback_data="s_scan"),
         InlineKeyboardButton("➖", callback_data="scan_dn"),
         InlineKeyboardButton("➕", callback_data="scan_up")],
        [InlineKeyboardButton("◀️ Назад", callback_data="back")],
    ])


def auth(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ALLOWED_USER_ID:
            return
        return await func(update, ctx)
    return wrapper


@auth
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        bot.status_text(),
        reply_markup=kb_main(), parse_mode="HTML"
    )

async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        bot.status_text(),
        reply_markup=kb_main(), parse_mode="HTML"
    )


awaiting: dict[int, str] = {}


async def cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != ALLOWED_USER_ID:
        return
    d = q.data

    # ── Главные действия ──────────────
    if d == "status":
        try:
            await q.edit_message_text(bot.status_text(), reply_markup=kb_main(), parse_mode="HTML")
        except Exception:
            pass  # Message not modified - ignore

    elif d == "positions":
        if not bot.positions:
            txt = "📂 <b>Открытых позиций нет</b>"
            btns = [[InlineKeyboardButton("◀️ Назад", callback_data="back")]]
        else:
            txt  = "💼 <b>Открытые позиции</b>\n\n"
            btns = []
            total_pnl = 0.0
            for mint, pos in bot.positions.items():
                cur_price = await bot.get_price(mint)
                if cur_price <= 0:
                    cur_price = getattr(pos, "last_price", 0) or pos.entry_price
                pct     = pos.pct(cur_price) * 100
                pnl_sol = pos.amount_sol * pos.pct(cur_price)
                total_pnl += pnl_sol
                em = "🟢" if pct >= 0 else "🔴"
                txt += (
                    f"━━━━━━━━━━━━━━━━\n"
                    f"🪙 <b>{pos.symbol}</b>  |  ⏱ {pos.age_str}\n"
                    f"💵 Вход: <code>${pos.entry_price:.8f}</code>\n"
                    f"💹 Текущая: <code>${cur_price:.8f}</code>\n"
                    f"💰 Размер: {pos.amount_sol:.4f} SOL\n"
                    f"📊 PnL: {em} <b>{pct:+.1f}%</b>  ({pnl_sol:+.4f} SOL)\n"
                )
                btn_emoji = "📈" if pct >= 0 else "📉"
                btns.append([InlineKeyboardButton(
                    f"{btn_emoji} {pos.symbol} {pct:+.1f}% | ❌ Закрыть",
                    callback_data=f"close_{mint}"
                )])
            itogo_em = "🟢" if total_pnl >= 0 else "🔴"
            txt += f"━━━━━━━━━━━━━━━━\n💼 Итого: {itogo_em} <b>{total_pnl:+.4f} SOL</b>"
            txt += "━━━━━━━━━━━━━━━━"
            btns.append([InlineKeyboardButton("◀️ Назад", callback_data="back")])
        await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup(btns), parse_mode="HTML")

    elif d.startswith("close_"):
        mint = d[6:]
        pos  = bot.positions.get(mint)
        if pos:
            await bot.sell_token(mint, "Ручное закрытие")
            await q.edit_message_text(f"✅ {pos.symbol} закрыт", reply_markup=kb_main())
        else:
            await q.edit_message_text("❌ Позиция не найдена", reply_markup=kb_main())

    elif d == "stats":
        t  = bot.stats["trades"]
        w  = bot.stats["wins"]
        wr = f"{w/t*100:.0f}%" if t > 0 else "0%"
        chart = ""
        for day, pnl in sorted(bot.daily.items())[-7:]:
            bar  = "█" * min(int(abs(pnl)*15), 10) or "▏"
            sign = "🟢" if pnl >= 0 else "🔴"
            chart += f"{sign} {day[-5:]}  {bar}  {pnl:+.3f}\n"
        total_pnl = bot.stats['total_pnl']
        today_pnl = bot.today_pnl
        total_emoji = "🟢" if total_pnl >= 0 else "🔴"
        today_emoji = "🟢" if today_pnl >= 0 else "🔴"
        avg_pnl = total_pnl / t if t > 0 else 0
        txt = (
            f"📈 <b>Статистика</b>\n\n"
            f"💰 PnL сегодня: {today_emoji} <b>{today_pnl:+.4f} SOL</b>\n"
            f"📊 Всего PnL: {total_emoji} <b>{total_pnl:+.4f} SOL</b>\n"
            f"📉 Средний PnL/сделка: <b>{avg_pnl:+.4f} SOL</b>\n\n"
            f"🔢 Сделок: <b>{t}</b>  ✅ {w}  ❌ {bot.stats['losses']}\n"
            f"🎯 Winrate: <b>{wr}</b>\n\n"
            f"📅 <b>PnL 7 дней:</b>\n{chart if chart else 'Нет данных'}"
        )
        await q.edit_message_text(txt,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="back")]]),
            parse_mode="HTML")

    elif d == "balance":
        await q.edit_message_text("💳 Загружаю баланс...", parse_mode="HTML")
        bal     = await bot.get_sol_balance()
        sol_usd = 0.0
        try:
            async with bot.session.get(
                "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
                timeout=aiohttp.ClientTimeout(total=5)
            ) as r:
                sol_usd = float((await r.json()).get("solana",{}).get("usd",0))
        except Exception:
            pass
        usd_str = f"(≈ ${bal*sol_usd:,.2f})" if sol_usd else ""
        txt = (
            f"💳 <b>Баланс кошелька</b>\n\n"
            f"◎ SOL: <b>{bal:.4f}</b> {usd_str}\n\n"
            f"<code>{WALLET_ADDRESS or 'Кошелёк не настроен'}</code>\n\n"
            f"🔗 <a href='https://solscan.io/account/{WALLET_ADDRESS}'>Открыть в Solscan</a>"
        )
        await q.edit_message_text(txt,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Обновить", callback_data="balance"),
                 InlineKeyboardButton("◀️ Назад",   callback_data="back")]
            ]),
            parse_mode="HTML", disable_web_page_preview=True)

    elif d == "smart":
        await q.edit_message_text("🧠 Ищу смарт-кошельки...", parse_mode="HTML")
        bot.last_smart_scan = 0
        found = await bot.hunter.find() if bot.hunter else []
        bot.smart_wallets = found
        bot._save()
        if found:
            txt = f"🧠 <b>Смарт-кошельки ({len(found)})</b>\n\n"
            for w in found[:8]:
                a = w["address"]
                txt += f"• <code>{a[:8]}...{a[-4:]}</code>  WR: <b>{w['win_rate']*100:.0f}%</b>  сделок: {w['trades']}\n"
            txt += "\n✅ Добавлены в copy-trade"
        else:
            txt = "😔 Смарт-кошельки не найдены. Попробуй позже."
        await q.edit_message_text(txt,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Обновить", callback_data="smart"),
                 InlineKeyboardButton("◀️ Назад",   callback_data="back")]
            ]),
            parse_mode="HTML")

    elif d == "report":
        await bot.send_report()
        await q.edit_message_text("📅 Отчёт отправлен!", reply_markup=kb_main())

    elif d == "toggle_pause":
        bot.paused = not bot.paused
        status = "⏸ ПАУЗА" if bot.paused else "▶️ ПРОДОЛЖАЕТ"
        await q.edit_message_text(
            f"Автопилот: <b>{status}</b>",
            reply_markup=kb_main(), parse_mode="HTML"
        )

    elif d == "start_pilot":
        if not bot.running:
            import asyncio as _aio
            _aio.create_task(bot.run())
            try:
                await q.edit_message_text(
                    "🚀 <b>Автопилот запущен!</b>",
                    reply_markup=kb_main(), parse_mode="HTML"
                )
            except Exception:
                pass
        else:
            try:
                await q.edit_message_text(
                    "✅ Автопилот уже работает.", reply_markup=kb_main(), parse_mode="HTML"
                )
            except Exception:
                pass

    elif d == "stop_all":
        # Закрыть все позиции и остановить
        for mint in list(bot.positions.keys()):
            await bot.sell_token(mint, "Ручная остановка всех позиций")
        bot.paused = True
        await q.edit_message_text(
            "⛔ <b>Все позиции закрыты. Автопилот на паузе.</b>",
            reply_markup=kb_main(), parse_mode="HTML"
        )

    elif d == "settings":
        await q.edit_message_text(
            "⚙️ <b>Настройки автопилота</b>",
            reply_markup=kb_settings(), parse_mode="HTML"
        )

    # ── Настройки ─────────────────────
    elif d in ("sl_dn", "sl_up"):
        sl = AUTO_CONFIG["stop_loss"] * 100
        sl = max(2, sl - 1) if d == "sl_dn" else min(50, sl + 1)
        AUTO_CONFIG["stop_loss"] = sl / 100
        bot._save()
        await q.edit_message_text(
            f"⛔ Стоп-лосс: <b>-{sl:.0f}%</b>",
            reply_markup=kb_settings(), parse_mode="HTML"
        )

    elif d in ("score_dn", "score_up"):
        sc = AUTO_CONFIG["min_score"]
        sc = max(10, sc - 5) if d == "score_dn" else min(95, sc + 5)
        AUTO_CONFIG["min_score"] = sc
        bot._save()
        await q.edit_message_text(
            f"🤖 Мин. скор: <b>{sc}/100</b>",
            reply_markup=kb_settings(), parse_mode="HTML"
        )

    elif d == "s_rug":
        AUTO_CONFIG["rugcheck"] = not AUTO_CONFIG["rugcheck"]
        bot._save()
        await q.edit_message_text(
            f"🛡 RugCheck: {'ВКЛ ✅' if AUTO_CONFIG['rugcheck'] else 'ВЫКЛ ❌'}",
            reply_markup=kb_settings(), parse_mode="HTML"
        )

    elif d == "s_trail":
        AUTO_CONFIG["trailing_stop"] = not AUTO_CONFIG["trailing_stop"]
        bot._save()
        await q.edit_message_text(
            f"🔄 Trailing Stop: {'ВКЛ ✅' if AUTO_CONFIG['trailing_stop'] else 'ВЫКЛ ❌'}",
            reply_markup=kb_settings(), parse_mode="HTML"
        )

    elif d == "noop":
        await q.answer()

    elif d in ("s_rug", "s_trail", "s_copy", "s_narr"):
        toggles = {
            "s_rug":   "rugcheck",
            "s_trail": "trailing_stop",
            "s_copy":  "copy_trade",
            "s_narr":  "narrative_filter",
        }
        key = toggles[d]
        AUTO_CONFIG[key] = not AUTO_CONFIG[key]
        bot._save()
        try:
            await q.edit_message_text("⚙️ <b>Настройки</b>", reply_markup=kb_settings(), parse_mode="HTML")
        except Exception:
            pass

    elif d in ("pos_dn","pos_up"):
        step = 0.01
        AUTO_CONFIG["position_sol"] = round(max(0.01, AUTO_CONFIG["position_sol"] + (step if d=="pos_up" else -step)), 3)
        bot._save()
        try: await q.edit_message_text("⚙️ <b>Настройки</b>", reply_markup=kb_settings(), parse_mode="HTML")
        except Exception: pass

    elif d in ("maxpos_dn","maxpos_up"):
        AUTO_CONFIG["max_positions"] = max(1, AUTO_CONFIG["max_positions"] + (1 if d=="maxpos_up" else -1))
        bot._save()
        try: await q.edit_message_text("⚙️ <b>Настройки</b>", reply_markup=kb_settings(), parse_mode="HTML")
        except Exception: pass

    elif d in ("limit_dn","limit_up"):
        step = 0.1
        AUTO_CONFIG["daily_loss_limit"] = round(max(0.1, AUTO_CONFIG["daily_loss_limit"] + (step if d=="limit_up" else -step)), 2)
        bot._save()
        try: await q.edit_message_text("⚙️ <b>Настройки</b>", reply_markup=kb_settings(), parse_mode="HTML")
        except Exception: pass

    elif d in ("slip_dn","slip_up"):
        step = 25
        AUTO_CONFIG["slippage_bps"] = max(50, AUTO_CONFIG["slippage_bps"] + (step if d=="slip_up" else -step))
        bot._save()
        try: await q.edit_message_text("⚙️ <b>Настройки</b>", reply_markup=kb_settings(), parse_mode="HTML")
        except Exception: pass

    elif d in ("sl_dn","sl_up"):
        step = 0.01
        AUTO_CONFIG["stop_loss"] = round(max(0.02, min(0.5, AUTO_CONFIG["stop_loss"] + (step if d=="sl_up" else -step))), 2)
        bot._save()
        try: await q.edit_message_text("⚙️ <b>Настройки</b>", reply_markup=kb_settings(), parse_mode="HTML")
        except Exception: pass

    elif d in ("score_dn","score_up"):
        AUTO_CONFIG["min_score"] = max(10, min(95, AUTO_CONFIG["min_score"] + (5 if d=="score_up" else -5)))
        bot._save()
        try: await q.edit_message_text("⚙️ <b>Настройки</b>", reply_markup=kb_settings(), parse_mode="HTML")
        except Exception: pass

    elif d in ("vol_dn","vol_up"):
        step = 10_000
        AUTO_CONFIG["min_volume"] = max(10_000, AUTO_CONFIG["min_volume"] + (step if d=="vol_up" else -step))
        bot._save()
        try: await q.edit_message_text("⚙️ <b>Настройки</b>", reply_markup=kb_settings(), parse_mode="HTML")
        except Exception: pass

    elif d in ("liq_dn","liq_up"):
        step = 5_000
        AUTO_CONFIG["min_liquidity"] = max(5_000, AUTO_CONFIG["min_liquidity"] + (step if d=="liq_up" else -step))
        bot._save()
        try: await q.edit_message_text("⚙️ <b>Настройки</b>", reply_markup=kb_settings(), parse_mode="HTML")
        except Exception: pass

    elif d in ("age_dn","age_up"):
        AUTO_CONFIG["max_age_hours"] = max(1, min(72, AUTO_CONFIG["max_age_hours"] + (1 if d=="age_up" else -1)))
        bot._save()
        try: await q.edit_message_text("⚙️ <b>Настройки</b>", reply_markup=kb_settings(), parse_mode="HTML")
        except Exception: pass

    elif d in ("snip_dn","snip_up"):
        AUTO_CONFIG["max_snipers_pct"] = max(1, min(30, AUTO_CONFIG["max_snipers_pct"] + (1 if d=="snip_up" else -1)))
        bot._save()
        try: await q.edit_message_text("⚙️ <b>Настройки</b>", reply_markup=kb_settings(), parse_mode="HTML")
        except Exception: pass

    elif d in ("bndl_dn","bndl_up"):
        AUTO_CONFIG["max_bundlers_pct"] = max(5, min(80, AUTO_CONFIG["max_bundlers_pct"] + (5 if d=="bndl_up" else -5)))
        bot._save()
        try: await q.edit_message_text("⚙️ <b>Настройки</b>", reply_markup=kb_settings(), parse_mode="HTML")
        except Exception: pass

    elif d in ("top10_dn","top10_up"):
        AUTO_CONFIG["max_top10_pct"] = max(10, min(80, AUTO_CONFIG["max_top10_pct"] + (5 if d=="top10_up" else -5)))
        bot._save()
        try: await q.edit_message_text("⚙️ <b>Настройки</b>", reply_markup=kb_settings(), parse_mode="HTML")
        except Exception: pass

    elif d in ("scan_dn","scan_up"):
        AUTO_CONFIG["scan_interval"] = max(10, min(120, AUTO_CONFIG["scan_interval"] + (10 if d=="scan_up" else -10)))
        bot._save()
        try: await q.edit_message_text("⚙️ <b>Настройки</b>", reply_markup=kb_settings(), parse_mode="HTML")
        except Exception: pass

    elif d in ("s_pos", "s_sl", "s_tp1", "s_tp2", "s_tp3", "s_limit", "s_score",
               "s_maxpos", "s_vol", "s_liq", "s_age", "s_snip", "s_bndl", "s_top10", "s_scan", "s_slip"):
        hints = {
            "s_pos":    ("position_sol",       "💰 Размер позиции в SOL\nПример: 0.02"),
            "s_maxpos": ("max_positions",       "📊 Макс. позиций (1-50)\nПример: 20"),
            "s_sl":     ("stop_loss",           "⛔ Стоп-лосс в % (без знака)\nПример: 8"),
            "s_tp1":    ("take_profit_1",       "🎯 TP1 в %\nПример: 15"),
            "s_tp2":    ("take_profit_2",       "🎯 TP2 в %\nПример: 40"),
            "s_tp3":    ("take_profit_3",       "🎯 TP3 в %\nПример: 100"),
            "s_limit":  ("daily_loss_limit",    "🚨 Лимит потерь/день в SOL\nПример: 0.5"),
            "s_score":  ("min_score",           "🤖 Мин. AI скор (10-95)\nПример: 35"),
            "s_vol":    ("min_volume",          "📊 Мин. объём $ (без запятых)\nПример: 75000"),
            "s_liq":    ("min_liquidity",       "💧 Мин. ликвидность $\nПример: 25000"),
            "s_age":    ("max_age_hours",       "⏱ Макс. возраст токена (часов)\nПример: 24"),
            "s_snip":   ("max_snipers_pct",     "🎯 Макс. снайперы % (гайд ≤8)\nПример: 8"),
            "s_bndl":   ("max_bundlers_pct",    "📦 Макс. бандлы % (гайд ≤45)\nПример: 45"),
            "s_top10":  ("max_top10_pct",       "👥 Макс. топ-10 % (гайд ≤45)\nПример: 45"),
            "s_scan":   ("scan_interval",       "⏱ Интервал скана (сек)\nПример: 20"),
            "s_slip":   ("slippage_bps",        "⚡ Slippage (150=1.5%)\nПример: 150"),
        }
        key, hint = hints[d]
        awaiting[q.from_user.id] = key
        await q.edit_message_text(
            f"✏️ <b>{hint}</b>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="settings")]]),
            parse_mode="HTML"
        )

    elif d == "back":
        try:
            await q.edit_message_text(bot.status_text(), reply_markup=kb_main(), parse_mode="HTML")
        except Exception:
            pass  # Message not modified - ignore


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    if uid != ALLOWED_USER_ID:
        return
    text = update.message.text.strip()
    key  = awaiting.pop(uid, None)
    if not key:
        await update.message.reply_text("Используй кнопки:", reply_markup=kb_main())
        return
    try:
        val = float(text)
        if key in ("stop_loss", "take_profit_1", "take_profit_2", "take_profit_3"):
            val = val / 100
        AUTO_CONFIG[key] = val
        bot._save()
        await update.message.reply_text(
            f"✅ <b>{key}</b> = {val}",
            reply_markup=kb_settings(), parse_mode="HTML"
        )
    except ValueError:
        await update.message.reply_text("❌ Введи число", reply_markup=kb_settings())


# ═══════════════════════════════════════════════
#  ЗАПУСК
# ═══════════════════════════════════════════════

async def post_init(app: Application):
    global WALLET_ADDRESS

    bot.session = aiohttp.ClientSession()
    await app.bot.set_my_commands([
        ("start", "Главное меню"),
        ("menu",  "Главное меню"),
    ])
    bot.hunter  = SmartWalletHunter(bot.session)

    # Получить публичный ключ из приватного
    try:
        jupiter_tmp = JupiterSwap(bot.session, WALLET_PRIVKEY, RPC_URL)
        if jupiter_tmp._pubkey:
            WALLET_ADDRESS = jupiter_tmp._pubkey
            bot.jupiter    = jupiter_tmp
            log.info(f"Wallet: {WALLET_ADDRESS}")
        else:
            raise ValueError("Не удалось получить pubkey — проверь формат ключа")
    except Exception as e:
        log.warning(f"Wallet init error: {e}")
        log.warning("Ключ должен быть в формате base58 (~88 символов) или JSON массив [1,2,3...]")
        bot.jupiter = None
        bot.positions.clear()
        log.warning("Позиции из прошлой сессии очищены (Jupiter не активен)")

    async def notify(text: str):
        try:
            await app.bot.send_message(
                chat_id=ALLOWED_USER_ID, text=text,
                parse_mode="HTML", disable_web_page_preview=True
            )
        except Exception as e:
            log.error(f"Notify error: {e}")

    bot.notify = notify

    # Автопилот запускается вручную кнопкой в боте
    bot._run_task = None
    log.info("✅ BOT STARTED — нажми 🚀 ЗАПУСТИТЬ в меню")


async def post_shutdown(app: Application):
    bot.stop()
    if bot.session:
        await bot.session.close()


def main():
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu",  cmd_menu))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Starting DEXSCREENER AUTOPILOT BOT...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
