# -*- coding: utf-8 -*-
"""
bot.py - Bot tín hiệu XAUUSD (Vàng) chạy trên PythonAnywhere

Luồng hoạt động (mỗi 15 phút, ngay sau khi nến M15 đóng):
    1. Lấy OHLC nến M15 (ngắn hạn) và H1 (xu hướng chính) của XAU/USD từ Twelve Data
    2. Định dạng thành bảng văn bản
    3. Gửi cho AI (Gemini miễn phí / Anthropic / OpenAI) kèm System Prompt
    4. Gửi kết quả về Telegram (parse_mode=HTML)

Chốt sổ số dư qua Telegram:
    - Gửi cho bot: /sodu 95.5   -> cập nhật số dư mới (bot ghim tin xác nhận để nhớ)
    - Gửi cho bot: /sodu        -> xem số dư bot đang dùng
    Lệnh được xử lý ở lượt chạy kế tiếp (tối đa ~15 phút).

Hai chế độ chạy:
    - RUN_ONCE=true  : chạy 1 lần rồi thoát (dùng cho GitHub Actions, miễn phí)
    - RUN_ONCE=false : tự lặp mỗi 15 phút (dùng cho VPS / PythonAnywhere trả phí)

Toàn bộ khóa API được đọc từ biến môi trường / file .env, KHÔNG ghi thẳng vào code.
"""

import os
import re
import sys
import html
import time
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta, timezone

import requests
import schedule

# Nạp file .env nằm cùng thư mục với bot.py (nếu có cài python-dotenv)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, ".env"))
except ImportError:
    pass


# =============================================================================
# BƯỚC 0: CẤU HÌNH
# =============================================================================
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Chọn nhà cung cấp AI: "gemini" (miễn phí), "anthropic" hoặc "openai"
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").strip().lower()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# Có thể khai báo nhiều model, cách nhau dấu phẩy: model đầu lỗi/quá tải sẽ tự chuyển sang model sau
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash,gemini-3.7-flash,gemini-3.5-flash-lite")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")

SYMBOL = "XAU/USD"

# Khung ngắn hạn M15: tìm điểm vào lệnh, mô hình nến
CANDLE_COUNT = int(os.getenv("CANDLE_COUNT", "10"))
# Khung H1: xác định xu hướng chính và vùng cản (50 nến ~ 2 ngày giao dịch)
H1_CANDLE_COUNT = int(os.getenv("H1_CANDLE_COUNT", "50"))

# Mỗi chu kỳ gọi Twelve Data 2 lần (M15 + H1) -> ~192 credit/ngày, gói free có 800

# true  = gửi cả tín hiệu WAIT lên Telegram
# false = chỉ gửi khi có BUY/SELL (đỡ spam)
SEND_WAIT_SIGNALS = os.getenv("SEND_WAIT_SIGNALS", "true").strip().lower() == "true"

# Quản trị vốn: số dư tài khoản (USD) và % rủi ro tối đa cho phép mỗi lệnh
# ACCOUNT_BALANCE chỉ là số dư mặc định, dùng khi chưa chốt sổ qua Telegram (/sodu)
ACCOUNT_BALANCE = float(os.getenv("ACCOUNT_BALANCE", "100"))
MAX_RISK_PERCENT = float(os.getenv("MAX_RISK_PERCENT", "3"))

# true = chạy 1 chu kỳ rồi thoát (GitHub Actions tự lên lịch thay cho vòng lặp)
RUN_ONCE = os.getenv("RUN_ONCE", "false").strip().lower() == "true"

REQUEST_TIMEOUT = 30          # giây, cho Twelve Data & Telegram
LLM_TIMEOUT = 120             # giây, AI có thể trả lời chậm
MAX_RETRIES = 3               # số lần thử lại khi gặp Timeout / 5xx
RETRY_STATUS = {429, 500, 502, 503, 504, 529}   # các mã lỗi nên thử lại
TELEGRAM_MAX_LEN = 4000       # Telegram giới hạn 4096 ký tự / tin nhắn

VN_TZ = timezone(timedelta(hours=7))


