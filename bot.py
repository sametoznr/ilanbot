import os
import re
import json
import time
import sqlite3
import logging
import urllib3
import requests
import cloudscraper
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_CHAT_ID")
DB_FILE = "bot_data.db"

FILTER_LABELS = {
    "4437": "4437 (Lisans Özel)",
    "3003": "3003 (Adalet)",
    "3001": "3001 (Önlisans Şartsız)",
    "ANY_LISANS": "Şartsız Lisans (Genel)"
}

ANY_DEGREE_KEYWORDS = [
    "herhangi bir lisans",
    "tüm lisans mezunları",
    "bölüm şartı aranmamaktadır",
    "lisans mezunu olmak",
    "dört yıllık fakülte",
    "4 yıllık fakülte"
]

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            filters TEXT DEFAULT '["4437", "3003", "3001", "ANY_LISANS"]',
            is_active INTEGER DEFAULT 1,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS seen_ads (
            ad_id TEXT PRIMARY KEY,
            title TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    conn.close()

def get_state(key, default="0"):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT value FROM bot_state WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else default

def set_state(key, value):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

def register_or_get_user(chat_id, username="", first_name=""):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT filters, is_active FROM users WHERE chat_id = ?", (chat_id,))
    row = c.fetchone()
    if not row:
        default_filters = json.dumps(["4437", "3003", "3001", "ANY_LISANS"])
        c.execute(
            "INSERT INTO users (chat_id, username, first_name, filters, is_active) VALUES (?, ?, ?, ?, 1)",
            (chat_id, username, first_name, default_filters)
        )
        conn.commit()
        conn.close()
        return ["4437", "3003", "3001", "ANY_LISANS"], 1
    conn.close()
    return json.loads(row[0]), row[1]

def toggle_user_filter(chat_id, filter_code):
    filters, is_active = register_or_get_user(chat_id)
    if filter_code in filters:
        filters.remove(filter_code)
    else:
        filters.append(filter_code)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE users SET filters = ? WHERE chat_id = ?", (json.dumps(filters), chat_id))
    conn.commit()
    conn.close()
    return filters

def toggle_user_active(chat_id):
    filters, is_active = register_or_get_user(chat_id)
    new_status = 0 if is_active == 1 else 1
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE users SET is_active = ? WHERE chat_id = ?", (new_status, chat_id))
    conn.commit()
    conn.close()
    return new_status

def tg_request(method, payload=None):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    try:
        r = requests.post(url, json=payload or {}, timeout=15)
        return r.json()
    except Exception as e:
        logger.error(f"Telegram API Hatası ({method}): {e}")
        return {"ok": False}

def build_main_menu(chat_id):
    filters, is_active = register_or_get_user(chat_id)
    keyboard = []
    for code, label in FILTER_LABELS.items():
        icon = "✅" if code in filters else "❌"
        keyboard.append([{"text": f"{icon} {label}", "callback_data": f"toggle_{code}"}])
    
    notif_text = "🔔 Bildirimler: AÇIK (Kapatmak için tıkla)" if is_active else "🔕 Bildirimler: KAPALI (Açmak için tıkla)"
    keyboard.append([{"text": notif_text, "callback_data": "toggle_notif"}])
    keyboard.append([{"text": "🔍 Son İlanları Şimdi Kontrol Et", "callback_data": "force_check"}])
    
    if ADMIN_ID and str(chat_id) == str(ADMIN_ID):
        keyboard.append([{"text": "👑 Admin İstatistik Paneli", "callback_data": "admin_stats"}])
        
    return {"inline_keyboard": keyboard}

def analyze_text_for_matches(text):
    text_lower = text.lower()
    matched_codes = []
    for code in ["4437", "3003", "3001"]:
        if re.search(rf"\b{code}\b", text_lower):
            matched_codes.append(code)
    for kw in ANY_DEGREE_KEYWORDS:
        if kw in text_lower:
            matched_codes.append("ANY_LISANS")
            break
    return list(set(matched_codes))

def scrape_government_ads():
    scraper = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "mobile": False})
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    found_ads = []
    sources = [
        "https://kamuilan.sbb.gov.tr/",
        "https://www.ilan.gov.tr/ilan/kategori/12/kamu-akademik-personel"
    ]
    for url in sources:
        try:
            resp = scraper.get(url, headers=headers, timeout=20, verify=False)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "lxml")
            for a in soup.find_all("a", href=True):
                title = a.get_text(strip=True)
                href = a["href"]
                if len(title) < 15:
                    continue
                if not href.startswith("http"):
                    base = "/".join(url.split("/")[:3])
                    href = base.rstrip("/") + "/" + href.lstrip("/")
                parent_text = a.parent.get_text(" ", strip=True) if a.parent else title
                matches = analyze_text_for_matches(f"{title} {parent_text}")
                
                if not matches and any(k in title.lower() for k in ["personel alım", "memur alım", "sözleşmeli", "kpss"]):
                    try:
                        sub_resp = scraper.get(href, headers=headers, timeout=10, verify=False)
                        if sub_resp.status_code == 200:
                            matches = analyze_text_for_matches(sub_resp.text)
                    except Exception:
                        pass
                if matches:
                    ad_id = re.sub(r"[^a-zA-Z0-9]", "", href)[-40:]
                    found_ads.append({"id": ad_id, "title": title[:200], "link": href, "matches": matches})
        except Exception as e:
            logger.error(f"Kaynak hatası [{url}]: {e}")
            continue
    return list({ad["id"]: ad for ad in found_ads}.values())

