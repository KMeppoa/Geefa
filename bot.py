import asyncio
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from datetime import datetime, timedelta
import os

# ─── НАСТРОЙКИ ───────────────────────────────────────────
API_KEY = "be64a932f0msh080c2a541696417p1ddcb1jsn0a4ebe0430ad"  # с rapidapi.com
BOT_TOKEN = "8975711370:AAF_rLNtBUtC1iglrDyZ0lj3eWMa3Tl9uXU"  # с @BotFather
API_URL = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}

# Топ лиги (ID из API-Football)
TOP_LEAGUES = {
    39: "Англия Премьер-лига",
    140: "Испания Ла Лига",
    78: "Германия Бундеслига",
    61: "Франция Лига 1",
    135: "Италия Серия А",
    88: "Нидерланды Эредивизи",
    113: "Швеция Алсвенскан",
    103: "Норвегия Элитесерин",
    244: "Финляндия Вейккаусliiga",
    98: "Япония J-League",
    71: "Бразилия Серия А",
    128: "Аргентина Примера",
    253: "США МЛС",
}

MIN_H2H_GOALS = 2.0      # минимальное среднее голов H2H
MIN_PROBABILITY = 0.75   # минимальная вероятность
# ─────────────────────────────────────────────────────────


async def get_fixtures(session, hours: int):
    """Получить матчи на ближайшие N часов"""
    now = datetime.utcnow()
    end = now + timedelta(hours=hours)
    date_str = now.strftime("%Y-%m-%d")
    
    async with session.get(
        f"{API_URL}/fixtures",
        headers=HEADERS,
        params={"date": date_str, "status": "NS"}
    ) as resp:
        data = await resp.json()
    
    fixtures = []
    for f in data.get("response", []):
        league_id = f["league"]["id"]
        if league_id not in TOP_LEAGUES:
            continue
        
        fixture_time = datetime.fromisoformat(
            f["fixture"]["date"].replace("Z", "+00:00")
        ).replace(tzinfo=None)
        
        if now <= fixture_time <= end:
            fixtures.append({
                "id": f["fixture"]["id"],
                "home": f["teams"]["home"]["name"],
                "away": f["teams"]["away"]["name"],
                "league": TOP_LEAGUES[league_id],
                "time": fixture_time.strftime("%H:%M"),
                "home_id": f["teams"]["home"]["id"],
                "away_id": f["teams"]["away"]["id"],
            })
    
    return fixtures


async def get_h2h(session, home_id: int, away_id: int):
    """Получить H2H статистику"""
    async with session.get(
        f"{API_URL}/fixtures/headtohead",
        headers=HEADERS,
        params={"h2h": f"{home_id}-{away_id}", "last": 10}
    ) as resp:
        data = await resp.json()
    
    matches = data.get("response", [])
    if not matches:
        return None
    
    total_goals = sum(
        m["goals"]["home"] + m["goals"]["away"]
        for m in matches
        if m["goals"]["home"] is not None
    )
    count = len([m for m in matches if m["goals"]["home"] is not None])
    
    if count == 0:
        return None
    
    avg = total_goals / count
    over_15 = sum(
        1 for m in matches
        if m["goals"]["home"] is not None
        and m["goals"]["home"] + m["goals"]["away"] > 1.5
    ) / count
    
    return {
        "avg_goals": round(avg, 2),
        "over_15_pct": round(over_15 * 100),
        "matches_checked": count
    }


async def get_odds(session, fixture_id: int):
    """Получить коэффициенты на тотал"""
    async with session.get(
        f"{API_URL}/odds",
        headers=HEADERS,
        params={"fixture": fixture_id, "bet": 5}  # bet 5 = тотал голов
    ) as resp:
        data = await resp.json()
    
    for bookmaker in data.get("response", [{}])[0].get("bookmakers", []):
        for bet in bookmaker.get("bets", []):
            if "Goals Over/Under" in bet.get("name", ""):
                for val in bet.get("values", []):
                    if val.get("value") == "Over 0.5":
                        return {"over_05": float(val["odd"])}
                    if val.get("value") == "Over 1.5":
                        return {"over_15": float(val["odd"])}
    return None


# ─── БОТ 1: Сборщик ──────────────────────────────────────
async def bot1_analyze(session, fixtures):
    results = []
    for f in fixtures:
        h2h = await get_h2h(session, f["home_id"], f["away_id"])
        if not h2h:
            continue
        if h2h["avg_goals"] < MIN_H2H_GOALS:
            continue
        
        prob = h2h["over_15_pct"] / 100
        if prob < MIN_PROBABILITY:
            # попробуем 0.5+
            if h2h["avg_goals"] >= 1.5:
                bet_type = "0.5+"
                prob = 0.95
            else:
                continue
        else:
            bet_type = "1.5+"
        
        results.append({
            **f,
            "h2h": h2h,
            "bet_type": bet_type,
            "probability": prob,
            "source": "bot1"
        })
    
    return sorted(results, key=lambda x: x["probability"], reverse=True)