# =============================================================================
# SYSTEM PROMPT CHO AI (đã chuyển sang định dạng HTML của Telegram)
# =============================================================================
SYSTEM_PROMPT_TEMPLATE = """[SYSTEM ROLE]
Bạn là một Nhà giao dịch Chuyên nghiệp (Professional Forex & Gold Futures Trader) với hơn 10 năm kinh nghiệm phân tích Price Action, nến Nhật và cấu trúc thị trường dành riêng cho cặp XAUUSD (Vàng).

Nhiệm vụ của bạn là tiếp nhận dữ liệu giá OHLC, phân tích xu hướng hiện tại, đưa ra nhận định khách quan và xuất ra tín hiệu giao dịch tối ưu được đóng gói sẵn để gửi trực tiếp qua Telegram Bot.

---

[QUY ƯỚC BẮT BUỘC]
- 1 pip XAUUSD = 0.10 USD biến động giá (VD: giá đi từ 2650.00 lên 2651.00 = 10 pips).
- 0.01 lot = 1 ounce, giá trị 0.10 USD mỗi pip.
- Dữ liệu được cung cấp gồm 2 bảng nến đã đóng: H1 (xác định xu hướng chính, vùng kháng cự/hỗ trợ, đỉnh/đáy quan trọng) và M15 (xác định mô hình nến và điểm vào lệnh). Xu hướng H4 được ước lượng bằng cách gộp 4 nến H1 liên tiếp.
- Nếu thiếu bảng H1, hãy nói rõ trong phần phân tích, chỉ dựa vào M15 và ưu tiên WAIT.
- Dùng đúng thời gian hiện tại được cung cấp trong tin nhắn, không tự bịa thời gian.

---

[ANALYSIS METHODOLOGY]
Phân tích qua 4 bước:

1. Cấu trúc thị trường & Xu hướng:
   - Xác định xu hướng chính và xu hướng ngắn hạn.
   - Xác định các vùng cản quan trọng: Kháng cự, Hỗ trợ, Vùng thanh khoản, Order Block gần nhất.

2. Phân tích Hành vi Nến:
   - Nhận diện mô hình nến đảo chiều hoặc tiếp diễn: Pinbar, Bearish/Bullish Engulfing, Inside Bar, Doji từ chối giá.
   - Kiểm tra các cú quét thanh khoản (Fakeout / Liquidity Sweep) quanh các đỉnh/đáy gần nhất.

3. Quản trị Rủi ro & Điểm vào lệnh:
   - Chỉ đề xuất lệnh khi tỷ lệ R:R tối thiểu 1:1.5.
   - Không bắt đỉnh/đáy nếu chưa có nến xác nhận đảo chiều.
   - Vốn rủi ro = Balance x Risk_% (mặc định 1% - 2%).
   - Lot Size = Vốn rủi ro / (Khoảng cách SL theo pip x 0.10 USD) x 0.01.
   - Số dư tài khoản: __BALANCE__ USD. Rủi ro tối đa cho phép mỗi lệnh: __MAX_RISK__% tài khoản (= __MAX_RISK_USD__ USD).
   - Nếu Lot Size tính ra nhỏ hơn 0.01: dùng 0.01 lot và ghi rõ % rủi ro thực tế. Nếu rủi ro thực tế vượt __MAX_RISK__% tài khoản, chuyển tín hiệu sang WAIT.

4. Đánh giá Rủi ro:
   - THẤP: Thuận xu hướng chính + có nến xác nhận tại vùng cản cứng.
   - TRUNG BÌNH: Đánh nhịp hồi (Pullback/Retest) + có nến xác nhận.
   - CAO: Đánh ngược xu hướng hoặc sát thời điểm ra tin tức lớn.

---

[OUTPUT FORMAT - TELEGRAM HTML]
Quy tắc định dạng:
- CHỈ dùng các thẻ HTML Telegram hỗ trợ: <b>, <i>, <u>, <code>. KHÔNG dùng Markdown (không dùng ** hay __).
- KHÔNG viết ký tự <, > hoặc & ở ngoài thẻ HTML (viết "nhỏ hơn", "lớn hơn", "và" thay thế).
- Chỉ trả về nội dung tin nhắn, không có lời dẫn, không bọc trong khối code.

Mẫu bắt buộc:

📊 <b>[XAUUSD] BÁO CÁO & TÍN HIỆU GIAO DỊCH</b>
---
⏰ <b>Thời gian:</b> [thời gian hiện tại được cung cấp]
📈 <b>Xu hướng chính:</b> [TĂNG / GIẢM / SIDEWAY] (Khung H1/H4)
🔍 <b>Phân tích ngắn gọn:</b> [2-3 câu về mô hình nến và vùng cản hiện tại]

🚨 <b>TÍN HIỆU GIAO DỊCH:</b> [BUY / SELL / WAIT (ĐỨNG NGOÀI)]

(Nếu BUY/SELL thì xuất đầy đủ phần dưới. Nếu WAIT thì chỉ nêu lý do và bỏ qua phần Entry/SL/TP/Quản trị vốn.)

📍 <b>Vùng vào lệnh (Entry):</b> [giá hoặc khoảng giá]
🛑 <b>Cắt lỗ (Stop Loss):</b> [giá SL] ([số pips] pips)
🎯 <b>Chốt lời 1 (TP1):</b> [giá TP1] (R:R = 1:1.5)
🎯 <b>Chốt lời 2 (TP2):</b> [giá TP2] (R:R = 1:2 trở lên)

⚖️ <b>QUẢN TRỊ VỐN ĐỀ XUẤT (Cho TK __BALANCE__ USD):</b>
- <b>Khối lượng khuyến nghị:</b> [Lot Size]
- <b>Mức lỗ tối đa khi chạm SL:</b> -$[số tiền] ([% tài khoản])
- <b>Mức lãi kỳ vọng khi chạm TP1:</b> +$[số tiền]

⚠️ <b>Mức độ rủi ro:</b> [THẤP / TRUNG BÌNH / CAO]
💡 <b>Lưu ý kỷ luật:</b> [một câu nhắc quản lý tâm lý hoặc xử lý lệnh khi giá chạy được 50% TP]
"""