def process_telegram_updates():
    offset = int(get_state("last_update_id", "0"))
    resp = tg_request("getUpdates", {"offset": offset + 1, "timeout": 5})
    if not resp.get("ok"):
        return
    for update in resp.get("result", []):
        set_state("last_update_id", update["update_id"])
        if "message" in update:
            msg = update["message"]
            chat_id = msg["chat"]["id"]
            text = msg.get("text", "")
            username = msg["from"].get("username", "")
            first_name = msg["from"].get("first_name", "Kullanıcı")
            register_or_get_user(chat_id, username, first_name)
            
            if text.startswith("/start"):
                welcome_text = (
                    f"👋 **Selam {first_name}!** Kamu İlan Takip Botuna hoş geldin.\n\n"
                    f"Aşağıdaki butonlara tıklayarak bildirim almak istediğin **kriterleri** açıp kapatabilirsin:"
                )
                tg_request("sendMessage", {
                    "chat_id": chat_id,
                    "text": welcome_text,
                    "parse_mode": "Markdown",
                    "reply_markup": build_main_menu(chat_id)
                })
            elif text.startswith("/duyuru ") and ADMIN_ID and str(chat_id) == str(ADMIN_ID):
                duyuru_metni = text.replace("/duyuru ", "", 1)
                conn = sqlite3.connect(DB_FILE)
                users = conn.cursor().execute("SELECT chat_id FROM users WHERE is_active = 1").fetchall()
                conn.close()
                sent_count = 0
                for (u_id,) in users:
                    if tg_request("sendMessage", {"chat_id": u_id, "text": f"📢 **DUYURU**\n\n{duyuru_metni}", "parse_mode": "Markdown"}).get("ok"):
                        sent_count += 1
                tg_request("sendMessage", {"chat_id": chat_id, "text": f"✅ Duyuru {sent_count} kişiye iletildi."})

        elif "callback_query" in update:
            cb = update["callback_query"]
            cb_id = cb["id"]
            chat_id = cb["message"]["chat"]["id"]
            msg_id = cb["message"]["message_id"]
            data = cb.get("data", "")
            
            if data.startswith("toggle_"):
                code = data.replace("toggle_", "")
                if code == "notif":
                    status = toggle_user_active(chat_id)
                    info = "Bildirimler açıldı 🔔" if status else "Bildirimler kapatıldı 🔕"
                else:
                    toggle_user_filter(chat_id, code)
                    info = f"{code} filtresi güncellendi ✅"
                tg_request("answerCallbackQuery", {"callback_query_id": cb_id, "text": info})
                tg_request("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": msg_id, "reply_markup": build_main_menu(chat_id)})
            elif data == "force_check":
                tg_request("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Kaynaklar taranıyor..."})
                ads = scrape_government_ads()
                if not ads:
                    tg_request("sendMessage", {"chat_id": chat_id, "text": "📭 Şu an kriterlere uyan yeni bir ilan bulunamadı."})
                else:
                    for ad in ads[:5]:
                        labels = [FILTER_LABELS.get(m, m) for m in ad["matches"]]
                        tg_request("sendMessage", {
                            "chat_id": chat_id,
                            "text": f"📌 **{ad['title']}**\n🎯 Eşleşen: `{', '.join(labels)}`\n🔗 [İlana Git]({ad['link']})",
                            "parse_mode": "Markdown",
                            "disable_web_page_preview": True
                        })
            elif data == "admin_stats" and ADMIN_ID and str(chat_id) == str(ADMIN_ID):
                conn = sqlite3.connect(DB_FILE)
                c = conn.cursor()
                total_u = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
                active_u = c.execute("SELECT COUNT(*) FROM users WHERE is_active = 1").fetchone()[0]
                total_ads = c.execute("SELECT COUNT(*) FROM seen_ads").fetchone()[0]
                conn.close()
                stats_msg = (
                    f"👑 **ADMİN PANELİ**\n\n"
                    f"👥 **Toplam Kullanıcı:** {total_u}\n"
                    f"🔔 **Aktif Bildirim Alan:** {active_u}\n"
                    f"📄 **Taranan İlan:** {total_ads}\n\n"
                    f"💡 *Duyuru atmak için:* `/duyuru mesajınız`"
                )
                tg_request("answerCallbackQuery", {"callback_query_id": cb_id})
                tg_request("sendMessage", {"chat_id": chat_id, "text": stats_msg, "parse_mode": "Markdown"})

def broadcast_new_ads():
    ads = scrape_government_ads()
    if not ads:
        return
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    for ad in ads:
        c.execute("SELECT 1 FROM seen_ads WHERE ad_id = ?", (ad["id"],))
        if c.fetchone():
            continue
        c.execute("INSERT INTO seen_ads (ad_id, title) VALUES (?, ?)", (ad["id"], ad["title"]))
        conn.commit()
        users = c.execute("SELECT chat_id, filters FROM users WHERE is_active = 1").fetchall()
        for chat_id, filters_json in users:
            common = set(ad["matches"]).intersection(set(json.loads(filters_json)))
            if common:
                matched_names = [FILTER_LABELS.get(code, code) for code in common]
                msg_text = (
                    f"🚨 **YENİ KAMU İLANI!**\n\n"
                    f"🏢 **İlan:** {ad['title']}\n"
                    f"🎯 **Şart:** `{', '.join(matched_names)}`\n\n"
                    f"🔗 [İlan Detaylarına Git]({ad['link']})"
                )
                tg_request("sendMessage", {"chat_id": chat_id, "text": msg_text, "parse_mode": "Markdown", "disable_web_page_preview": True})
                time.sleep(0.05)
    conn.close()

def main():
    if not TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN eksik!")
        return
    init_db()
    for _ in range(6):
        process_telegram_updates()
        time.sleep(5)
    broadcast_new_ads()

if __name__ == "__main__":
    main()