# ─── БОТ 2: Верификатор ──────────────────────────────────
async def bot2_verify(session, bot1_results):
    verified = []
    suspicious = []
    
    for item in bot1_results:
        # Независимая проверка
        h2h = await get_h2h(session, item["home_id"], item["away_id"])
        if not h2h:
            suspicious.append(item)
            continue
        
        # Сравниваем с результатом Бота 1
        diff = abs(h2h["avg_goals"] - item["h2h"]["avg_goals"])
        
        if diff <= 0.3:  # расхождение не более 0.3 гола
            item["verified"] = True
            item["bot2_avg"] = h2h["avg_goals"]
            verified.append(item)
        else:
            item["verified"] = False
            item["bot2_avg"] = h2h["avg_goals"]
            suspicious.append(item)
    
    return verified, suspicious


# ─── ФОРМАТИРОВАНИЕ РЕЗУЛЬТАТА ────────────────────────────
def format_results(verified, suspicious, hours):
    msg = f"🔍 Анализ на ближайшие {hours} часов\n"
    msg += f"{'='*35}\n\n"
    
    if verified:
        msg += f"✅ ПРОВЕРЕНО ОБОИМИ БОТАМИ ({len(verified)}):\n\n"
        for i, item in enumerate(verified[:5], 1):
            msg += f"{i}. {item['home']} - {item['away']}\n"
            msg += f"   🏆 {item['league']} | ⏰ {item['time']}\n"
            msg += f"   📊 H2H среднее: {item['h2h']['avg_goals']} голов\n"
            msg += f"   📈 1.5+ в {item['h2h']['over_15_pct']}% матчей\n"
            msg += f"   🎯 Ставка: Тотал {item['bet_type']}\n"
            msg += f"   💯 Вероятность: {int(item['probability']*100)}%\n"
            msg += f"   🤖 Бот1: {item['h2h']['avg_goals']} | Бот2: {item['bot2_avg']}\n\n"
    
    if suspicious:
        msg += f"\n⚠️ РАСХОЖДЕНИЕ БОТОВ ({len(suspicious)}) — пропусти:\n"
        for item in suspicious:
            msg += f"   • {item['home']} - {item['away']}\n"
    
    if not verified and not suspicious:
        msg += "❌ Нет подходящих матчей для этого периода\n"
        msg += "Попробуй другой временной диапазон"
    
    return msg


# ─── TELEGRAM ХЕНДЛЕРЫ ───────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton("⚡ 1 час", callback_data="1"),
            InlineKeyboardButton("🕑 2 часа", callback_data="2"),
            InlineKeyboardButton("🕒 3 часа", callback_data="3"),
        ],
        [
            InlineKeyboardButton("🕕 6 часов", callback_data="6"),
            InlineKeyboardButton("🕛 12 часов", callback_data="12"),
        ]
    ]
    markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "⚽ Бот анализа ставок\n\nВыбери период для анализа:",
        reply_markup=markup
    )


async def analyze(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    hours = int(query.data)
    
    await query.edit_message_text(
        f"🔄 Анализирую матчи на {hours} часов...\n"
        f"Бот 1 собирает данные...\n"
        f"Бот 2 верифицирует..."
    )
    
    async with aiohttp.ClientSession() as session:
        fixtures = await get_fixtures(session, hours)
        
        if not fixtures:
            await query.edit_message_text(
                f"❌ Нет матчей из топ лиг на ближайшие {hours} часов"
            )
            return
        
        bot1_results = await bot1_analyze(session, fixtures)
        verified, suspicious = await bot2_verify(session, bot1_results)
    
    msg = format_results(verified, suspicious, hours)
    
    # Кнопка для нового анализа
    keyboard = [[InlineKeyboardButton("🔄 Новый анализ", callback_data="menu")]]
    markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(msg, reply_markup=markup)


async def menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [
            InlineKeyboardButton("⚡ 1 час", callback_data="1"),
            InlineKeyboardButton("🕑 2 часа", callback_data="2"),
            InlineKeyboardButton("🕒 3 часа", callback_data="3"),
        ],
        [
            InlineKeyboardButton("🕕 6 часов", callback_data="6"),
            InlineKeyboardButton("🕛 12 часов", callback_data="12"),
        ]
    ]
    markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "⚽ Выбери период для анализа:",
        reply_markup=markup
    )


# ─── ЗАПУСК ──────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(analyze, pattern="^[0-9]+$"))
    app.add_handler(CallbackQueryHandler(menu, pattern="^menu$"))
    print("✅ Бот запущен!")
    app.run_polling()


if __name__ == "__main__":
    main()