def build_system_prompt(balance):
    """Điền số dư hiện tại và mức rủi ro tối đa vào System Prompt."""
    max_risk_usd = round(balance * MAX_RISK_PERCENT / 100, 2)
    return (SYSTEM_PROMPT_TEMPLATE
            .replace("__BALANCE__", f"{balance:g}")
            .replace("__MAX_RISK_USD__", f"{max_risk_usd:g}")
            .replace("__MAX_RISK__", f"{MAX_RISK_PERCENT:g}"))


# =============================================================================
# LOGGING: in ra console (xem trong log task của PythonAnywhere) + ghi file bot.log
# =============================================================================
log = logging.getLogger("xau_bot")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

_console = logging.StreamHandler(sys.stdout)
_console.setFormatter(_fmt)
log.addHandler(_console)

_file = RotatingFileHandler(os.path.join(BASE_DIR, "bot.log"),
                            maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
_file.setFormatter(_fmt)
log.addHandler(_file)


# =============================================================================
# HÀM TIỆN ÍCH: gọi HTTP có thử lại khi Timeout / Server Error / Bad Gateway
# =============================================================================
def http_request(method, url, name, **kwargs):
    """
    Gọi HTTP với cơ chế retry (backoff 2s, 4s, ...).
    Trả về đối tượng Response, hoặc None nếu thất bại hết số lần thử.
    Không bao giờ ném exception ra ngoài -> vòng lặp chính không bị crash.
    """
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.request(method, url, **kwargs)
            if resp.status_code in RETRY_STATUS:
                log.warning("[%s] Server trả mã %s (lần %d/%d): %s",
                            name, resp.status_code, attempt, MAX_RETRIES,
                            resp.text[:200].replace("\n", " "))
            else:
                return resp
        except requests.Timeout:
            log.warning("[%s] Timeout (lần %d/%d)", name, attempt, MAX_RETRIES)
        except requests.ConnectionError as e:
            log.warning("[%s] Lỗi kết nối (lần %d/%d): %s", name, attempt, MAX_RETRIES, e)
        except requests.RequestException as e:
            log.error("[%s] Lỗi request không thử lại được: %s", name, e)
            return None

        if attempt < MAX_RETRIES:
            time.sleep(2 ** attempt)

    log.error("[%s] Thất bại sau %d lần thử.", name, MAX_RETRIES)
    return None


# =============================================================================
# BƯỚC 1: LẤY DỮ LIỆU NẾN TỪ TWELVE DATA
# =============================================================================
def fetch_candles(interval, count, candle_minutes):
    """
    Lấy các nến ĐÃ ĐÓNG gần nhất của XAU/USD cho một khung thời gian.
        interval       : "15min", "1h", ... (theo chuẩn Twelve Data)
        count          : số nến cần lấy
        candle_minutes : độ dài 1 nến tính bằng phút (dùng để loại nến chưa đóng)
    Trả về list dict [{time, open, high, low, close}] sắp xếp cũ -> mới, hoặc None nếu lỗi.
    """
    name = f"TwelveData {interval}"
    try:
        resp = http_request(
            "GET",
            "https://api.twelvedata.com/time_series",
            name,
            params={
                "symbol": SYMBOL,
                "interval": interval,
                # Lấy dư 1 nến để còn đủ số lượng sau khi bỏ nến đang hình thành
                "outputsize": count + 1,
                "timezone": "UTC",
                "apikey": TWELVE_DATA_API_KEY,
            },
        )
        if resp is None:
            return None

        data = resp.json()
        if data.get("status") != "ok":
            # VD: hết credit, sai API key, sai symbol...
            log.error("[%s] API báo lỗi: code=%s, message=%s",
                      name, data.get("code"), data.get("message"))
            return None

        candles = []
        for v in data.get("values", []):
            start = datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            candles.append({
                "time": start,
                "open": float(v["open"]),
                "high": float(v["high"]),
                "low": float(v["low"]),
                "close": float(v["close"]),
            })

        # Twelve Data trả nến mới nhất trước -> đảo lại thành cũ -> mới
        candles.sort(key=lambda c: c["time"])

        # Loại nến chưa đóng (thời điểm mở + 15 phút > hiện tại)
        now = datetime.now(timezone.utc)
        candles = [c for c in candles if c["time"] + timedelta(minutes=candle_minutes) <= now]

        candles = candles[-count:]
        if not candles:
            log.warning("[%s] Không có nến đã đóng nào trong dữ liệu trả về.", name)
            return None

        log.info("[%s] Lấy được %d nến, nến cuối mở lúc %s UTC",
                 name, len(candles), candles[-1]["time"].strftime("%Y-%m-%d %H:%M"))
        return candles

    except (ValueError, KeyError, TypeError) as e:
        log.error("[%s] Dữ liệu trả về không đúng định dạng: %s", name, e)
        return None
    except Exception:
        log.exception("[%s] Lỗi không mong muốn", name)
        return None


def format_candles(candles):
    """Chuyển list nến thành bảng văn bản cho AI đọc (giờ Việt Nam GMT+7)."""
    lines = ["Thời gian (GMT+7) | Open | High | Low | Close"]
    for c in candles:
        t = c["time"].astimezone(VN_TZ).strftime("%Y-%m-%d %H:%M")
        lines.append(f"{t} | {c['open']:.2f} | {c['high']:.2f} | {c['low']:.2f} | {c['close']:.2f}")
    return "\n".join(lines)


# =============================================================================
# BƯỚC 2: GỬI DỮ LIỆU CHO AI PHÂN TÍCH
# =============================================================================
def build_user_message(m15_text, h1_text):
    """Tạo nội dung tin nhắn gửi AI: thời gian hiện tại + bảng nến H1 + bảng nến M15."""
    now_vn = datetime.now(VN_TZ).strftime("%H:%M %d/%m/%Y (GMT+7)")
    if h1_text:
        h1_block = f"=== KHUNG H1 ({H1_CANDLE_COUNT} nến đã đóng, cũ -> mới) ===\n{h1_text}"
    else:
        h1_block = "=== KHUNG H1 ===\n(Không lấy được dữ liệu H1 ở chu kỳ này)"
    return (
        f"Thời gian hiện tại: {now_vn}\n"
        f"Cặp: XAUUSD\n\n"
        f"{h1_block}\n\n"
        f"=== KHUNG M15 ({CANDLE_COUNT} nến đã đóng, cũ -> mới) ===\n{m15_text}\n\n"
        "Hãy phân tích theo đúng 4 bước: dùng H1 để xác định xu hướng chính và vùng cản, "
        "dùng M15 để tìm mô hình nến và điểm vào lệnh. Trả về tín hiệu theo mẫu HTML đã quy định."
    )


def call_anthropic(system_prompt, user_message):
    """Gọi Anthropic Messages API bằng requests. Trả về text hoặc None."""
    try:
        resp = http_request(
            "POST",
            "https://api.anthropic.com/v1/messages",
            "Anthropic",
            timeout=LLM_TIMEOUT,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 1500,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_message}],
            },
        )
        if resp is None:
            return None
        if resp.status_code != 200:
            log.error("[Anthropic] HTTP %s: %s", resp.status_code, resp.text[:500])
            return None

        data = resp.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        return text.strip() or None

    except (ValueError, KeyError, TypeError) as e:
        log.error("[Anthropic] Phản hồi không đúng định dạng: %s", e)
        return None
    except Exception:
        log.exception("[Anthropic] Lỗi không mong muốn")
        return None


