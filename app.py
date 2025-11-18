import os
import tempfile
import traceback
import requests
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from flask import Flask
import telebot
from groq import Groq
from deep_translator import GoogleTranslator

# ============================================
# CONFIG / ENV
# ============================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("BOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
MAX_VIDEO_SECONDS = int(os.environ.get("MAX_VIDEO_SECONDS", "300"))
WORKERS = int(os.environ.get("WORKERS", "1"))

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN לא מוגדר")
if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY לא מוגדר")

# ============================================
# SETUP
# ============================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("vidtransbot")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = Groq(api_key=GROQ_API_KEY)
translator = GoogleTranslator(source="auto", target="iw")

app = Flask(__name__)

executor = ThreadPoolExecutor(max_workers=WORKERS)
job_semaphore = threading.Semaphore(WORKERS)

@app.route("/")
def home():
    return "Telegram Hebrew Subtitle Bot — Running ✅"

# ============================================
# Helpers: time formatting
# ============================================
def seconds_to_ass_time(sec_float):
    h = int(sec_float // 3600)
    m = int((sec_float % 3600) // 60)
    s = int(sec_float % 60)
    cs = int((sec_float - int(sec_float)) * 100)
    return f"{h}:{m:02}:{s:02}.{cs:02}"

# ============================================
# RTL text wrapper (Hebrew fix)
# ============================================
def shape_for_ass(text):
    """
    Hebrew does NOT need reshaping. Just force RTL direction.
    """
    return r"{\rtl}" + text.replace("\n", "\\N")

# ============================================
# Make ASS subtitle file
# ============================================
def make_ass_file(segments, fonts_dir=None, font_name="NotoSansHebrew"):
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1280\n"
        "PlayResY: 720\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n\n"
        
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        
        # Alignment 2 = center-bottom. Outline=3 Shadow=1
        f"Style: Default,{font_name},42,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,"
        "0,0,0,0,100,100,0,0,1,3,1,2,20,20,35,1\n\n"
        
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".ass", mode="w", encoding="utf-8")
    tmp.write(header)

    for seg in segments:
        start = seconds_to_ass_time(seg["start"])
        end = seconds_to_ass_time(seg["end"])
        text_shaped = shape_for_ass(seg["text"])
        line = f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text_shaped}\n"
        tmp.write(line)

    tmp.close()
    return tmp.name

# ============================================
# Burn to video using ffmpeg
# ============================================
def burn_ass_with_ffmpeg(input_path, ass_path, output_path, fonts_dir=None):
    filter_str = f"ass='{ass_path}'"
    if fonts_dir:
        filter_str = f"ass='{ass_path}':fontsdir='{fonts_dir}'"

    cmd = (
        f"ffmpeg -y -nostdin -i \"{input_path}\" -vf \"{filter_str}\" "
        f"-c:v libx264 -preset veryfast -crf 23 -c:a copy \"{output_path}\""
    )
    logger.info("Running ffmpeg command: %s", cmd)
    res = os.system(cmd)
    if res != 0:
        raise RuntimeError(f"ffmpeg failed with code {res}")

# ============================================
# Batch translate
# ============================================
def batch_translate_texts(texts):
    if not texts:
        return []
    delimiter = "\n<<<SPLIT>>> \n"
    big = delimiter.join(texts)
    out = translator.translate(big)
    parts = [p.strip() for p in out.split(delimiter)]
    if len(parts) != len(texts):
        parts = [translator.translate(t) for t in texts]
    return parts

# ============================================
# Main processing job
# ============================================
def process_video_job(chat_id, input_bytes, filename_hint="video.mp4"):
    try:
        logger.info("Acquired job for chat %s", chat_id)

        # Save input video
        in_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(filename_hint)[1])
        in_tmp.write(input_bytes)
        in_tmp.close()
        input_path = in_tmp.name

        bot.send_message(chat_id, "🎧 מפענח אודיו (Whisper over Groq)...")

        resp = client.audio.transcriptions.create(
            model="whisper-large-v3-turbo",
            file=open(input_path, "rb"),
            response_format="verbose_json"
        )

        segments = resp.segments
        simple_segments, texts = [], []

        for s in segments:
            if "start" in s and "end" in s and s.get("text"):
                t = s["text"].strip()
                if t:
                    simple_segments.append({
                        "start": float(s["start"]),
                        "end": float(s["end"]),
                        "text": t
                    })
                    texts.append(t)

        if not simple_segments:
            bot.send_message(chat_id, "❌ לא אותרו דיבורים בסרטון.")
            return

        # Duration limit
        total_duration = max(s["end"] for s in simple_segments)
        if total_duration > MAX_VIDEO_SECONDS:
            bot.send_message(chat_id, f"❌ הסרטון ארוך מ-{MAX_VIDEO_SECONDS} שניות.")
            return

        bot.send_message(chat_id, "🌍 מתרגם שורות...")
        translated = batch_translate_texts(texts)

        # Attach translations
        for i, seg in enumerate(simple_segments):
            seg["text"] = translated[i]

        bot.send_message(chat_id, "📝 יוצר קובץ כתוביות...")
        fonts_dir = "fonts" if os.path.isdir("fonts") else None
        ass_path = make_ass_file(simple_segments, fonts_dir=fonts_dir, font_name="NotoSansHebrew")

        bot.send_message(chat_id, "🔥 שורף כתוביות לתוך הסרטון...")
        out_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        out_tmp.close()
        output_path = out_tmp.name

        burn_ass_with_ffmpeg(input_path, ass_path, output_path, fonts_dir)

        bot.send_message(chat_id, "📤 מעלה את הסרטון...")
        with open(output_path, "rb") as f:
            bot.send_video(chat_id, f, caption="✅ הנה הסרטון עם כתוביות בעברית!")

        # Cleanup
        for p in (input_path, ass_path, output_path):
            try:
                os.remove(p)
            except:
                pass

    except Exception as e:
        logger.exception("Error: %s", e)
        bot.send_message(chat_id, f"❌ שגיאה בעיבוד הסרטון: {e}")

    finally:
        try:
            job_semaphore.release()
        except:
            pass

# ============================================
# Telegram handlers
# ============================================
@bot.message_handler(commands=["start"])
def cmd_start(msg):
    bot.reply_to(msg, "🎬 שלח סרטון עד 5 דקות ואחזיר אותו עם כתוביות בעברית — מסודרות וימניות!")

@bot.message_handler(content_types=["video"])
def handle_video(message):
    chat = message.chat.id
    try:
        if not job_semaphore.acquire(blocking=False):
            bot.send_message(chat, "⏳ יש עומס — נסה שוב בעוד כמה רגעים.")
            return

        bot.send_message(chat, "📥 מוריד את הסרטון...")
        file_info = bot.get_file(message.video.file_id)
        url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_info.file_path}"
        data = requests.get(url).content

        executor.submit(process_video_job, chat, data, message.video.file_name or "video.mp4")

    except Exception as e:
        logger.exception(e)
        bot.send_message(chat, f"❌ שגיאה: {e}\n{traceback.format_exc()}")

# ============================================
# Runner
# ============================================
def run_bot():
    logger.info("Starting bot polling...")
    bot.infinity_polling(timeout=60, long_polling_timeout=60)

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