def call_openai(system_prompt, user_message):
    """Gọi OpenAI Chat Completions API bằng requests. Trả về text hoặc None."""
    try:
        resp = http_request(
            "POST",
            "https://api.openai.com/v1/chat/completions",
            "OpenAI",
            timeout=LLM_TIMEOUT,
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENAI_MODEL,
                "max_tokens": 1500,
                "temperature": 0.3,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
            },
        )
        if resp is None:
            return None
        if resp.status_code != 200:
            log.error("[OpenAI] HTTP %s: %s", resp.status_code, resp.text[:500])
            return None

        data = resp.json()
        text = data["choices"][0]["message"]["content"] or ""
        return text.strip() or None

    except (ValueError, KeyError, IndexError, TypeError) as e:
        log.error("[OpenAI] Phản hồi không đúng định dạng: %s", e)
        return None
    except Exception:
        log.exception("[OpenAI] Lỗi không mong muốn")
        return None


def call_gemini(system_prompt, user_message):
    """
    Gọi lần lượt các model Gemini trong GEMINI_MODEL.
    Model đầu quá tải (503) hoặc không khả dụng (404) thì tự chuyển sang model tiếp theo.
    """
    models = [m.strip() for m in GEMINI_MODEL.split(",") if m.strip()]
    for model in models:
        result = call_gemini_model(model, system_prompt, user_message)
        if result:
            return result
        log.warning("[Gemini] Model %s không trả kết quả, thử model tiếp theo (nếu còn).", model)
    return None


def call_gemini_model(model, system_prompt, user_message):
    """Gọi 1 model Google Gemini (có gói miễn phí) bằng requests. Trả về text hoặc None."""
    try:
        resp = http_request(
            "POST",
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            f"Gemini {model}",
            timeout=LLM_TIMEOUT,
            headers={
                "x-goog-api-key": GEMINI_API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "system_instruction": {"parts": [{"text": system_prompt}]},
                "contents": [{"role": "user", "parts": [{"text": user_message}]}],
                # Để dư token vì model Flash có "suy nghĩ" nội bộ, tốn thêm token đầu ra
                "generationConfig": {"temperature": 0.3, "maxOutputTokens": 8192},
            },
        )
        if resp is None:
            return None
        if resp.status_code != 200:
            log.error("[Gemini %s] HTTP %s: %s", model, resp.status_code, resp.text[:500])
            return None

        data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            log.error("[Gemini %s] Không có kết quả trả về: %s", model, str(data)[:500])
            return None
        parts = candidates[0].get("content", {}).get("parts", [])
        # Bỏ qua phần "thought" (nếu có), chỉ lấy câu trả lời
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if text.strip():
            log.info("[Gemini] Model %s đã trả kết quả.", model)
        return text.strip() or None

    except (ValueError, KeyError, IndexError, TypeError) as e:
        log.error("[Gemini %s] Phản hồi không đúng định dạng: %s", model, e)
        return None
    except Exception:
        log.exception("[Gemini %s] Lỗi không mong muốn", model)
        return None


def clean_ai_output(text):
    """
    Dọn dẹp output của AI phòng khi AI không tuân thủ định dạng:
    - Bỏ khối ```...``` bao ngoài
    - Đổi **đậm** (Markdown) thành <b>đậm</b> (HTML)
    """
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text.strip())
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    return text.strip()


def analyze_with_ai(m15_text, h1_text, balance):
    """Chọn nhà cung cấp AI theo cấu hình và trả về tín hiệu đã làm sạch."""
    system_prompt = build_system_prompt(balance)
    user_message = build_user_message(m15_text, h1_text)
    if LLM_PROVIDER == "openai":
        result = call_openai(system_prompt, user_message)
    elif LLM_PROVIDER == "anthropic":
        result = call_anthropic(system_prompt, user_message)
    else:
        result = call_gemini(system_prompt, user_message)
    return clean_ai_output(result) if result else None


def is_wait_signal(text):
    """Kiểm tra tín hiệu có phải WAIT không (dựa trên dòng 'TÍN HIỆU GIAO DỊCH')."""
    for line in text.splitlines():
        if "TÍN HIỆU GIAO DỊCH" in line.upper():
            return "WAIT" in line.upper()
    return False


# =============================================================================
# BƯỚC 3: GỬI TIN NHẮN TELEGRAM
# =============================================================================
def strip_html(text):
    """Bỏ thẻ HTML để gửi dạng text thường khi HTML bị lỗi parse."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def send_telegram(text):
    """
    Gửi tin nhắn qua Telegram Bot API với parse_mode=HTML.
    Nếu Telegram báo lỗi parse HTML (AI viết sai thẻ), tự gửi lại dạng text thường.
    Trả về True nếu gửi thành công.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    if len(text) > TELEGRAM_MAX_LEN:
        text = text[:TELEGRAM_MAX_LEN] + "\n..."

    try:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        resp = http_request("POST", url, "Telegram", json=payload)
        if resp is None:
            return False

        if resp.status_code == 200:
            log.info("[Telegram] Đã gửi tin nhắn thành công.")
            return True

        # Lỗi 400 "can't parse entities" -> HTML của AI bị sai, gửi lại text thường
        if resp.status_code == 400 and "parse" in resp.text.lower():
            log.warning("[Telegram] HTML lỗi, gửi lại dạng text thường. Chi tiết: %s", resp.text[:300])
            payload.pop("parse_mode")
            payload["text"] = strip_html(text)
            resp = http_request("POST", url, "Telegram", json=payload)
            if resp is not None and resp.status_code == 200:
                log.info("[Telegram] Đã gửi (dạng text thường).")
                return True

        log.error("[Telegram] Gửi thất bại: HTTP %s - %s",
                  getattr(resp, "status_code", "N/A"), getattr(resp, "text", "")[:300])
        return False

    except Exception:
        log.exception("[Telegram] Lỗi không mong muốn")
        return False


# =============================================================================
# CHỐT SỔ SỐ DƯ QUA TELEGRAM
# Số dư được lưu bằng cách GHIM tin nhắn xác nhận trong chat -> không cần database,
# GitHub Actions chạy lại từ đầu mỗi lần vẫn đọc được số dư qua getChat.
# =============================================================================
BALANCE_MARKER = "SỐ DƯ TÀI KHOẢN"
BALANCE_CMD = re.compile(r"^/(?:sodu|balance)(?:@\w+)?(?:\s+(.+))?$", re.IGNORECASE)


def tg_api(method, payload):
    """Gọi 1 phương thức Telegram Bot API. Trả về trường 'result' hoặc None nếu lỗi."""
    try:
        resp = http_request("POST", f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}",
                            f"Telegram {method}", json=payload)
        if resp is None:
            return None
        data = resp.json()
        if not data.get("ok"):
            log.error("[Telegram %s] Lỗi: %s", method, data.get("description"))
            return None
        return data.get("result")
    except ValueError:
        log.error("[Telegram %s] Phản hồi không phải JSON.", method)
        return None
    except Exception:
        log.exception("[Telegram %s] Lỗi không mong muốn", method)
        return None


def parse_amount(raw):
    """Đổi chuỗi người dùng gõ thành số: '95.5', '95,5', '1,250.75', '$120' -> float."""
    raw = raw.strip().replace("$", "").replace("USD", "").replace("usd", "").strip()
    if "," in raw and "." in raw:
        raw = raw.replace(",", "")          # 1,250.75 -> 1250.75
    else:
        raw = raw.replace(",", ".")         # 95,5 -> 95.5
    return float(raw)


def get_pinned_balance():
    """Đọc số dư từ tin nhắn đang được ghim trong chat. Trả về float hoặc None."""
    chat = tg_api("getChat", {"chat_id": TELEGRAM_CHAT_ID})
    text = ((chat or {}).get("pinned_message") or {}).get("text", "")
    m = re.search(BALANCE_MARKER + r"\D*?(\d+(?:\.\d+)?)", text)
    return float(m.group(1)) if m else None


def get_current_balance():
    """Số dư dùng để tính lot: ưu tiên số đã chốt sổ, nếu chưa có thì dùng ACCOUNT_BALANCE."""
    balance = get_pinned_balance()
    if balance is None:
        log.info("Chưa có số dư chốt sổ, dùng mặc định ACCOUNT_BALANCE = %g USD", ACCOUNT_BALANCE)
        return ACCOUNT_BALANCE
    log.info("Số dư theo chốt sổ gần nhất: %g USD", balance)
    return balance


def save_balance(balance):
    """Gửi tin xác nhận số dư mới và ghim lại để các lượt chạy sau đọc được."""
    now_vn = datetime.now(VN_TZ).strftime("%H:%M %d/%m/%Y")
    max_risk_usd = round(balance * MAX_RISK_PERCENT / 100, 2)
    text = (f"💰 <b>{BALANCE_MARKER}: {balance:g} USD</b>\n"
            f"🕒 Chốt sổ lúc: {now_vn} (GMT+7)\n"
            f"⚖️ Rủi ro tối đa mỗi lệnh: {MAX_RISK_PERCENT:g}% = {max_risk_usd:g} USD\n"
            f"<i>Tin nhắn này đang được ghim để bot ghi nhớ số dư. Đừng bỏ ghim.</i>")
    msg = tg_api("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"})
    if not msg:
        return False
    pinned = tg_api("pinChatMessage", {"chat_id": TELEGRAM_CHAT_ID,
                                       "message_id": msg["message_id"],
                                       "disable_notification": True})
    if pinned is None:
        send_telegram("⚠️ Không ghim được tin chốt sổ. Hãy gửi lại lệnh /sodu kèm số dư.")
        return False
    log.info("Đã chốt sổ số dư mới: %g USD", balance)
    return True


def process_balance_commands():
    """
    Đọc các tin nhắn mới gửi cho bot, xử lý lệnh /sodu.
    Chỉ nhận lệnh từ đúng TELEGRAM_CHAT_ID của bạn, người khác nhắn bot sẽ bị bỏ qua.
    """
    try:
        updates = tg_api("getUpdates", {"timeout": 0, "allowed_updates": ["message"]})
        if not updates:
            return

        new_balance, want_show, bad_input = None, False, None
        for u in updates:
            msg = u.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != str(TELEGRAM_CHAT_ID):
                continue
            m = BALANCE_CMD.match((msg.get("text") or "").strip())
            if not m:
                continue
            if m.group(1):
                try:
                    value = parse_amount(m.group(1))
                    if value <= 0:
                        raise ValueError
                    new_balance = value          # nhiều lệnh thì lấy lệnh mới nhất
                except ValueError:
                    bad_input = m.group(1)
            else:
                want_show = True

        # Báo Telegram đã xử lý xong các tin này để lần sau không đọc lại
        tg_api("getUpdates", {"offset": updates[-1]["update_id"] + 1, "timeout": 0})

        if new_balance is not None:
            save_balance(new_balance)
        elif bad_input is not None:
            send_telegram(f"⚠️ Không hiểu số dư <code>{html.escape(bad_input)}</code>. "
                          "Gửi đúng dạng: <code>/sodu 95.5</code>")
        elif want_show:
            balance = get_current_balance()
            send_telegram(f"💰 Số dư bot đang dùng để tính lot: <b>{balance:g} USD</b>\n"
                          "Cập nhật bằng lệnh: <code>/sodu 95.5</code>")
    except Exception:
        log.exception("Lỗi khi xử lý lệnh chốt sổ")


# =============================================================================
# BƯỚC 4: JOB CHÍNH - chạy mỗi 15 phút
# =============================================================================
last_analyzed_candle = None   # Lưu thời gian nến cuối đã phân tích để tránh trùng lặp


def job():
    """Một chu kỳ: lấy nến -> AI phân tích -> gửi Telegram."""
    global last_analyzed_candle
    try:
        log.info("===== Bắt đầu chu kỳ phân tích =====")

        # Xử lý lệnh chốt sổ /sodu (nếu có) rồi lấy số dư hiện tại
        process_balance_commands()
        balance = get_current_balance()

        # M15 là bắt buộc: không có thì bỏ qua chu kỳ
        m15 = fetch_candles("15min", CANDLE_COUNT, 15)
        if not m15:
            log.warning("Không có dữ liệu nến M15, bỏ qua chu kỳ này.")
            return

        # Nếu nến M15 cuối không đổi (cuối tuần / thị trường đóng cửa) thì không phân tích lại
        newest = m15[-1]["time"]
        if newest == last_analyzed_candle:
            log.info("Không có nến mới (thị trường có thể đang đóng cửa), bỏ qua.")
            return

        # H1 là phụ trợ: lỗi thì vẫn tiếp tục với M15, AI sẽ được báo là thiếu H1
        h1 = fetch_candles("1h", H1_CANDLE_COUNT, 60)
        if not h1:
            log.warning("Không lấy được nến H1, tiếp tục phân tích chỉ với M15.")

        m15_text = format_candles(m15)
        h1_text = format_candles(h1) if h1 else None
        log.info("Dữ liệu M15 gửi AI:\n%s", m15_text)
        if h1:
            log.info("Kèm %d nến H1 (từ %s đến %s GMT+7)", len(h1),
                     h1[0]["time"].astimezone(VN_TZ).strftime("%d/%m %H:%M"),
                     h1[-1]["time"].astimezone(VN_TZ).strftime("%d/%m %H:%M"))

        signal = analyze_with_ai(m15_text, h1_text, balance)
        if not signal:
            log.warning("AI không trả về kết quả, sẽ thử lại ở chu kỳ sau.")
            return

        if not SEND_WAIT_SIGNALS and is_wait_signal(signal):
            log.info("Tín hiệu WAIT, không gửi Telegram (SEND_WAIT_SIGNALS=false).")
            last_analyzed_candle = newest
            return

        if send_telegram(signal):
            last_analyzed_candle = newest

    except Exception:
        # Lưới an toàn cuối cùng: mọi lỗi đều chỉ ghi log, không làm dừng bot
        log.exception("Lỗi không mong muốn trong job()")


# =============================================================================
# KHỞI ĐỘNG
# =============================================================================
def validate_config():
    """Kiểm tra các biến môi trường bắt buộc trước khi chạy."""
    missing = []
    if not TWELVE_DATA_API_KEY:
        missing.append("TWELVE_DATA_API_KEY")
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if LLM_PROVIDER == "openai" and not OPENAI_API_KEY:
        missing.append("OPENAI_API_KEY")
    elif LLM_PROVIDER == "anthropic" and not ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    elif LLM_PROVIDER not in ("openai", "anthropic") and not GEMINI_API_KEY:
        missing.append("GEMINI_API_KEY")

    if missing:
        log.critical("Thiếu biến môi trường: %s. Hãy khai báo trong file .env", ", ".join(missing))
        sys.exit(1)


def main():
    validate_config()
    model = {"openai": OPENAI_MODEL, "anthropic": ANTHROPIC_MODEL}.get(LLM_PROVIDER, GEMINI_MODEL)
    log.info("Bot XAUUSD khởi động | AI: %s (%s) | Nến: %d x M15 + %d x H1 | Rủi ro tối đa %g%%/lệnh",
             LLM_PROVIDER, model, CANDLE_COUNT, H1_CANDLE_COUNT, MAX_RISK_PERCENT)

    # Chế độ GitHub Actions: chạy 1 lần rồi thoát, lịch do GitHub quản lý
    if RUN_ONCE:
        job()
        return

    # Chạy vào giây thứ 20 sau mỗi mốc :00, :15, :30, :45
    # -> nến M15 vừa đóng, Twelve Data đã kịp cập nhật
    for minute in ("00", "15", "30", "45"):
        schedule.every().hour.at(f"{minute}:20").do(job)

    job()  # Chạy ngay một lần khi khởi động để kiểm tra cấu hình

    while True:
        try:
            schedule.run_pending()
        except Exception:
            log.exception("Lỗi trong scheduler")
        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Bot đã dừng theo yêu cầu.")
