"""إملاء عربي محلي: نقرة واحدة على الميكروفون أو Ctrl+Alt+F9، ثم يُكتب الكلام عند كل وقفة في النافذة النشطة.

التفريغ بـ faster-whisper على كرت الشاشة إن اتسعت ذاكرته بجانب نموذج LM Studio (نحو 1.1 GB)، وإلا على المعالج؛ لا يرسل الصوت خارج الجهاز.
النافذة لا تأخذ التركيز، فيبقى المؤشر في البرنامج الذي تكتب فيه، والنص يُكتب بضغطات مفاتيح (SendInput) لا بالحافظة. الأوامر (بالعربية أو الإنجليزية أو نطقها العربي) تُقال وحدها بعد وقفة فتُنفَّذ ولا تُكتب؛ قائمتها في COMMANDS. «اقرأ» ينطق النص المحدد بصوت كريم (Piper محلي في models/piper) أو صوت ويندوز نايف (speak.ps1)؛ «صوت كريم»/«صوت نايف» يبدّل. الميكروفون يُكتم أثناء النطق.
«اتبع بايونيك» يراقب قاعدة جلسات Bionic وينطق ردوده وخطواته وأخطاءه، و«وافق»/«ارفض»/«أوقف بايونيك» تُرسل إلى نافذته. «محادثة» ينقل إلى وضع الحوار: كلامك يذهب إلى Qwen في LM Studio ويُنطق رده جملةً جملة ويُكتب مع سؤالك في النافذة النشطة، والحوار يُحفظ في chat.log؛ «إملاء» يعيد الكتابة. مصطلحاتك في terms.txt. التشغيل: start.cmd. الاختبار: python dictate.pyw --file ملف.wav
"""
import atexit
import ctypes
import json
import logging
import urllib.request
import math
import os
import pathlib
import queue
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import wave
import winreg
import winsound
from ctypes import wintypes

MODEL = "large-v3-turbo"
LANGUAGE = "ar"
RATE = 16000
BLOCK = 1600                # 0.1 ثانية
PAUSE_SECONDS = 0.6         # وقفة تنهي الجملة
MAX_SEGMENT = 25            # قطع إجباري للكلام المتصل
IDLE_STOP = 45              # إغلاق الميكروفون بعد صمت طويل
LEAD_BLOCKS = 5             # ما يُبقى قبل بداية الكلام
MIN_VOICED = 3              # أقل كتل صوتية تُعد كلامًا
SPEECH_RATIO, SPEECH_FLOOR, NOISE_RISE = 3.0, 0.001, 1.01
MMDEVICES = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio"
EP_DESC, EP_IFACE = "{a45c254e-df1c-4efd-8020-67d146a850e0},2", "{b3f8fa53-0004-438e-9003-51a46e139bfc},6"
BT_MIC = re.compile("hands-free|headset|earbuds|bluetooth|airpods|buds", re.IGNORECASE)      # ميكروفون بلوتوث: ضيق النطاق ويُحوّل السماعة إلى وضع مكالمة
BUILTIN_MIC = re.compile("microphone array|microphone \\(realtek|realtek", re.IGNORECASE)
NOT_A_MIC = re.compile("stereo mix|what u hear|loopback|mixage|wave out", re.IGNORECASE)
IS_A_MIC = re.compile("microphone|mic\b|ميكروفون|headset", re.IGNORECASE)
HOTKEY_ID = 1
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x1, 0x2, 0x4000
VK_F9, VK_F10, VK_F11, VK_CONTROL, VK_MENU = 0x78, 0x79, 0x7A, 0x11, 0x12      # Ctrl+Alt+F9 التسجيل، F10 التبديل إملاء/محادثة، F11 تصغير/توسيع
VK_RETURN, VK_SHIFT, VK_BACK, VK_DELETE, VK_TAB, VK_ESCAPE, VK_END, VK_HOME = 0x0D, 0x10, 0x08, 0x2E, 0x09, 0x1B, 0x23, 0x24
VK_A, VK_C, VK_S, VK_V, VK_X, VK_Y, VK_Z = 0x41, 0x43, 0x53, 0x56, 0x58, 0x59, 0x5A
WM_HOTKEY = 0x0312
INPUT_KEYBOARD, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 1, 0x2, 0x4
TYPE_CHUNK = 64
COMBINING = re.compile("[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed]")
MIN_LOGPROB, MAX_NO_SPEECH, COMMAND_LOGPROB, COMMAND_NO_SPEECH = -1.2, 0.6, -0.5, 0.2
COMMAND_LOGPROB_EXACT = -1.1    # المطابقة التامة دليل بذاتها؛ الإنجليزية تأخذ ثقة منخفضة من نموذج مثبّت على العربية
RETRY_SECONDS, RETRY_LOGPROB = 3.5, -0.7    # مقطع قصير مشكوك فيه أو فارغ يُعاد تفريغه بالإنجليزية بحثًا عن أمر فقط
CONTEXT_CHARS = 160         # آخر ما كُتب يُمرَّر للنموذج ليفهم السياق
GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = -20, 0x80, 0x08000000
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
HWND_TOPMOST, SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = -1, 0x2, 0x1, 0x10
TOPMOST_EVERY = 20          # إعادة التثبيت في المقدمة كل 20 دورة (ثانية)
WEAK_PEAK, TARGET_PEAK, MAX_GAIN = 0.02, 0.7, 20
DWMWA_CORNER, DWMWCP_ROUND = 33, 2
PROMPT = "إملاء باللغة العربية الفصحى مع علامات الترقيم، مثل الفاصلة والنقطة وعلامة الاستفهام؟ "
DEFAULT_TERMS = ("المرصد", "بايونيك", "كرت الشاشة", "المعالج", "الذاكرة", "القرص", "النموذج")
# الأمر: (الفعل، معطاه، صيغ النطق). الفعل key يضغط (مفتاح، معدِّل)، وtype يكتب العلامة، والبقية داخلية.
COMMANDS = (
    ("send", ("key", (VK_RETURN, None)), ("ارسل", "ارسال", "ادخل", "send", "سند", "enter", "انتر")),
    ("line", ("key", (VK_RETURN, VK_SHIFT)), ("سطر جديد", "new line", "نيو لاين")),
    ("erase", ("erase", None), ("امسح", "مسح", "erase", "ايريز")),
    ("stop", ("stop", None), ("توقف", "قف", "stop", "ستوب")),
    ("select_all", ("key", (VK_A, VK_CONTROL)), ("حدد الكل", "تحديد الكل", "اختر الكل", "اختار الكل", "select all", "سيلكت اول", "سلكت اول", "سيليكت اول")),
    ("delete", ("key", (VK_DELETE, None)), ("احذف", "حذف", "delete", "ديليت", "دليت")),
    ("delete_word", ("key", (VK_BACK, VK_CONTROL)), ("احذف كلمة", "حذف كلمة", "delete word", "ديليت ورد")),
    ("undo", ("key", (VK_Z, VK_CONTROL)), ("تراجع", "undo", "اندو")),
    ("redo", ("key", (VK_Y, VK_CONTROL)), ("اعد", "اعادة", "redo", "ريدو")),
    ("copy", ("key", (VK_C, VK_CONTROL)), ("انسخ", "نسخ", "copy", "كوبي")),
    ("cut", ("key", (VK_X, VK_CONTROL)), ("قص", "cut", "كت")),
    ("paste", ("key", (VK_V, VK_CONTROL)), ("الصق", "لصق", "paste", "بيست")),
    ("tab", ("key", (VK_TAB, None)), ("tab", "تاب")),
    ("escape", ("key", (VK_ESCAPE, None)), ("الغاء", "escape", "اسكيب")),
    ("end", ("key", (VK_END, VK_CONTROL)), ("النهاية", "نهاية", "end", "اند")),
    ("home", ("key", (VK_HOME, VK_CONTROL)), ("البداية", "بداية", "beginning", "home", "هوم")),
    ("save", ("key", (VK_S, VK_CONTROL)), ("احفظ", "حفظ", "save", "سيف")),
    ("comma", ("type", "،"), ("فاصلة", "comma", "كوما")),
    ("period", ("type", "."), ("نقطة", "period", "بيريود")),
    ("colon", ("type", ":"), ("نقطتان", "colon", "كولون")),
    ("dash", ("type", "-"), ("شرطة", "dash", "داش")),
    ("open", ("type", "("), ("قوس فتح", "فتح قوس", "افتح قوس", "open paren", "اوبن")),
    ("close", ("type", ")"), ("قوس اغلاق", "اغلاق قوس", "اغلق قوس", "close paren", "كلوز")),
    ("question", ("type", "؟"), ("علامة استفهام", "question mark")),
    ("exclaim", ("type", "!"), ("علامة تعجب", "exclamation")),
)
FUZZY_MAX_WORDS, FUZZY_MIN_LEN = 3, 4
CHAT_URL, CHAT_MODEL, CHAT_TURNS, CHAT_GAP, CHAT_TIMEOUT = "http://127.0.0.1:1234/v1/chat/completions", "qwen/qwen3-8b", 12, 1.3, 120
CHAT_SYSTEM = ("اسمك سلمى، مساعدة صوتية محلية تحاور المالك بالعربية الفصحى؛ إن سُئلت عن اسمك فأنت سلمى. أجيبي بجمل قصيرة واضحة تُنطق بصوت، من جملة إلى أربع، "
               "نثرًا متصلًا في فقرة واحدة كما يتكلم الناس. ممنوع منعًا باتًا: القوائم، والنقاط، والترقيم (1، 2، -، •)، والعناوين، والرموز، والنجوم، "
               "والأسطر المنفصلة؛ اربطي الأفكار بحروف العطف. الأرقام غربية 0-9. إن كان السؤال عصفًا ذهنيًا فاطرحي فكرة واحدة ثم سؤالًا يدفع التفكير.")
LIST_MARK = re.compile(r"(?m)^[ \t]*(?:[-•*–—●▪►]+|\(?\d{1,2}[.)\-:]?|[أ-ي][.)]|#+)[ \t]+")  # علامات القوائم والترقيم والعناوين في أول السطر (ورقم عارٍ كما يفعل Qwen)
MARK_ONLY = re.compile(r"[\s\d.)(\-•*–—●▪►:#]*")
LINE_BREAKS = re.compile(r"[ \t]*\n+[ \t]*")


def prose(text):
    """رد نثري: يحذف علامات القوائم والترقيم من أوائل الأسطر ويصل الأسطر بمسافة (المالك 7/11: الترقيم مزعج، سرد بلا نقاط)،
    ويجرّد تشكيل النموذج (Qwen يشكّل أحيانًا تشكيلًا خاطئًا مثل «بَارَكاتِهِ» فيتجاوز المشكّلَ الأدقَّ لو تُرك)."""
    return HARAKAT.sub("", LINE_BREAKS.sub(" ", LIST_MARK.sub("", text)))
THINK = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
CHAT_WRITE, ME_LABEL, BOT_LABEL = True, "أنا: ", "سلمى: "
COMMANDS += (
    ("chat", ("mode", "chat"), ("محادثة", "وضع المحادثة", "محاذا", "محاذة", "محادسة", "chat", "chat mode", "تشات")),
    ("dictate", ("mode", "dictate"), ("املاء", "وضع الاملاء", "dictation", "dictate")),
    ("new_chat", ("newchat", None), ("محادثة جديدة", "new chat", "new conversation")),
    ("read", ("read", None), ("اقرا", "اقرا النص", "read", "ريد")),
    ("hush", ("hush", None), ("اسكت", "صمت", "quiet", "hush", "stop reading")),
)
TASHKEEL_MODEL = "rawi-ensemble"      # text2tashkeel: ONNX على المعالج، ~0.01 ث للجملة؛ كريم دُرّب على نص مشكّل فيقرأ غير المشكّل بحركات خاطئة
DIGITS = re.compile(r"\d+(?:[.,]\d+)?")
SENTENCE_SPLIT = re.compile(r"(?<=[.!؟?:؛;])\s+|\n+")
MAX_SPEAK_CHARS = 4000
SND_ASYNC, SND_FILENAME, SND_PURGE = 0x1, 0x20000, 0x40
SPEED_DEFAULT, SPEED_STEP, SPEED_MIN, SPEED_MAX = 1.35, 0.15, 0.8, 2.0   # مضاعف سرعة النطق
COMMANDS += (
    ("voice_kareem", ("voice", "kareem"), ("صوت كريم", "كريم")),
    ("voice_naayf", ("voice", "naayf"), ("صوت نايف", "نايف")),
    ("voice_silma", ("voice", "silma"), ("صوت سلمى", "سلمى", "صوت سيلما", "سيلما")),
    ("lexicon", ("lexicon", None), ("أعد قراءة المعجم", "اعد قراءة المعجم", "حدث المعجم", "حدّث المعجم")),
    ("faster", ("speed", SPEED_STEP), ("اسرع", "بسرعة", "faster", "speed up")),
    ("slower", ("speed", -SPEED_STEP), ("ابطا", "ببطء", "slower", "slow down")),
    ("follow", ("follow", True), ("اتبع بايونيك", "تابع بايونيك", "راقب بايونيك", "follow bionic")),
    ("unfollow", ("follow", False), ("توقف عن المتابعة", "اترك بايونيك", "stop following")),
    ("approve", ("bionic", "نعم، نفّذ."), ("وافق", "موافق", "نعم نفذ", "approve")),
    ("reject", ("bionic", "لا، توقف."), ("ارفض", "لا توقف", "reject")),
    ("halt", ("bionic", None), ("اوقف بايونيك", "stop bionic")),
    ("status", ("status", None), ("ماذا تفعل الان", "ماذا يفعل", "what are you doing")),
    ("repeat", ("repeat", None), ("كرر", "اعد القراءة", "repeat")),
)
BIONIC_ROOT = pathlib.Path.home() / ".lmstudio" / "apps" / "bionic"
BIONIC_POLL, BIONIC_MAX_CHARS, BIONIC_TITLE = 1.0, 700, "Bionic"
MARKDOWN = re.compile(r"```.*?```|`([^`]*)`|!\[[^\]]*\]\([^)]*\)|\[([^\]]*)\]\([^)]*\)|^\s{0,3}#{1,6}\s*|^\s*[-*+]\s+|^\s*\d+\.\s+|\*\*|__|~~|^\s*>\s?|\|", re.MULTILINE | re.DOTALL)
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]")
ERRORS_AR = (("disconnected", "انقطع الاتصال بـ LM Studio"), ("not retried", ""), ("context", "تجاوز السياق"), ("timeout", "انتهت المهلة"))
CF_UNICODETEXT = 13
PHANTOM = re.compile("اشتركوا في القناة|ترجمة نانسي|للمشاهدة")
LATIN, ARABIC_LETTER = re.compile("[A-Za-zÀ-ɏ]"), re.compile("[ء-ي]")
REPEATED = re.compile(r"(\S+)(?:\s+){3,}")      # كلمة تتكرر 4 مرات فأكثر: هلوسة متكررة


def hallucinated(text):
    """أنماط هلوسة Whisper مع صوت مشوّه أو مُشبع: لاتينية في وضع عربي، نص مشكّل بالكامل (Whisper لا يشكّل عادة)، تكرار كلمة."""
    latin, arabic = len(LATIN.findall(text)), len(ARABIC_LETTER.findall(text))
    if latin > arabic and latin >= 4:
        return "لاتينية"
    if len(COMBINING.findall(text)) >= max(4, arabic // 3):
        return "مشكّلة"
    if REPEATED.search(text):
        return "تكرار"
    return ""
SPOKEN = ((re.compile(r"\s*علامة استفهام[.،؟?]?"), "؟"), (re.compile(r"\s*علامة تعجب[.،!]?"), "!"))
EASTERN_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
BG, IDLE, LISTEN, WORK, INK, DIM = "#202124", "#3c4043", "#d93025", "#b06000", "#ffffff", "#9aa0a6"
CHAT = "#1a73e8"                                                   # لون مفتاح الوضع حين تكون المحادثة فعّالة
SWITCH_W, SWITCH_H = 110, 24                                       # مفتاح «إملاء | محادثة»
CARD, WAVE_Y, WAVE_W, WAVE_H, BARS = 200, 98, 184, 96, 1             # البطاقة المربعة، مركز السحابة وعرضها وارتفاعها؛ قيمة انتشار واحدة
SCALE_MIN, SCALE_MAX = 0.7, 2.5                                      # حدود تكبير البطاقة بالمقبض
MIC_D = 26                                                           # قطر زر الميكروفون في الرأس
COMPACT_W, COMPACT_H = 150, 38                                       # الكبسولة المصغّرة
FADE_AFTER, FADE_ALPHA = 10.0, 0.35                                  # التلاشي بعد ثوانٍ من السكون، وشفافيته
FONT_DIR, FR_PRIVATE = pathlib.Path(__file__).resolve().parent / "fonts", 0x10


def load_fonts():
    """يحمّل خط Cairo (OFL) من مجلد الأداة لهذه العملية وحدها دون تثبيت على النظام؛ وإلا Segoe UI."""
    loaded = 0
    for name in ("Cairo-Regular.ttf", "Cairo-Bold.ttf"):
        path = FONT_DIR / name
        if path.exists():
            loaded += bool(ctypes.windll.gdi32.AddFontResourceExW(str(path), FR_PRIVATE, 0))
    return "Cairo" if loaded else "Segoe UI"


FONT = load_fonts()
ENVELOPE_STEP = 0.02                                                 # ثانية لكل نقطة في غلاف صوت الوكيل
OUTPUT_LATENCY, OUTPUT_LATENCY_BT = 0.06, 0.22                       # تأخير الإخراج من PlaySound حتى الأذن: سماعات الجهاز، وبلوتوث A2DP
BLUETOOTH_OUT = re.compile(r"earbuds|headphones|hands-free|headset|bluetooth|bthenum|airpods|buds", re.IGNORECASE)


def envelope(data, rate, width, channels):
    """غلاف ملف النطق: جذر متوسط المربعات لكل 50 مللي ثانية مقيسًا على الذروة (0-1) بمنحنى إدراكي؛ لعينات 16 بت فقط."""
    if width != 2 or not data:
        return []
    import numpy
    samples = numpy.frombuffer(data[: len(data) - len(data) % (2 * channels)], dtype="<i2").astype(numpy.float32)
    step = max(1, int(rate * ENVELOPE_STEP)) * channels
    frames = len(samples) // step
    if not frames:
        return []
    rms = numpy.sqrt((samples[: frames * step].reshape(frames, step) ** 2).mean(axis=1))
    peak = float(rms.max()) or 1.0
    return [min(1.0, float(v) / peak) ** 0.6 for v in rms]


def rounded_points(x0, y0, x1, y1, r):
    """نقاط مضلّع ناعم يرسم مستطيلًا بزوايا مستديرة في tkinter."""
    return [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]


def rounded(canvas, x0, y0, x1, y1, r, **kw):
    return canvas.create_polygon(*rounded_points(x0, y0, x1, y1, r), smooth=True, outline="", **kw)

HERE = pathlib.Path(__file__).resolve().parent
LMSTUDIO_VENDOR = pathlib.Path.home() / ".lmstudio" / "extensions" / "backends" / "vendor"
logging.basicConfig(filename=HERE / "dictate.log", level=logging.INFO, encoding="utf-8", format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("faster_whisper").setLevel(logging.WARNING)
TERMS_FILE, PLACE_FILE, CHAT_LOG = HERE / "terms.txt", HERE / "place.json", HERE / "chat.log"
SAY_FILE = HERE / "say.txt"                   # نص يوضع في هذا الملف يُنطق فورًا ثم يُحذف الملف؛ للاختبار والأتمتة
LEXICON_FILE = HERE / "tashkeel.txt"          # معجم تصحيح التشكيل: «كلمة كلمةٌ مشكّلة» في كل سطر؛ يصحح ما يخطئ فيه المشكّل الآلي (جَمَل/جُمَل)
PIPER_VOICE, VOICE_FILE = HERE / "models" / "piper" / "ar_JO-kareem-medium.onnx", HERE / "voice.txt"   # الصوت: «silma» أو «kareem» أو «naayf»
SILMA_DIR = HERE.parent / "SilmaTTS"          # خادم SILMA TTS على كرت الشاشة في بيئته الخاصة (torch)؛ كريم بديل تلقائي
SILMA_PYTHON, SILMA_SERVER, SILMA_PID = SILMA_DIR / ".venv" / "Scripts" / "python.exe", SILMA_DIR / "server.py", SILMA_DIR / "server.pid"
SILMA_START_TIMEOUT, SILMA_TIMEOUT = 180, 25   # ثوانٍ: للتحميل والإحماء، ولكل جملة
VOICE_NAMES = {"kareem": "كريم", "naayf": "نايف", "silma": "سلمى"}

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
user32.SendInput.restype = wintypes.UINT


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


class INPUT(ctypes.Structure):
    class _Union(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("padding", ctypes.c_byte * 32)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _Union)]


def cuda_dirs():
    """مجلدات cublas/cudnn: حزم nvidia في البيئة الافتراضية أولًا، ثم أحدث مورّد CUDA 12 في LM Studio."""
    found = []
    for name in ("cublas", "cudnn"):
        found += [d for d in pathlib.Path(sys.prefix).glob("Lib/site-packages/nvidia/" + name + "/bin") if d.is_dir()]
    if LMSTUDIO_VENDOR.is_dir():
        found += sorted(d for d in LMSTUDIO_VENDOR.glob("win-llama-cuda12-vendor-*") if d.is_dir())[-1:]
    return found


def load_model(cpu_only=False):
    """كرت الشاشة أولًا (نحو 0.3 ثانية للمقطع)، ثم المعالج (نحو 6 ثوانٍ) إن لم تتسع الذاكرة أو غابت مكتبات CUDA."""
    from faster_whisper import WhisperModel
    dirs = [] if cpu_only else cuda_dirs()
    for d in dirs:
        os.add_dll_directory(str(d))
        os.environ["PATH"] = str(d) + os.pathsep + os.environ["PATH"]
    if dirs:
        try:
            model = WhisperModel(MODEL, device="cuda", compute_type="int8_float16", local_files_only=True)
            model.device = "cuda"
            return model
        except (RuntimeError, ValueError, OSError) as e:
            logging.warning("كرت الشاشة غير متاح للإملاء: %s", str(e)[:200])
    model = WhisperModel(MODEL, device="cpu", compute_type="int8", cpu_threads=max(4, (os.cpu_count() or 8) - 6), local_files_only=True)
    model.device = "cpu"
    return model


def load_prompt():
    """مصطلحات المالك تحسن تمييز الكلمات؛ سطر لكل مصطلح."""
    if not TERMS_FILE.exists():
        TERMS_FILE.write_text("\n".join(DEFAULT_TERMS) + "\n", encoding="utf-8")
    terms = [t.strip() for t in TERMS_FILE.read_text(encoding="utf-8").splitlines() if t.strip()]
    return PROMPT + "، ".join(terms) + "."


def transcribe(model, audio, prompt=None, language=LANGUAGE):
    """النص مع أدنى ثقة (avg_logprob) وأعلى احتمال «لا كلام» بين مقاطعه؛ بهما تُرفض الهلوسة."""
    segments, _ = model.transcribe(audio, language=language, beam_size=5, vad_filter=True, condition_on_previous_text=False, without_timestamps=True, initial_prompt=prompt)
    parts = [(s.text.strip(), s.avg_logprob, s.no_speech_prob) for s in segments]
    if not parts:
        return "", 0.0, 1.0
    return " ".join(t for t, _, _ in parts if t).strip(), min(lp for _, lp, _ in parts), max(ns for _, _, ns in parts)


def system_default_name(flow):
    """اسم الجهاز الافتراضي للنظام (Render/Capture) من سجل MMDevices: أحدث طابع Level:0 بين الأجهزة النشطة.
    PortAudio داخل العملية يرى «الافتراضي» الذي ثبّته ويندوز لهذا التطبيق في خلاط الصوت، لا افتراضي النظام؛ لهذا نقرأه من السجل."""
    best = (0, "")
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, MMDEVICES + "\\" + flow) as root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                with winreg.OpenKey(root, winreg.EnumKey(root, i)) as dev:
                    try:
                        if winreg.QueryValueEx(dev, "DeviceState")[0] != 1:
                            continue
                        level = winreg.QueryValueEx(dev, "Level:0")[0]      # REG_QWORD يصل عددًا، وقد يصل REG_BINARY
                        stamp = level if isinstance(level, int) else int.from_bytes(level, "little")
                        with winreg.OpenKey(dev, "Properties") as props:
                            name = "%s (%s)" % (winreg.QueryValueEx(props, EP_DESC)[0], winreg.QueryValueEx(props, EP_IFACE)[0])
                    except (FileNotFoundError, OSError, TypeError):
                        continue
                    if stamp > best[0]:
                        best = (stamp, name)
    except OSError:
        logging.exception("قراءة أجهزة الصوت من السجل")
    return best[1]


def pinned_elsewhere(sounddevice):
    """إن خالف افتراضي العملية (كما يراه PortAudio) افتراضي النظام (من السجل) فويندوز يثبّت هذا التطبيق على جهاز في خلاط الصوت؛ يعيد وصفًا أو فارغًا."""
    notes = []
    for kind, flow in (("input", "Capture"), ("output", "Render")):
        system = system_default_name(flow)
        mine = sounddevice.query_devices(kind=kind)["name"]
        if system and not system.startswith(mine[:20]):     # أسماء MME مقطوعة عند 31 حرفًا
            notes.append("%s: العملية «%s» والنظام «%s»" % (kind, mine, system))
    return "؛ ".join(notes)


PAUSE_END = re.compile(r"([\u0621-\u064A]+)([\u064B-\u0652]+)(?=\s*(?:[.!؟?:؛;،,]|$))")


HARAKAT = re.compile(r"[\u064B-\u0652\u0670]")
ALEFS = re.compile("[\u0623\u0625\u0622]")                   # \u0627\u0644\u0645\u0634\u0643\u0651\u0644 \u064A\u0628\u062F\u0651\u0644 \u0647\u0645\u0632\u0627\u062A \u0627\u0644\u0623\u0644\u0641 (\u0627\u0636\u063A\u0637\u2192\u0623\u064E\u0636\u0652\u063A\u064E\u0637\u0652)\u061B \u0627\u0644\u0645\u0642\u0627\u0631\u0646\u0629 \u0628\u0623\u0644\u0641 \u0645\u0648\u062D\u0651\u062F\u0629
PREFIXES = ("وال", "فال", "بال", "كال", "ولل", "ال", "لل", "و", "ف", "ب", "ل", "ك")
_lexicon, _lexicon_mtime = {}, [0.0]


def lexicon_mtime():
    try:
        return LEXICON_FILE.stat().st_mtime
    except OSError:
        return 0.0


def load_lexicon():
    """يقرأ tashkeel.txt ويعيد قاموس «بلا تشكيل → مشكّل»؛ السطور الفارغة و# تعليقات. يُعاد تلقائيًا حين يتغيّر الملف على القرص."""
    _lexicon.clear()
    _lexicon_mtime[0] = lexicon_mtime()
    try:
        for line in LEXICON_FILE.read_text(encoding="utf-8").splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and not line.lstrip().startswith("#"):
                _lexicon[ALEFS.sub("ا", HARAKAT.sub("", parts[0]))] = parts[1]
    except OSError:
        pass
    return _lexicon


def correct_word(word):
    """كلمة مشكّلة من المشكّل: إن كان جذعها (بعد نزع السابقة) في المعجم استُبدلت حركاته وبقيت حركة حرفه الأخير (الإعراب) كما قدّرها المشكّل."""
    bare = ALEFS.sub("ا", HARAKAT.sub("", word))
    for prefix in ("",) + PREFIXES:
        if bare.startswith(prefix) and bare[len(prefix):] in _lexicon:
            stem_bare, fixed = bare[len(prefix):], _lexicon[bare[len(prefix):]]
            if len(stem_bare) < 2:
                continue
            letters = [i for i, ch in enumerate(word) if not HARAKAT.match(ch)]           # مواضع الحروف في الكلمة المشكّلة
            stem_start = letters[len(prefix)]
            tail = word[letters[-1] + 1:]                                                   # حركة الحرف الأخير عند المشكّل
            fixed_letters = [i for i, ch in enumerate(fixed) if not HARAKAT.match(ch)]
            fixed_body = fixed[:fixed_letters[-1] + 1]                                       # المعجم بلا حركة حرفه الأخير
            fixed_tail = fixed[fixed_letters[-1] + 1:]
            if len(fixed_letters) > 1 and "\u0651" in word[stem_start + 1:letters[len(prefix) + 1]] and "\u0651" not in fixed[fixed_letters[0] + 1:fixed_letters[1]]:
                fixed = fixed[:fixed_letters[0] + 1] + "\u0651" + fixed[fixed_letters[0] + 1:]       # شدة الإدغام بعد «ال» الشمسية (الرَّقَم) تبقى
                fixed_letters = [i for i, ch in enumerate(fixed) if not HARAKAT.match(ch)]
                fixed_body, fixed_tail = fixed[:fixed_letters[-1] + 1], fixed[fixed_letters[-1] + 1:]
            if ALEFS.sub("ا", HARAKAT.sub("", fixed_body)) != stem_bare:                                    # المعجم يغيّر الحروف (ألصق/الصق): خذه كله
                return word[:stem_start] + fixed
            if "\u0652" in fixed_tail or not tail:                                           # المعجم يسكّن (أمر) أو المشكّل لم يُعرب: حركة المعجم
                return word[:stem_start] + fixed
            return word[:stem_start] + fixed_body + tail
    return word


def correct(text):
    if lexicon_mtime() != _lexicon_mtime[0]:                                              # المعجم عُدّل على القرص: أعد قراءته بلا أمر صوتي ولا إعادة تشغيل
        logging.info("معجم التصحيح تغيّر على القرص: %d كلمة", len(load_lexicon()))
    if not _lexicon:
        return text
    return re.sub(r"[\u0621-\u064A\u064B-\u0652\u0670]+", lambda m: correct_word(m.group()), text)


SHADDA_AFTER = re.compile("([\u064B-\u0650])\u0651")
PUNCT_DROP = re.compile(r"[\"'«»“”‘’()\[\]{}<>*#_~`|\\/^+=@$%&•·]")                     # علامات لا تُنطق ولا تفيد الوقف
PUNCT_PAUSE = re.compile(r"[;؛:]")                                                           # تُقرأ وقفة قصيرة
PUNCT_END = re.compile(r"[!¡]+")                                                              # نهاية جملة
PUNCT_DASH = re.compile(r"\s*[-–—]+\s*")
PUNCT_DOTS = re.compile(r"(?:\.\s*){2,}|…+")
PUNCT_REPEAT = re.compile(r"([.،؟])(?:\s*[.،؟!])+")
PAUSAL_ALL = False                                                                            # True: تسكين أواخر كل الكلمات (يخفي أخطاء الإعراب)


LETTERS = re.compile(r"[ء-ي]")


def dense_tashkeel(text):
    """صحيح إن كان النص مشكَّلًا تشكيلًا شبه كامل (حركة لكل حرفين على الأقل)؛ التشكيل الجزئي يُترك للمشكّل."""
    return len(HARAKAT.findall(text)) >= 0.5 * max(1, len(LETTERS.findall(text)))


def spoken_punct(text):
    """ترقيم صالح للنطق: نماذج النطق تتأثر بالعلامات الغريبة (نجوم، أقواس، شرطات، نقاط متتابعة) فتتلعثم أو تصمت؛
    تُبقى فقط النقطة والفاصلة وعلامة الاستفهام العربيتان، وتُحوَّل البقية إليها أو تُحذف."""
    text = PUNCT_DROP.sub(" ", text)
    text = PUNCT_DOTS.sub(". ", text)
    text = PUNCT_END.sub(".", text)
    text = PUNCT_PAUSE.sub("،", text)
    text = PUNCT_DASH.sub("، ", text)
    text = text.replace(",", "،").replace("?", "؟")
    text = PUNCT_REPEAT.sub(r"\1", text)
    text = re.sub(r"\s+([.،؟])", r"\1", text)
    text = re.sub(r"^[\s.،؟]+", "", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def shadda_first(text):
    """الشدة قبل حركتها: مشكّل rawi يكتب الحركة ثم الشدة (ترتيب يونيكود القانوني)، ونماذج النطق دُرّبت على الشدة ثم الحركة."""
    return SHADDA_AFTER.sub("\u0651\\1", text)


WORD_END = re.compile(r"([\u0621-\u064A]+)([\u064B-\u0652]+)(?=\s|$|[.!؟?:؛;،,])")


def pausal_all(text):
    """تسكين آخر كل كلمة (كما يقرأ كثيرون بلا إعراب): يخفي أخطاء المشكّل في الإعراب على حساب فصاحة الوصل؛ تنوين الفتح يبقى ألفًا والشدة تبقى."""
    def fix(m):
        word, marks = m.group(1), m.group(2)
        keep = "\u0651" if "\u0651" in marks else ""
        if "\u064B" in marks and not word.endswith("ة"):
            return word + keep + "\u064B"
        return word + keep
    return WORD_END.sub(fix, text)


def pausal(text):
    """الوقف: آخر كلمة قبل علامة ترقيم أو نهاية النص تُسكَّن (تسقط حركتها وتنوينها)، إلا تنوين الفتح فيبقى ألفًا؛ هكذا يقرأ العربي."""
    def fix(m):
        word, marks = m.group(1), m.group(2)
        keep = "\u0651" if "\u0651" in marks else ""          # الشدة تبقى (الرَّئِيسِيّ)
        if "\u064B" in marks and not word.endswith("ة"):      # مَلَفًّا: تنوين الفتح يُنطق ألفًا ويُقرأ «ملفّا»
            return word + keep + "\u064B"
        return word + keep

    return PAUSE_END.sub(fix, text)


def speech_text(text, vocalizer):
    """نص جاهز للنطق: الأرقام كلمات عربية، والنسبة «بالمئة»، ثم تشكيل آلي؛ وعند أي خلل يعود النص كما هو."""
    try:
        from num2words import num2words

        def words(m):
            whole, _, fraction = m.group().replace(",", ".").partition(".")
            spoken = num2words(int(whole), lang="ar").replace("و ", "و")
            if fraction:
                spoken += " فاصلة " + num2words(int(fraction), lang="ar").replace("و ", "و")
            before = text[m.start() - 1] if m.start() else " "
            after = text[m.end()] if m.end() < len(text) else " "
            return ("" if before in " و(" else " ") + spoken + ("" if after in " .،,؛:!؟?)%" else " ")

        text = DIGITS.sub(words, text.replace("%", " بالمئة "))
        text = spoken_punct(text)
        if not vocalizer:
            return text
        if dense_tashkeel(text):                                 # نص مشكَّل أصلًا (say.txt أو جواب مشكَّل): المشكّل يعيد تشكيله فيُفسد إعرابه
            spoken = text
        else:
            spoken = correct(vocalizer.diacritize(text))
        return shadda_first(pausal_all(spoken) if PAUSAL_ALL else pausal(spoken))
    except Exception:
        logging.exception("تجهيز نص النطق")
        return text


def graphemes(text):
    """ما تحذفه ضغطة Backspace واحدة: حرف مع حركاته، أو رمز بجزأيه."""
    return sum(1 for c in COMBINING.sub("", text) if not 0xDC00 <= ord(c) <= 0xDFFF)


def tidy(text):
    """يحذف عبارات الهلوسة المعروفة، ويحول الترقيم المنطوق، ويوحد الأرقام."""
    if PHANTOM.search(text) or (plain(text) and plain(text) in plain(PROMPT)):      # صدى الموجّه حرفيًا حين لا يفهم الصوت؛ لا يمسّ جملة المالك عن «العربية الفصحى»
        return ""
    for spoken, mark in SPOKEN:
        text = spoken.sub(mark, text)
    return text.translate(EASTERN_DIGITS).strip()


def plain(text):
    """صورة موحدة للمطابقة: بلا حركات ولا همزات ولا ترقيم، وبحروف صغيرة."""
    text = re.sub("[\u064b-\u0652\u0670\u0640]", "", text.lower())
    text = re.sub("[أإآ]", "ا", text)
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


SPOKEN_FORMS = {plain(form): (name, action) for name, action, forms in COMMANDS for form in forms}


def distance(a, b):
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def command_of(text):
    """الأمر إن طابق المقطع كله صيغة منطوقة، أو قاربها بحرف أو حرفين في المقاطع القصيرة."""
    said = plain(text)
    if not said:
        return None
    if said in SPOKEN_FORMS:
        return SPOKEN_FORMS[said] + (True,)
    if len(said.split()) > FUZZY_MAX_WORDS:
        return None
    close = []
    for form, command in SPOKEN_FORMS.items():
        if len(form) < FUZZY_MIN_LEN:
            continue
        gap = distance(said, form)
        if gap <= (1 if len(form) <= 5 else 2) and gap / len(form) <= 0.5:
            close.append((gap, form, command))
    close.sort()
    if close and (len(close) == 1 or close[0][0] < close[1][0] or close[0][2][0] == close[1][2][0]):
        return close[0][2] + (False,)
    return None


def read_wav(path):
    """ملف الاختبار إلى عينات 16 kHz أحادية."""
    import wave

    import numpy
    with wave.open(path) as w:
        rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError("يلزم wav بعينات 16 بت")
    samples = numpy.frombuffer(raw, dtype="<i2").astype("float32").reshape(-1, channels).mean(axis=1) / 32768
    return numpy.interp(numpy.arange(0, len(samples), rate / RATE), numpy.arange(len(samples)), samples).astype("float32")


def read_clipboard():
    """نص الحافظة للقراءة فقط؛ لا يكتب فيها شيئًا."""
    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        try:
            return ctypes.wstring_at(pointer) if pointer else None
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


class Speaker:
    """ينطق النص جملةً جملة بصوت ويندوز العربي عبر خادم PowerShell دائم؛ الإيقاف فوري."""

    def __init__(self):
        self.proc, self.queue, self.speaking, self.generation, self.voice = None, queue.Queue(), False, 0, ""
        self.envelope, self.play_start, self.latency = [], 0.0, OUTPUT_LATENCY
        self.now_text, self.cumulative = "", []                      # الجملة الجارية وتقدّم طاقتها (0-1) لإضاءة الكلمات
        self.piper, self.piper_failed, self.vocalizer = None, False, None
        self.silma, self.silma_lines, self.silma_state = None, queue.Queue(), ""      # "" لم يبدأ، "starting"، "ready"، "failed"
        atexit.register(self.stop_silma)
        saved = (VOICE_FILE.read_text(encoding="utf-8").split() + ["kareem", str(SPEED_DEFAULT)])[:2] if VOICE_FILE.exists() else ["kareem", str(SPEED_DEFAULT)]
        self.choice, self.speed = saved[0], min(SPEED_MAX, max(SPEED_MIN, float(saved[1])))
        self.folder = pathlib.Path(tempfile.gettempdir()) / "VoiceDictateSpeech"
        self.folder.mkdir(exist_ok=True)
        threading.Thread(target=self.work, daemon=True).start()
        if self.choice in ("kareem", "silma"):
            threading.Thread(target=self.ensure_piper, daemon=True).start()
        if self.choice == "silma":
            threading.Thread(target=self.start_silma, daemon=True).start()

    def choose(self, name=None, speed=None):
        self.choice = name or self.choice
        self.speed = min(SPEED_MAX, max(SPEED_MIN, speed if speed is not None else self.speed))
        VOICE_FILE.write_text("%s %.2f" % (self.choice, self.speed), encoding="utf-8")
        name = self.choice
        if name in ("kareem", "silma"):
            threading.Thread(target=self.ensure_piper, daemon=True).start()
        if name == "silma":
            threading.Thread(target=self.start_silma, daemon=True).start()

    def start_silma(self):
        """يشغّل خادم SILMA (مرة واحدة) ويقرأ سطر الجاهزية؛ خيط القراءة يغذّي silma_lines طوال عمره."""
        if self.silma_state in ("starting", "ready"):
            return
        self.silma_state = "starting"
        if not (SILMA_PYTHON.exists() and SILMA_SERVER.exists()):
            logging.warning("خادم سلمى غير مثبت في %s", SILMA_DIR)
            self.silma_state = "failed"
            return
        self.kill_stale_silma()
        try:
            self.silma = subprocess.Popen([str(SILMA_PYTHON), "-X", "utf8", str(SILMA_SERVER)], cwd=str(SILMA_DIR),
                                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            SILMA_PID.write_text(str(self.silma.pid), encoding="utf-8")
            threading.Thread(target=self.read_silma, daemon=True).start()
            line = self.silma_lines.get(timeout=SILMA_START_TIMEOUT)
        except (OSError, queue.Empty) as e:
            logging.warning("تعذر تشغيل خادم سلمى: %s", e)
            self.stop_silma()
            self.silma_state = "failed"
            return
        self.silma_state = "ready" if line.startswith("ready") else "failed"
        logging.info("خادم سلمى: %s", line)

    def await_silma(self, generation):
        """ينتظر جاهزية خادم سلمى (يُشغّله إن لم يبدأ) ما دام الرد ما زال مطلوبًا؛ يعود عند الجاهزية أو الفشل أو إلغاء الرد."""
        if self.silma_state == "":
            threading.Thread(target=self.start_silma, daemon=True).start()
        began = time.time()
        while self.silma_state in ("", "starting") and generation == self.generation and time.time() - began < SILMA_START_TIMEOUT:
            time.sleep(0.2)

    def read_silma(self):
        proc = self.silma
        for line in proc.stdout:
            self.silma_lines.put(line.strip())
        self.silma_lines.put("err exited")

    def kill_stale_silma(self):
        """خادم يتيم من تشغيل سابق يحجز 3 GB من كرت الشاشة؛ يُقتل عبر ملف pid."""
        try:
            pid = int(SILMA_PID.read_text(encoding="utf-8").strip())
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, ValueError):
            pass

    def stop_silma(self):
        if self.silma and self.silma.poll() is None:
            self.silma.kill()
        self.silma = None
        if self.silma_state == "ready":
            self.silma_state = ""

    def synthesize_silma(self, text, index):
        """جملة عبر خادم سلمى؛ None عند أي خلل أو تأخر فتعود الجملة إلى كريم."""
        path = self.folder / ("m%d.wav" % index)
        import base64
        speed = max(0.7, min(1.5, 1.0 + (self.speed - SPEED_DEFAULT)))       # سرعة كريم 135% تقابل 100% عند سلمى
        began = time.time()
        try:
            while not self.silma_lines.empty():
                self.silma_lines.get_nowait()
            spoken = speech_text(text, self.ensure_vocalizer())      # تشكيل rawi (أدق في أواخر الكلمات من مشكّل سلمى الداخلي) مع الوقف
            self.silma.stdin.write(str(path) + "\t" + base64.b64encode(spoken.encode("utf-8")).decode() + "\t%.2f\n" % speed)
            self.silma.stdin.flush()
            reply = self.silma_lines.get(timeout=SILMA_TIMEOUT)
        except (OSError, ValueError, queue.Empty, AttributeError) as e:
            logging.warning("خادم سلمى لا يستجيب (%s)؛ كريم احتياطًا", e)
            self.stop_silma()
            self.silma_state = "failed"
            return None
        if not reply.startswith("ok"):
            logging.warning("سلمى: %s", reply)
            return None
        logging.info("نطق سلمى: %d حرفًا، توليد %.2f ث", len(text), time.time() - began)
        return path

    def ensure_piper(self):
        """صوت كريم من Piper على المعالج؛ التحميل نحو 3 ثوانٍ مرة واحدة."""
        if self.piper or self.piper_failed or not PIPER_VOICE.exists():
            return self.piper is not None
        try:
            from piper import PiperVoice
            self.piper = PiperVoice.load(str(PIPER_VOICE))
            logging.info("صوت كريم جاهز")
        except Exception:
            logging.exception("تعذر تحميل صوت كريم، سيُستعمل نايف")
            self.piper_failed = True
        return self.piper is not None

    def ensure_vocalizer(self):
        if self.vocalizer is None:
            try:
                import text2tashkeel
                self.vocalizer = text2tashkeel.Diacritizer(TASHKEEL_MODEL)
                logging.info("المشكّل جاهز: %s، معجم التصحيح %d كلمة", TASHKEEL_MODEL, len(load_lexicon()))
            except Exception:
                logging.exception("تعذر تحميل المشكّل؛ النطق بلا تشكيل")
                self.vocalizer = False
        return self.vocalizer or None

    def synthesize_piper(self, text, index):
        path = self.folder / ("p%d.wav" % index)
        from piper.config import SynthesisConfig
        self.ensure_vocalizer()
        began = time.time()
        with wave.open(str(path), "wb") as w:
            self.piper.synthesize_wav(speech_text(text, self.vocalizer or None), w, SynthesisConfig(length_scale=1.0 / self.speed))
        with wave.open(str(path)) as w:
            seconds = w.getnframes() / w.getframerate()
        logging.info("نطق: %d حرفًا، توليد %.2f ث، مدة %.1f ث", len(text), time.time() - began, seconds)
        return path

    def ensure(self):
        if self.proc and self.proc.poll() is None:
            return True
        self.proc = subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(HERE / "speak.ps1")],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        line = self.proc.stdout.readline().strip()
        self.voice = line[6:] if line.startswith("ready") else ""
        logging.info("خادم النطق: %s", line)
        return line.startswith("ready")

    def synthesize(self, text, index):
        path = self.folder / ("s%d.wav" % index)
        import base64
        self.proc.stdin.write(str(path) + "\t" + base64.b64encode(text.encode("utf-8")).decode() + "\t%.2f\n" % self.speed)
        self.proc.stdin.flush()
        reply = self.proc.stdout.readline().strip()
        return path if reply.startswith("ok") else None

    def say(self, text):
        self.stop()
        self.add(text)

    def add(self, text):
        """يضيف إلى ما يُنطق دون قطع الجاري؛ للردود المتدفقة."""
        self.speaking = True
        self.queue.put((self.generation, text[:MAX_SPEAK_CHARS]))

    def stop(self):
        self.generation += 1
        winsound.PlaySound(None, SND_PURGE)
        self.speaking = False

    def play(self, path, generation):
        """يشغّل الملف على افتراضي ويندوز عبر winsound؛ ويندوز نفسه يبدّل سماعة البلوتوث بين Headphones وHands-Free.
        الشرط: ألا يكون التطبيق مثبّتًا على جهاز بعينه في «خلاط الصوت»، وإلا ذهب الصوت إليه (يُكشف ذلك في سجل بدء التسجيل)."""
        with wave.open(str(path)) as w:
            seconds = w.getnframes() / w.getframerate()
            try:
                self.envelope = envelope(w.readframes(w.getnframes()), w.getframerate(), w.getsampwidth(), w.getnchannels())
            except Exception:
                logging.exception("غلاف الصوت")
                self.envelope = []
        total = sum(self.envelope) or 1.0
        acc, self.cumulative = 0.0, []
        for v in self.envelope:
            acc += v
            self.cumulative.append(acc / total)
        self.play_start = time.time()
        self.latency = OUTPUT_LATENCY_BT if BLUETOOTH_OUT.search(self.output_name()) else OUTPUT_LATENCY
        winsound.PlaySound(str(path), SND_FILENAME | SND_ASYNC)
        end = time.time() + seconds
        while time.time() < end and generation == self.generation:
            time.sleep(0.05)
        self.envelope = []

    def progress(self):
        """نسبة ما نُطق من الجملة الجارية (0-1) بحسب الطاقة المتراكمة في الغلاف؛ الصمت بين الكلمات لا يتقدم بها."""
        i = int((time.time() - self.play_start - self.latency) / ENVELOPE_STEP)
        if not self.cumulative:
            return 0.0
        return self.cumulative[min(max(i, 0), len(self.cumulative) - 1)] if i >= 0 else 0.0

    def level(self):
        """مستوى صوت الوكيل الآن (0-1) من غلاف الملف الجاري؛ تتحرك به دائرة اللوحة."""
        i = int((time.time() - self.play_start - self.latency) / ENVELOPE_STEP)
        return self.envelope[i] if 0 <= i < len(self.envelope) else 0.0

    @staticmethod
    def output_name():
        try:
            import sounddevice
            return sounddevice.query_devices(kind="output")["name"]
        except Exception:
            return "?"

    def work(self):
        while True:
            generation, text = self.queue.get()
            try:
                if self.choice == "silma":
                    self.await_silma(generation)                                   # لا كريم إلى حين الجاهزية؛ ننتظرها
                use_silma = self.choice == "silma" and self.silma_state == "ready"
                use_piper = self.choice in ("kareem", "silma") and self.ensure_piper()   # كريم: الصوت المختار، أو احتياط إن فشل خادم سلمى
                if self.choice == "silma" and not use_silma:
                    logging.warning("سلمى غير جاهزة (%s)؛ كريم احتياطًا لهذا الرد", self.silma_state or "لم تبدأ")
                if not use_piper and not use_silma and not self.ensure():
                    continue
                self.speaking = True
                for i, sentence in enumerate(x.strip() for x in SENTENCE_SPLIT.split(text) if x.strip()):
                    if generation != self.generation:
                        break
                    path = self.synthesize_silma(sentence, i) if use_silma and self.silma_state == "ready" else None
                    if path is None:
                        path = self.synthesize_piper(sentence, i) if use_piper else self.synthesize(sentence, i)
                    if not path or generation != self.generation:
                        continue
                    self.now_text = sentence
                    self.play(path, generation)
                    self.now_text = ""
            except (OSError, ValueError):
                logging.exception("فشل النطق")
            finally:
                if generation == self.generation and self.queue.empty():
                    self.speaking = False


def clean_markdown(text):
    """نص Bionic للنطق: بلا ماركداون ولا رموز تعبيرية ولا وسوم."""
    text = re.sub(r"<[^>]{1,40}>", " ", text)
    text = MARKDOWN.sub(lambda m: m.group(1) or m.group(2) or " ", text)
    text = EMOJI.sub("", text)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n{2,}", "\n", text)).strip()


def find_window(prefix):
    """أول نافذة ظاهرة يبدأ عنوانها بالبادئة."""
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if buf.value.startswith(prefix):
                    found.append(hwnd)
                    return False
        return True

    user32.EnumWindows(visit, 0)
    return found[0] if found else None


def bring_front(hwnd):
    """ضغطة Alt وهمية تمنح العملية حق نقل التركيز، ثم SetForegroundWindow."""
    send([key_event(VK_MENU), key_event(VK_MENU, flags=KEYEVENTF_KEYUP)])
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.25)
    return user32.GetForegroundWindow() == hwnd


class BionicWatcher:
    """يقرأ قاعدة جلسات Bionic (sqlite، قراءة فقط) كل ثانية وينطق الجديد: ردود، خطوات، أخطاء."""

    def __init__(self, speaker, tell):
        self.speaker, self.tell, self.on, self.seen = speaker, tell, False, {}
        self.last_reply, self.last_state = "", ""
        threading.Thread(target=self.work, daemon=True).start()

    def databases(self):
        paths = [BIONIC_ROOT / ".internal" / "ng-sessions.sqlite"]
        paths += sorted((BIONIC_ROOT / "projects").glob("*/.internal/ng-sessions.sqlite")) if (BIONIC_ROOT / "projects").is_dir() else []
        return [x for x in paths if x.exists()]

    def rows(self, path, after):
        try:
            with sqlite3.connect("file:%s?mode=ro" % path.as_posix(), uri=True, timeout=0.5) as c:
                return c.execute("select rowid, entry_json from chat_entries where rowid > ? order by rowid", (after,)).fetchall()
        except sqlite3.Error as e:
            logging.warning("قاعدة Bionic: %s", str(e)[:120])
            return []

    def start(self):
        for path in self.databases():
            rows = self.rows(path, 0)
            self.seen[str(path)] = rows[-1][0] if rows else 0
        self.on = True

    def work(self):
        while True:
            time.sleep(BIONIC_POLL)
            if not self.on:
                continue
            for path in self.databases():
                key = str(path)
                for rowid, raw in self.rows(path, self.seen.get(key, 0)):
                    self.seen[key] = rowid
                    try:
                        self.narrate(json.loads(raw))
                    except (ValueError, TypeError, KeyError):
                        logging.exception("مدخل Bionic غير مفهوم")

    def narrate(self, entry):
        kind = entry.get("type")
        if kind == "message":
            message = entry.get("message") or {}
            if message.get("role") != "assistant" or entry.get("hidden"):
                return
            raw = " ".join(p.get("text", "") for p in message.get("parts", []) if p.get("type") == "text")
            if re.fullmatch(r"\s*<name>.*</name>\s*", raw, re.DOTALL):
                return      # اقتراح عنوان الجلسة، ليس ردًا
            text = clean_markdown(raw)
            if not text:
                return
            self.last_reply = text
            spoken = text if len(text) <= BIONIC_MAX_CHARS else text[:BIONIC_MAX_CHARS].rsplit(" ", 1)[0] + ". والباقي في النافذة."
            self.speaker.add(spoken)
        elif kind == "error":
            raw = str(entry.get("message", ""))
            text = next((ar for en, ar in ERRORS_AR if en in raw.lower() and ar), "") or raw[:120]
            self.last_state = "خطأ: " + text
            self.speaker.add("خطأ في بايونيك: " + text)
        elif kind == "turnSummary":
            seconds = float(entry.get("durationMs") or 0) / 1000
            self.last_state = "انتهت الدورة"
            if seconds >= 20:
                self.speaker.add("انتهى خلال %d دقيقة و%d ثانية." % divmod(int(seconds), 60) if seconds >= 60 else "انتهى خلال %d ثانية." % seconds)
        elif kind == "stateChange":
            previews = [c.get("previewText", "") for c in entry.get("stateChanges", []) if c.get("previewText")]
            if previews:
                self.last_state = previews[-1]
        elif "tool" in str(kind).lower() or "skill" in str(kind).lower():
            name = entry.get("name") or entry.get("toolName") or entry.get("skillName") or str(entry.get("tool", ""))[:40] or kind
            self.last_state = "يشغّل " + str(name)
            self.speaker.add("يشغّل " + str(name) + ".")
        elif kind not in ("forkPoint",):
            logging.info("مدخل Bionic من نوع %s", kind)

    def send(self, text):
        """ينقل التركيز إلى نافذة Bionic، يكتب النص ويرسله (أو يضغط Esc إن كان النص فارغًا)، ثم يعيد التركيز."""
        hwnd = find_window(BIONIC_TITLE)
        if not hwnd:
            self.tell("نافذة Bionic غير مفتوحة")
            return False
        previous = user32.GetForegroundWindow()
        if not bring_front(hwnd):
            self.tell("تعذر نقل التركيز إلى Bionic")
            return False
        if text:
            paste(text)
            press(VK_RETURN)
        else:
            press(VK_ESCAPE)
        time.sleep(0.3)
        if previous and previous != hwnd:
            bring_front(previous)
        return True


def wait_for_keys():
    while user32.GetAsyncKeyState(VK_CONTROL) & 0x8000 or user32.GetAsyncKeyState(VK_MENU) & 0x8000:
        time.sleep(0.03)


def key_event(vk=0, scan=0, flags=0):
    item = INPUT(type=INPUT_KEYBOARD)
    item.ki = KEYBDINPUT(vk, scan, flags, 0, None)
    return item


def send(events):
    array = (INPUT * len(events))(*events)
    sent = user32.SendInput(len(events), ctypes.byref(array), ctypes.sizeof(INPUT))
    if sent != len(events):
        raise OSError("SendInput أرسل %d من %d" % (sent, len(events)))


def press(key, modifier=None, times=1):
    wait_for_keys()
    events = [key_event(modifier)] if modifier else []
    for _ in range(times):
        events += [key_event(key), key_event(key, flags=KEYEVENTF_KEYUP)]
    if modifier:
        events.append(key_event(modifier, flags=KEYEVENTF_KEYUP))
    for i in range(0, len(events), TYPE_CHUNK):
        send(events[i:i + TYPE_CHUNK])


def paste(text):
    """يكتب النص وحدةً وحدةً بأحداث يونيكود، فلا يمس الحافظة."""
    wait_for_keys()
    units = text.encode("utf-16-le")
    events = []
    for i in range(0, len(units), 2):
        unit = int.from_bytes(units[i:i + 2], "little")
        events += [key_event(0, unit, KEYEVENTF_UNICODE), key_event(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]
    for i in range(0, len(events), TYPE_CHUNK):
        send(events[i:i + TYPE_CHUNK])


class Recorder:
    """يسجل باستمرار، ويسلّم كل مقطع كلام عند الوقفة؛ حد الكلام يتبع ضجيج الغرفة."""

    def __init__(self, deliver, muted=lambda: False):
        self.deliver, self.muted, self.stream, self.level, self.noise, self.silent = deliver, muted, None, 0.0, None, 0.0
        self.pulse = (0.0, [])                                       # (وقت وصول الكتلة، مستويات أجزائها كل 20 مللي) لمزامنة اللوحة
        self.device_name, self.pinned = "", ""
        self.reset()

    def reset(self):
        self.chunks, self.voiced, self.quiet = [], 0, 0.0

    @property
    def active(self):
        return self.stream is not None

    @property
    def idle(self):
        return self.active and self.silent >= IDLE_STOP

    def start(self):
        import sounddevice
        self.reset()
        self.noise, self.silent = None, 0.0
        try:        # PortAudio يحفظ قائمة الأجهزة والافتراضي عند التهيئة فقط؛ إعادة التهيئة تلتقط سماعة بلوتوث وُصلت أو افتراضيًا تغيّر بعد التشغيل
            sounddevice._terminate()
            sounddevice._initialize()
        except Exception:
            logging.exception("تعذرت إعادة تهيئة أجهزة الصوت")
        device = self.pick_device(sounddevice)
        logging.info("جهاز الإدخال: %s | الإخراج: %s", self.device_name, Speaker.output_name())
        self.pinned = pinned_elsewhere(sounddevice)
        if self.pinned:
            logging.warning("التطبيق مثبّت على جهاز في خلاط الصوت: %s", self.pinned)
        stream = sounddevice.InputStream(samplerate=RATE, channels=1, dtype="float32", blocksize=BLOCK, callback=self._take, device=device)
        try:
            stream.start()
        except Exception:
            stream.close()
            raise
        self.stream = stream

    def pick_device(self, sounddevice):
        """الجهاز الافتراضي، إلا إن كان «Stereo Mix» ونحوه (يلتقط السماعات لا صوتك) فأول ميكروفون حقيقي."""
        default = sounddevice.query_devices(kind="input")
        self.device_name = default["name"]
        if BT_MIC.search(default["name"]):       # قرار المالك 7/11: الكلام إلى ميكروفون الجهاز، والسماعة للصوت فقط (تبقى بجودة Headphones)
            for index, info in enumerate(sounddevice.query_devices()):
                if info["max_input_channels"] > 0 and BUILTIN_MIC.search(info["name"]) and not NOT_A_MIC.search(info["name"]):
                    logging.info("الإدخال الافتراضي «%s» بلوتوث؛ استُعمل ميكروفون الجهاز «%s»", default["name"], info["name"])
                    self.device_name = info["name"]
                    return index
        if not NOT_A_MIC.search(default["name"]):
            return None
        for index, info in enumerate(sounddevice.query_devices()):
            if info["max_input_channels"] > 0 and IS_A_MIC.search(info["name"]) and not NOT_A_MIC.search(info["name"]):
                logging.warning("الإدخال الافتراضي «%s» ليس ميكروفونًا؛ استُعمل «%s»", default["name"], info["name"])
                self.device_name = info["name"]
                return index
        logging.warning("الإدخال الافتراضي «%s» ليس ميكروفونًا ولم أجد بديلًا", default["name"])
        return None

    def _take(self, data, frames, when, status):
        self.feed(data[:, 0].copy())

    def live(self):
        """مستوى الميكروفون اللحظي: الجزء المناسب من آخر كتلة بحسب الزمن المنقضي منذ وصولها؛ أدق من level للحركة."""
        arrived, parts = self.pulse
        if not parts:
            return self.level
        i = int((time.time() - arrived) / 0.02)
        return parts[i] if i < len(parts) else parts[-1] if time.time() - arrived < 0.25 else 0.0

    def feed(self, block):
        if self.muted():
            self.level, self.chunks, self.voiced, self.quiet = 0.0, [], 0, 0.0
            return
        seconds = len(block) / RATE
        self.level = float((block * block).mean()) ** 0.5
        part = max(1, int(RATE * 0.02))
        self.pulse = (time.time(), [float((block[i:i + part] ** 2).mean()) ** 0.5 for i in range(0, len(block) - part + 1, part)])
        self.noise = self.level if self.noise is None else min(self.level, self.noise * NOISE_RISE)
        self.chunks.append(block)
        if self.level > max(self.noise * SPEECH_RATIO, SPEECH_FLOOR):
            self.voiced, self.quiet, self.silent = self.voiced + 1, 0.0, 0.0
        else:
            self.quiet, self.silent = self.quiet + seconds, self.silent + seconds
        if self.voiced < MIN_VOICED:
            self.chunks = self.chunks[-LEAD_BLOCKS:]
            if self.quiet >= PAUSE_SECONDS:
                self.voiced = 0
        elif self.quiet >= PAUSE_SECONDS or len(self.chunks) * seconds >= MAX_SEGMENT:
            self.cut()

    def cut(self):
        import numpy
        audio = numpy.concatenate(self.chunks)
        self.reset()
        self.deliver(audio)

    def stop(self):
        stream, self.stream, self.level = self.stream, None, 0.0
        try:
            stream.stop()
        finally:
            stream.close()
        if self.voiced >= MIN_VOICED:
            self.cut()
        self.reset()


class App:
    def __init__(self):
        self.jobs, self.working, self.fresh = queue.Queue(), False, True
        self.speaker = Speaker()
        self.bionic = BionicWatcher(self.speaker, self.tell)
        self.mode, self.thinking, self.history, self.heard, self.heard_at = "dictate", False, [], "", 0.0
        self.model, self.recorder = None, Recorder(self.jobs.put, lambda: self.speaker.speaking or self.thinking)
        self.note, self.note_until = "", 0.0
        self.prompt, self.last_text, self.last_len, self.want_stop = None, "", 0, False
        self.fresh_before, self.context, self.last_text_before = True, "", ""
        threading.Thread(target=self.work, daemon=True).start()

    def tell(self, text, seconds=6):
        self.note, self.note_until = text, time.time() + seconds

    def status(self):
        pending = self.working or not self.jobs.empty()
        if self.thinking:
            return "محادثة: أفكر...", WORK
        if self.speaker.speaking:
            return ("محادثة: أجيب... انقر للمقاطعة" if self.mode == "chat" else "أقرأ... انقر للإسكات"), WORK
        if self.mode == "chat" and self.recorder.active:
            return ("محادثة: سمعتك، أكمل أو اصمت لأجيب" if self.heard else "محادثة: تكلّم"), LISTEN
        if self.recorder.active:
            return ("أستمع وأكتب..." if pending else "أستمع، وأكتب عند كل وقفة"), LISTEN
        if pending:
            return "أكتب النص...", WORK
        if time.time() < self.note_until:
            return self.note, IDLE
        if self.model is None:
            return "يحمّل النموذج...", WORK
        return "جاهز، اضغط وتكلّم", IDLE

    def warm(self):
        try:
            self.prompt = load_prompt()
            self.model = load_model()
            logging.info("النموذج جاهز: %s على %s", MODEL, "كرت الشاشة" if self.model.device == "cuda" else "المعالج")
            winsound.Beep(1000, 90)
            winsound.Beep(1300, 90)
        except Exception:
            logging.exception("تعذر تحميل النموذج")
            self.tell("تعذر تحميل النموذج", 3600)
            winsound.MessageBeep(winsound.MB_ICONHAND)

    def toggle(self):
        if self.speaker.speaking:
            self.speaker.stop()
            return
        if self.recorder.active:
            winsound.Beep(600, 120)
            try:
                self.recorder.stop()
            except Exception:
                logging.exception("تعذر إغلاق الميكروفون")
                self.tell("تعذر إغلاق الميكروفون")
        elif self.model is None:
            winsound.MessageBeep(winsound.MB_ICONHAND)
        else:
            try:
                self.recorder.start()
                self.fresh, self.want_stop, self.context = True, False, ""
                if NOT_A_MIC.search(self.recorder.device_name):
                    self.tell("تنبيه: جهاز الإدخال الافتراضي ليس ميكروفونًا", 10)
                elif self.recorder.pinned:
                    self.tell("تنبيه: التطبيق مثبّت على جهاز في خلاط الصوت", 10)
                winsound.Beep(1200, 120)
            except Exception:
                logging.exception("تعذر فتح الميكروفون")
                self.tell("تعذر فتح الميكروفون")
                winsound.MessageBeep(winsound.MB_ICONHAND)

    def switch_mode(self):
        """Ctrl+Alt+F10 أو نقر عنوان الوضع: يقلب بين الإملاء والمحادثة؛ يُنطق الوضع الجديد كي يُعرف بلا نظر."""
        new = "dictate" if self.mode == "chat" else "chat"
        self.act(("mode_hotkey", ("mode", new)))
        self.speaker.say("وضع المحادثة." if new == "chat" else "وضع الإملاء.")

    def act(self, command):
        name, (kind, arg) = command[:2]
        logging.info("أمر صوتي: %s", name)
        if kind == "key":
            press(arg[0], arg[1])
            if name in ("send", "line"):
                self.fresh, self.last_len = True, 0
            self.last_text = "نُفّذ: " + name
        elif kind == "type":
            paste(arg)
            self.fresh, self.last_text, self.last_len, self.context = False, arg, 1, (self.context + arg)[-CONTEXT_CHARS:]
        elif kind == "read":
            press(VK_C, VK_CONTROL)
            time.sleep(0.3)
            text = (read_clipboard() or "").strip()
            if text:
                self.last_text = "أقرأ %d حرفًا" % len(text)
                self.speaker.say(text)
            else:
                self.tell("حدد النص أولًا ثم قل «اقرأ»")
        elif kind == "hush":
            self.speaker.stop()
        elif kind == "speed":
            self.speaker.choose(speed=self.speaker.speed + arg)
            self.tell("سرعة النطق %.0f%%" % (self.speaker.speed * 100))
            self.speaker.say("هذه سرعتي الآن.")
        elif kind == "voice":
            self.speaker.choose(arg)
            self.tell("الصوت: " + VOICE_NAMES.get(arg, arg) + ("؛ تُحمَّل، وسأنتظرها" if arg == "silma" and self.speaker.silma_state != "ready" else ""))
            self.speaker.say({"kareem": "أنا كريم.", "naayf": "أنا نايف.", "silma": "أنا سلمى، الصوت الجديد."}.get(arg, "حسنًا."))
        elif kind == "erase":
            press(VK_BACK, times=self.last_len)
            self.fresh, self.last_len, self.last_text = self.fresh_before, 0, "مُسح آخر مقطع"
            self.context = self.context[: max(0, len(self.context) - len(self.last_text_before) - 1)] if self.last_text_before else self.context
        elif kind == "follow":
            if arg:
                self.bionic.start()
                self.tell("أتابع Bionic وأنطق ما يجدّ", 8)
                self.speaker.say("أتابع بايونيك الآن.")
            else:
                self.bionic.on = False
                self.tell("توقفت عن متابعة Bionic")
        elif kind == "bionic":
            if self.bionic.send(arg):
                self.tell(("أرسلت: " + arg) if arg else "أوقفت Bionic", 6)
        elif kind == "status":
            self.speaker.say(self.bionic.last_state or "لا نشاط مسجل لبايونيك.")
        elif kind == "repeat":
            self.speaker.say(self.bionic.last_reply or (self.history[-1]["content"] if self.history else "لا شيء لأكرره."))
        elif kind == "lexicon":
            self.tell("معجم التشكيل: %d كلمة" % len(load_lexicon()))
        elif kind == "mode":
            self.mode, self.heard = arg, ""
            self.tell("وضع المحادثة: تكلّم وستجيبك سلمى" if arg == "chat" else "وضع الإملاء")
            if arg == "chat" and not self.recorder.active:
                self.toggle()
        elif kind == "newchat":
            self.history, self.heard, self.mode = [], "", "chat"
            self.tell("محادثة جديدة")
        else:
            self.want_stop = True

    def due(self):
        """في وضع المحادثة: كلام تجمّع وسكت المالك بعده مدة CHAT_GAP."""
        return self.mode == "chat" and self.heard and not self.thinking and not self.speaker.speaking and time.time() - self.heard_at >= CHAT_GAP and self.recorder.quiet >= PAUSE_SECONDS

    def ask(self):
        question, self.heard, self.thinking = self.heard.strip(), "", True
        threading.Thread(target=self.converse, args=(question,), daemon=True).start()

    def write(self, text, newline=False):
        """يكتب الحوار في النافذة النشطة؛ السطر الجديد بـ Shift+Enter كي لا يُرسل في مربعات الدردشة."""
        if not CHAT_WRITE:
            return
        try:
            if text:
                paste(text)
            if newline:
                press(VK_RETURN, VK_SHIFT)
        except OSError:
            logging.exception("تعذرت كتابة الحوار")

    def converse(self, question):
        """يرسل السؤال مع آخر الحوار إلى Qwen ويقرأ الرد جملةً جملة فور اكتمال كل جملة."""
        began, reply, pending, spoken = time.time(), "", "", False
        self.write(ME_LABEL + question, newline=True)
        self.write(BOT_LABEL)
        try:
            messages = [{"role": "system", "content": CHAT_SYSTEM}] + self.history[-CHAT_TURNS * 2:] + [{"role": "user", "content": question + " /no_think"}]
            body = json.dumps({"model": CHAT_MODEL, "messages": messages, "stream": True, "temperature": 0.6, "max_tokens": 400}).encode("utf-8")
            request = urllib.request.Request(CHAT_URL, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=CHAT_TIMEOUT) as response:
                for raw in response:
                    line = raw.decode("utf-8").strip()
                    if not line.startswith("data:") or line == "data: [DONE]":
                        continue
                    delta = json.loads(line[5:]).get("choices", [{}])[0].get("delta", {}).get("content") or ""
                    reply += delta
                    pending = THINK.sub("", pending + delta)
                    if "<think>" in pending and "</think>" not in pending:
                        continue
                    if pending.rstrip(" \t").endswith("\n"):
                        continue                                     # سطر جديد في الآخر: ننتظر ما بعده كي تُحذف علامة القائمة التالية كاملة
                    parts = SENTENCE_SPLIT.split(prose(pending))
                    for sentence in parts[:-1]:
                        if sentence.strip() and not MARK_ONLY.fullmatch(sentence):
                            self.speaker.add(sentence.strip())
                            self.write(sentence.strip() + " ")
                            spoken = True
                            if self.thinking:
                                self.thinking, self.last_text = False, sentence.strip()
                    pending = parts[-1]
            pending = prose(pending)
            if pending.strip() and not MARK_ONLY.fullmatch(pending):
                self.speaker.add(pending.strip())
                self.write(pending.strip())
                spoken = True
            self.write("", newline=True)
            self.write("", newline=True)
            reply = prose(THINK.sub("", reply)).strip()
            self.history += [{"role": "user", "content": question}, {"role": "assistant", "content": reply}]
            self.last_text = reply
            with CHAT_LOG.open("a", encoding="utf-8") as log:
                log.write("[%s] أنا: %s\n[%s] Qwen: %s\n" % (time.strftime("%H:%M"), question, time.strftime("%H:%M"), reply))
            logging.info("محادثة: %d حرف سؤال، %d حرف رد، %.1f ثانية حتى أول جملة", len(question), len(reply), time.time() - began)
            if not spoken:
                self.tell("لم يرد Qwen بشيء")
        except (OSError, ValueError, KeyError) as e:
            logging.warning("فشل الاتصال بـ Qwen: %s", str(e)[:160])
            self.tell("تعذر الوصول إلى Qwen في LM Studio", 10)
            self.speaker.say("تعذر الوصول إلى النموذج.")
            self.write("(تعذر الوصول إلى Qwen)", newline=True)
        finally:
            self.thinking = False

    def work(self):
        while True:
            audio = self.jobs.get()
            self.working = True
            try:
                self.finish(audio)
            finally:
                self.working = False

    def transcribe(self, audio):
        """المصطلحات ثم آخر ما كُتب كسياق؛ وإن نفدت ذاكرة كرت الشاشة أثناء العمل انتقل إلى المعالج وأعاد المحاولة مرة."""
        prompt = self.prompt + (" " + self.context if self.context else "")
        try:
            return transcribe(self.model, audio, prompt)
        except RuntimeError as e:
            if self.model.device != "cuda" or "memory" not in str(e).lower():
                raise
            logging.warning("نفدت ذاكرة كرت الشاشة، الانتقال إلى المعالج: %s", str(e)[:120])
            self.tell("كرت الشاشة ممتلئ، انتقلت إلى المعالج", 20)
            self.model = load_model(cpu_only=True)
            return transcribe(self.model, audio, prompt)

    def finish(self, audio):
        try:
            seconds = len(audio) / RATE
            peak = float(abs(audio).max())
            if peak > 0:
                audio = audio * min(MAX_GAIN, max(1.0, TARGET_PEAK / peak))
            began = time.time()
            text, logprob, no_speech = self.transcribe(audio)
            text = tidy(text) if logprob >= MIN_LOGPROB and no_speech <= MAX_NO_SPEECH else ""
            logging.info("%.1f ثانية صوت، قمة %.3f، ضجيج %.5f، ثقة %.2f، لا كلام %.2f، %.1f ثانية تفريغ، %d حرفًا", seconds, peak, self.recorder.noise or 0, logprob, no_speech, time.time() - began, len(text))
            command = command_of(text)
            why = hallucinated(text) if text and not command else ""
            if why:
                logging.info("هلوسة (%s) أُهملت: %s", why, text[:60])
                text = ""
            if not command and seconds <= RETRY_SECONDS and peak >= WEAK_PEAK and (logprob < RETRY_LOGPROB or not text):
                english, logprob_en, no_speech_en = transcribe(self.model, audio, language="en")
                command = command_of(english)
                if command:     # أمر إنجليزي سمعه النموذج العربي حروفًا عربية مشوهة؛ التفريغ الإنجليزي يُستعمل للأوامر فقط لا للكتابة
                    text, logprob, no_speech = english, logprob_en, no_speech_en
                    logging.info("تفريغ إنجليزي: %s، ثقة %.2f", english, logprob_en)
            if command and peak >= WEAK_PEAK and logprob >= (COMMAND_LOGPROB_EXACT if command[2] else COMMAND_LOGPROB) and no_speech <= COMMAND_NO_SPEECH:
                self.act(command)
            elif command:
                logging.info("أمر مشكوك فيه أُهمل: %s → %s", text, command[0])
                self.tell("لم أتأكد من الأمر، أعده بوضوح")
            elif text and self.mode == "chat":
                self.heard, self.heard_at, self.last_text = (self.heard + " " + text).strip(), time.time(), text
            elif text:
                piece = text if self.fresh else " " + text
                self.fresh_before, self.last_text_before = self.fresh, text
                paste(piece)
                self.fresh, self.last_text, self.last_len = False, text, graphemes(piece)
                self.context = (self.context + " " + text)[-CONTEXT_CHARS:].lstrip()
            else:
                self.tell("الصوت ضعيف، اقترب من الميكروفون" if peak < WEAK_PEAK else "لم أميّز كلامًا، أعد المحاولة")
        except Exception:
            logging.exception("فشل الإملاء")
            self.tell("فشل الإملاء، راجع السجل")
            winsound.MessageBeep(winsound.MB_ICONHAND)


def rgb(color):
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


def mix(a, b, t):
    """لون بين a وb بنسبة t (0 → a، 1 → b)."""
    return tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


class Wave:
    """سحابة جسيمات ثلاثية الأبعاد (طلب المالك 7/11: إحساس بالعمق في كل الاتجاهات): كل جسيم له موضع (x, y, z) في سحابة بيضوية
    تدور ببطء حول محورها الرأسي وتتمايل، فتمرّ الجسيمات أمامًا وخلفًا ويمينًا ويسارًا وفوق وتحت؛ الإسقاط بمنظور: القريب أكبر وأسطع وأحدّ،
    والبعيد أصغر وأخفت وأشد ضبابية ويميل إلى لون الخلفية. الصوت يوسّع السحابة في الأبعاد الثلاثة ويدفع الجسيمات إلى الخارج.
    تُرسم بـ Pillow بدقة مضاعفة ثم تُصغَّر (تنعيم الحواف) في طبقتين: بعيدة مموّهة وقريبة حادة."""

    SS = 2                                                          # معامل الدقة المضاعفة
    COUNT = 8                                                       # عدد الجسيمات (المالك 7/11: 5-10 بدل 81، شكل احترافي)
    R_SCALE = 2.4                                                   # معامل حجم الجسيم (أكبر حين يقلّ العدد)

    def __init__(self, k, width=WAVE_W, height=WAVE_H):
        import random
        from PIL import Image, ImageDraw, ImageFilter, ImageTk
        self.Image, self.ImageDraw, self.ImageFilter, self.ImageTk = Image, ImageDraw, ImageFilter, ImageTk
        self.k = k
        self.SS = 2 if k < 1.6 else 1                                # عند التكبير الكبير الدقة الأصلية تكفي ويبقى الإطار دون 40 مللي
        self.width, self.height = int(width * k), int(height * k)
        self.photo = None
        rnd = random.Random(7)
        # لكل جسيم: موضع x (أفقي -1..1)، y (رأسي غاوسي)، z (عمق -1..1)، نصف قطر، طور، لون، وسرعة دوران خاصة
        self.dots = []
        if self.COUNT <= 12:                                           # عدد قليل (المالك 7/11: 5-10 جسيمات): توزيع منتظم مقصود لا عشوائي
            for i in range(self.COUNT):
                a = 2 * math.pi * i / self.COUNT
                y = (0.0, 0.5, -0.4, 0.25, -0.6, 0.6)[i % 6] * 0.9
                self.dots.append((math.cos(a) * 0.95, y, math.sin(a) * 0.95, (1.6, 2.4, 1.2, 2.0)[i % 4], a, i / self.COUNT, 1.0 + 0.15 * (i % 3)))
        else:
            for _ in range(self.COUNT):
                x, y, z = rnd.uniform(-1, 1), max(-1.0, min(1.0, rnd.gauss(0, 0.45))), rnd.uniform(-1, 1)
                self.dots.append((x, y, z, rnd.choice((0.6, 0.8, 1.0, 1.4, 1.9, 2.6)), rnd.uniform(0, 2 * math.pi), rnd.random(), rnd.uniform(0.6, 1.4)))

    def render(self, amps, color, level, phase):
        """amps[0]: الانتشار المُنعَّم (0-1)، color: لون الحالة، level: 0-1 لشدة التوهّج، phase: زمن الحركة."""
        S, k = self.SS, self.k * self.SS
        w, h = self.width * S, self.height * S
        img = self.Image.new("RGBA", (w, h), (0, 0, 0, 0))
        tone, dark, white, violet = rgb(color), rgb(BG), (255, 255, 255), (140, 90, 255)
        cx, mid = w / 2, h / 2
        amp = amps[0]
        half_w, half_h = w * 0.46, (h / 2 - 2 * k)
        # بلا هالة خلف السحابة (طلب المالك)؛ العمق يُحسّ بالحجم والسطوع والضباب وحدها
        near_c, far_c = mix(tone, white, 0.55), mix(tone, violet, 0.5)
        idle_c = mix(dark, white, 0.4)
        rot, tilt = phase * 0.22, -math.radians(30) + 0.06 * math.sin(phase * 0.17)     # دوران بطيء حول الرأسي، وزاوية رؤية 30° من أعلى (البعيد أعلى في الصورة) مع تمايل طفيف
        cr, sr, ct, st = math.cos(rot), math.sin(rot), math.cos(tilt), math.sin(tilt)
        spread_y = (0.06 + 0.94 * amp) if level else 0.08 + 0.04 * amp * 10
        spread_z = 0.35 + 0.65 * amp if level else 0.5
        projected = []
        for x0, y0, z0, r0, ph, hue, spin in self.dots:
            # دوران الجسيم في مستوى x-z (أمام/خلف/يمين/يسار) بسرعته الخاصة ثم تمايل في y-z (فوق/تحت)
            a = rot * spin + ph
            ca, sa = math.cos(a), math.sin(a)
            x, z = x0 * ca - z0 * sa, x0 * sa + z0 * ca
            bell = math.exp(-(x * x) / 0.5)                                             # أكثف وأوسع في الوسط
            push = 1 + 0.35 * amp * math.sin(phase * 2.1 + ph)                          # دفع للخارج مع الصوت
            y = y0 * (0.12 + 0.88 * bell) * spread_y * push + 0.06 * math.sin(phase * 1.1 + ph)
            z = z * spread_z * push
            y, z = y * ct - z * st, y * st + z * ct
            f = 1 / (1.55 - 0.55 * z)                                                   # منظور: z=+1 أمام (أكبر)، z=-1 خلف (أصغر)
            depth = (z + 1) / 2                                                         # 0 بعيد، 1 قريب
            sx, sy = cx + x * half_w * f, mid + y * half_h * f
            r = r0 * self.R_SCALE * k * f * ((0.8 + 0.6 * amp) if level else 0.55)
            projected.append((depth, sx, sy, r, hue, abs(y0)))
        projected.sort()
        far = self.Image.new("RGBA", (w, h), (0, 0, 0, 0))
        near = self.Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d_far, d_near = self.ImageDraw.Draw(far), self.ImageDraw.Draw(near)
        few = self.COUNT <= 12                                                           # جسيمات قليلة: كلها مرئية، والعمق بالحجم والسطوع لا بالاختفاء
        fog_lo, a_lo, a_span = (0.85, 190, 65) if few else (0.35, 70, 185)
        for depth, sx, sy, r, hue, dist in projected:
            if level:
                col = mix(far_c, near_c, depth * (0.7 + 0.3 * hue))
                col = mix(dark, col, fog_lo + (1 - fog_lo) * depth)                       # ضباب العمق: البعيد يذوب في الخلفية
                alpha = int(a_lo + a_span * depth)
            else:
                col, alpha = mix(dark, idle_c, (0.75 if few else 0.4) + 0.25 * depth), int((150 if few else 60) + 90 * depth)
            (d_near if depth > 0.5 else d_far).ellipse((sx - r, sy - r, sx + r, sy + r), fill=col + (alpha,))
        img.alpha_composite(far.filter(self.ImageFilter.GaussianBlur(radius=(0.9 if few else 1.8) * k)))   # البعيد ضبابي
        img.alpha_composite(near)                                                                  # القريب حاد بلا هالة
        self.photo = self.ImageTk.PhotoImage(img.resize((self.width, self.height), self.Image.LANCZOS))
        return self.photo

    def mic_button(self, color):
        """زر الميكروفون في الرأس: قرص بلون الحالة ورمز ميكروفون أبيض، بدقة مضاعفة."""
        S = self.SS
        n = int(MIC_D * self.k)
        m = n * S
        img = self.Image.new("RGBA", (m, m), rgb(BG) + (255,))
        d = self.ImageDraw.Draw(img)
        d.ellipse((0, 0, m - 1, m - 1), fill=rgb(color))
        c, u = m / 2, m / 26                                                     # وحدة الرسم: الزر 26 وحدة
        d.rounded_rectangle((c - 3 * u, c - 8 * u, c + 3 * u, c + 1 * u), radius=3 * u, fill=(255, 255, 255))
        d.arc((c - 6 * u, c - 5 * u, c + 6 * u, c + 5 * u), start=0, end=180, fill=(255, 255, 255), width=max(1, int(1.5 * u)))
        d.line((c, c + 5 * u, c, c + 8 * u), fill=(255, 255, 255), width=max(1, int(1.5 * u)))
        d.line((c - 3 * u, c + 8 * u, c + 3 * u, c + 8 * u), fill=(255, 255, 255), width=max(1, int(1.5 * u)))
        return self.ImageTk.PhotoImage(img.resize((n, n), self.Image.LANCZOS))


def tray_image(color, size=32):
    """صورة أيقونة شريط المهام: قرص بلون الحالة ورمز ميكروفون أبيض (Pillow)."""
    from PIL import Image, ImageDraw
    m = size * 4
    img = Image.new("RGBA", (m, m), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((0, 0, m - 1, m - 1), fill=rgb(color))
    c, u = m / 2, m / 26
    d.rounded_rectangle((c - 3 * u, c - 8 * u, c + 3 * u, c + 1 * u), radius=3 * u, fill=(255, 255, 255))
    d.arc((c - 6 * u, c - 5 * u, c + 6 * u, c + 5 * u), start=0, end=180, fill=(255, 255, 255), width=max(1, int(1.5 * u)))
    d.line((c, c + 5 * u, c, c + 8 * u), fill=(255, 255, 255), width=max(1, int(1.5 * u)))
    d.line((c - 3 * u, c + 8 * u, c + 3 * u, c + 8 * u), fill=(255, 255, 255), width=max(1, int(1.5 * u)))
    return img.resize((size, size), Image.LANCZOS)


class TextImage:
    """يرسم نصًا عربيًا متعدد الأسطر صورةً: كل كلمة بلونها (منطوقة/جارية/قادمة)، التفاف من اليمين، تشكيل الحروف بـ arabic_reshaper
    وترتيب bidi لكل كلمة؛ لأن Tk Text لا يرتّب العربية عند التلوين الجزئي."""

    def __init__(self, k):
        from PIL import Image, ImageDraw, ImageFont, ImageTk
        self.Image, self.ImageDraw, self.ImageTk = Image, ImageDraw, ImageTk
        self.k = k
        size = max(9, int(round(10 * k)))
        self.fonts = {}
        for weight, name in (("normal", "Cairo-Regular.ttf"), ("bold", "Cairo-Bold.ttf")):
            try:
                self.fonts[weight] = ImageFont.truetype(str(FONT_DIR / name), size)
            except OSError:
                self.fonts[weight] = ImageFont.truetype("segoeui.ttf" if weight == "normal" else "segoeuib.ttf", size)
        self.line_h = int(size * 1.9)
        self.space = max(self.fonts["normal"].getlength(" "), size * 0.42)        # فراغ واضح بين الكلمات؛ مسافة Cairo ضيقة

    @staticmethod
    def shape(word):
        try:
            import arabic_reshaper
            from bidi.algorithm import get_display
            return get_display(arabic_reshaper.reshape(word))
        except Exception:
            return word

    def render(self, words, width, colors, bold_at=-1, max_lines=3):
        """words: قائمة الكلمات بترتيبها المنطقي، colors: لون كل كلمة، bold_at: فهرس الكلمة الغليظة؛ يعيد PhotoImage بعرض width."""
        lines, line, used = [], [], 0.0
        for i, w in enumerate(words):
            font = self.fonts["bold" if i == bold_at else "normal"]
            shaped = self.shape(w)
            wl = font.getlength(shaped)
            if line and used + self.space + wl > width:
                lines.append(line)
                line, used = [], 0.0
            line.append((shaped, font, colors[i]))
            used += (self.space if line[:-1] else 0) + wl
        if line:
            lines.append(line)
        lines = lines[-max_lines:]                                                     # الأسطر الأخيرة إن طال النص
        height = max(self.line_h, self.line_h * len(lines))
        img = self.Image.new("RGBA", (int(width), height), rgb(BG) + (255,))
        d = self.ImageDraw.Draw(img)
        for row, items in enumerate(lines):
            x = width
            for shaped, font, color in items:                                          # من اليمين إلى اليسار
                wl = font.getlength(shaped)
                d.text((x - wl, row * self.line_h + self.line_h * 0.15), shaped, font=font, fill=rgb(color))
                x -= wl + self.space
        return self.ImageTk.PhotoImage(img)


class Tray:
    """أيقونة في شريط المهام بلون الحالة وقائمة يمين: إظهار/إخفاء، تبديل الوضع، تصغير/توسيع، إنهاء؛ تعمل في خيطها (pystray)."""

    def __init__(self, events):
        import pystray
        self.events, self.color = events, IDLE
        menu = pystray.Menu(pystray.MenuItem("إظهار / إخفاء", lambda: events.put("show"), default=True),
                            pystray.MenuItem("تبديل إملاء / محادثة", lambda: events.put("mode")),
                            pystray.MenuItem("تصغير / توسيع", lambda: events.put("compact")),
                            pystray.MenuItem("إنهاء سلمى", lambda: events.put("quit")))
        self.icon = pystray.Icon("salma", tray_image(IDLE), "سلمى", menu)
        threading.Thread(target=self.icon.run, daemon=True).start()

    def update(self, color, title):
        if color != self.color:
            self.color = color
            self.icon.icon = tray_image(color)
        self.icon.title = title[:60]

    def stop(self):
        try:
            self.icon.stop()
        except Exception:
            pass


class Panel:
    """بطاقة مربعة عائمة فوق كل النوافذ لا تأخذ التركيز: رأس فيه × وزر الميكروفون وزر التصغير ومفتاح «إملاء | محادثة»، سحابة الجسيمات
    (تتبع ميكروفونك أثناء الاستماع وصوت الوكيل أثناء النطق)، سطر حالة، نص يضيء كلمةً كلمة مع نطق سلمى، زر يُظهر النص أو يخفيه،
    ومقبض أسفل اليمين للتكبير بالسحب. وضع مصغّر: كبسولة صغيرة فيها الميكروفون وسحابة صغيرة (زر – أو Ctrl+Alt+F11).
    تخفت بعد سكون طويل وتعود مع الصوت أو الفأرة. أيقونة شريط المهام تعكس الحالة."""

    def __init__(self, app, events):
        import tkinter
        saved = self.saved()
        self.app, self.events, self.grab, self.shown, self.resizing = app, events, None, None, None
        self.shown_mode, self.bars, self.phase, self.mic_photos = None, [0.0] * BARS, 0.0, {}
        self.show_text, self.s = bool(saved.get("text", True)), min(SCALE_MAX, max(SCALE_MIN, float(saved.get("scale", 1.0))))
        self.compact, self.hover, self.last_active, self.alpha = bool(saved.get("compact", False)), False, time.time(), 0.97
        self.said_key = None
        root = self.root = tkinter.Tk()
        root.title("سلمى")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", self.alpha)
        root.configure(bg=BG)
        self.cv = tkinter.Canvas(root, bg=BG, highlightthickness=0)
        self.cv.pack()
        self.said = tkinter.Label(root, bg=BG, bd=0, cursor="fleur")
        self.said_photo = None
        self.build()
        self.cv.bind("<Button-1>", self.press)
        self.cv.bind("<B1-Motion>", self.drag)
        self.cv.bind("<ButtonRelease-1>", self.release)
        self.said.bind("<Button-1>", lambda e: (self.hold(e), "break")[1])
        self.said.bind("<B1-Motion>", lambda e: (self.drag(e), "break")[1])
        self.said.bind("<ButtonRelease-1>", self.release)
        root.bind("<Enter>", lambda _: self.touch(True))
        root.bind("<Leave>", lambda _: self.touch(False))
        root.update_idletasks()
        root.geometry("+%d+%d" % self.place(root.winfo_reqwidth(), root.winfo_reqheight()))
        root.update()
        self.hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
        style = user32.GetWindowLongPtrW(self.hwnd, GWL_EXSTYLE)
        user32.SetWindowLongPtrW(self.hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        corner = ctypes.c_int(DWMWCP_ROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(wintypes.HWND(self.hwnd), DWMWA_CORNER, ctypes.byref(corner), ctypes.sizeof(corner))
        try:
            self.tray = Tray(events)
        except Exception:
            logging.exception("أيقونة شريط المهام")
            self.tray = None
        self.tick()

    def font(self, points, weight="normal"):
        return (FONT, max(7, int(round(points * self.s))), weight)

    def build(self):
        """يرسم كل عناصر البطاقة بالمقياس s (تصميم 200×200)، أو الكبسولة المصغّرة؛ يُستدعى عند البدء وبعد كل تغيير حجم أو وضع."""
        cv, k = self.cv, self.s
        cv.delete("all")
        self.font_small, self.font_bold = self.font(9), self.font(9, "bold")
        self.mic_photos = {}
        if self.compact:
            self.build_compact()
        else:
            self.build_full()
        self.text_image = TextImage(self.s)
        self.said.pack_forget()
        if self.show_text and not self.compact:
            self.said.pack(fill="x", padx=12, pady=(0, 10))
        self.shown = self.shown_mode = self.said_key = None                               # لتطبيق الحالة والوضع على العناصر الجديدة

    def build_full(self):
        cv, k = self.cv, self.s
        size = int(CARD * k)
        cv.configure(width=size, height=size)
        cv.create_text(14 * k, 19 * k, text="×", fill=DIM, font=self.font(13), tags=("close", "btn"))
        self.orb = Wave(k)
        self.mic_item = cv.create_image(40 * k, 19 * k, image=self.mic_photo(IDLE), tags=("mic", "btn"))
        cv.create_text(64 * k, 17 * k, text="–", fill=DIM, font=self.font(14, "bold"), tags=("compact", "btn"))
        x0, y0, w, h = (CARD - SWITCH_W - 10) * k, 7 * k, SWITCH_W * k, SWITCH_H * k
        rounded(cv, x0, y0, x0 + w, y0 + h, h / 2, fill=IDLE, tags=("mode", "btn"))
        self.knob = rounded(cv, x0, y0, x0 + w / 2, y0 + h, h / 2, fill=CHAT, tags=("mode", "btn"))
        self.seg_dictate = cv.create_text(x0 + w * 3 / 4, y0 + h / 2 - 1 * k, text="إملاء", fill=INK, font=self.font_bold, tags=("mode", "btn"))
        self.seg_chat = cv.create_text(x0 + w / 4, y0 + h / 2 - 1 * k, text="محادثة", fill=DIM, font=self.font_small, tags=("mode", "btn"))
        self.orb_item = cv.create_image(CARD / 2 * k, WAVE_Y * k, image=self.orb.render(self.bars, IDLE, 0.0, 0.0), tags=("orb", "btn"))
        self.status = cv.create_text(CARD / 2 * k, (CARD - 31) * k, text="", fill=INK, font=self.font_small, width=(CARD - 16) * k, justify="center")
        self.textbtn = rounded(cv, 10 * k, (CARD - 24) * k, 52 * k, (CARD - 6) * k, 9 * k, fill=IDLE if self.show_text else BG, tags=("text", "btn"))
        cv.create_text(31 * k, (CARD - 16) * k, text="النص", fill=INK, font=self.font(8), tags=("text", "btn"))
        grip = 18 * k                                                                   # مقبض الحجم: مثلث أسفل اليمين بثلاثة خطوط
        cv.create_polygon(size, size - grip, size, size, size - grip, size, fill=BG, outline="", tags=("resize",))
        for i in range(3):
            d = (6 + 5 * i) * k
            cv.create_line(size - d, size - 3 * k, size - 3 * k, size - d, fill=DIM, width=1, tags=("resize",))

    def build_compact(self):
        """الكبسولة: زر الميكروفون يمينًا، سحابة صغيرة في الوسط، زر التوسيع يسارًا؛ بلا نص ولا مقبض."""
        cv, k = self.cv, self.s
        w, h = int(COMPACT_W * k), int(COMPACT_H * k)
        cv.configure(width=w, height=h)
        self.orb = Wave(k, COMPACT_W - 70, COMPACT_H - 8)
        self.mic_item = cv.create_image(w - 19 * k, h / 2, image=self.mic_photo(IDLE), tags=("mic", "btn"))
        self.orb_item = cv.create_image(w / 2 + 2 * k, h / 2, image=self.orb.render(self.bars, IDLE, 0.0, 0.0), tags=("orb", "btn"))
        cv.create_text(14 * k, h / 2 - 1 * k, text="▢", fill=DIM, font=self.font(11), tags=("compact", "btn"))
        self.status = cv.create_text(-100, -100, text="")                               # خارج الكبسولة؛ الحالة تُقرأ من لون الميكروفون وأيقونة الشريط

    def mic_photo(self, color):
        if color not in self.mic_photos:
            self.mic_photos[color] = self.orb.mic_button(color)
        return self.mic_photos[color]

    def set_compact(self, compact):
        if compact == self.compact:
            return
        self.remember()                                                                  # احفظ موضع الوضع الحالي قبل التبديل
        self.compact = compact
        self.build()
        self.root.update_idletasks()
        self.root.geometry("+%d+%d" % self.place(self.root.winfo_reqwidth(), self.root.winfo_reqheight()))
        self.remember()

    def touch(self, hover=None):
        if hover is not None:
            self.hover = hover
        self.last_active = time.time()

    def press(self, event):
        """نقرة على عنصر موسوم تنفّذ فعله؛ على المقبض تبدأ تغيير الحجم؛ على الفراغ تبدأ السحب."""
        self.touch()
        tags = self.cv.gettags("current")
        if "resize" in tags:
            self.resizing = (event.x_root, event.y_root, self.s)
        elif "orb" in tags or "mic" in tags:
            self.app.toggle()
        elif "mode" in tags:
            wanted = "chat" if event.x < (CARD - SWITCH_W - 10 + SWITCH_W / 2) * self.s else "dictate"
            if wanted != self.app.mode:
                self.app.switch_mode()
        elif "close" in tags:
            self.hide()
        elif "compact" in tags:
            self.set_compact(not self.compact)
        elif "text" in tags:
            self.toggle_text()
        else:
            self.hold(event)

    def toggle_text(self):
        if self.compact:
            return
        self.show_text = not self.show_text
        if self.show_text:
            self.said.pack(fill="x", padx=12, pady=(0, 10))
        else:
            self.said.pack_forget()
        self.cv.itemconfigure(self.textbtn, fill=IDLE if self.show_text else BG)
        self.remember()

    def saved(self):
        try:
            return json.loads(PLACE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def place(self, width, height):
        """آخر مكان محفوظ لهذا الوضع إن كان داخل الشاشات الحالية؛ وإلا أسفل المنتصف للبطاقة، وأسفل اليمين فوق شريط المهام للكبسولة."""
        left, top = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
        right, bottom = left + user32.GetSystemMetrics(78), top + user32.GetSystemMetrics(79)
        key = "compact_pos" if self.compact else "pos"
        try:
            x, y = (int(v) for v in self.saved()[key])
            if left <= x <= right - width and top <= y <= bottom - height:
                return x, y
        except (ValueError, KeyError, TypeError):
            pass
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        if self.compact:
            return sw - width - 16, sh - height - 64
        return (sw - width) // 2, sh - height - 90

    def remember(self):
        data = self.saved()
        data.update({"text": self.show_text, "scale": round(self.s, 2), "compact": self.compact})
        data["compact_pos" if self.compact else "pos"] = [self.root.winfo_x(), self.root.winfo_y()]
        data.pop("x", None), data.pop("y", None)
        PLACE_FILE.write_text(json.dumps(data), encoding="utf-8")

    def release(self, _):
        if self.grab or self.resizing:
            self.remember()
        self.grab = self.resizing = None

    def hide(self):
        user32.ShowWindow(self.hwnd, SW_HIDE)

    def show(self):
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
        self.touch()

    def hold(self, event):
        self.grab = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def drag(self, event):
        self.touch()
        if self.resizing:
            x, y, s0 = self.resizing
            wanted = min(SCALE_MAX, max(SCALE_MIN, s0 + max(event.x_root - x, event.y_root - y) / CARD))
            if abs(wanted - self.s) >= 0.03:
                self.s = wanted
                self.build()
        elif self.grab:
            self.root.geometry("+%d+%d" % (event.x_root - self.grab[0], event.y_root - self.grab[1]))

    def quit(self):
        if self.tray:
            self.tray.stop()
        self.app.speaker.stop_silma()
        self.root.destroy()
        os._exit(0)

    def tick(self):
        try:
            self.refresh()
        except Exception:
            logging.exception("خلل في تحديث النافذة")
        finally:
            self.root.after(40, self.tick)

    def animate(self, color):
        """انتشار السحابة: من غلاف صوت الوكيل أثناء النطق (أولًا، لأن الميكروفون يبقى مفتوحًا في المحادثة)،
        ومن الميكروفون أثناء الاستماع، وموجة بطيئة أثناء التفكير، ونبض خفيف عند السكون."""
        self.phase += 0.18
        if self.app.speaker.speaking:
            level, spread = self.app.speaker.level(), 0.45
        elif self.app.recorder.active:
            decibels = 20 * math.log10(self.app.recorder.live() + 1e-6)
            level, spread = min(1.0, max(0.0, (decibels + 58) / 42)), 0.5
        elif self.app.thinking:
            level, spread = 0.35, 1.0
        else:
            level, spread = 0.0, 0.0
        if level > 0.08:
            self.last_active = time.time()
        wave = 0.5 + 0.5 * math.sin(self.phase * (1.1 if self.app.thinking else 1.9))             # انتشار السحابة يتبع المستوى بنبض خفيف
        if self.app.thinking and not self.app.speaker.speaking:
            target = 0.25 + 0.75 * level * wave
        else:
            target = (0.12 + 0.88 * level * (1 - spread * 0.3 + spread * 0.3 * wave)) if level else 0.05 + 0.03 * (1 + math.sin(self.phase * 0.4))
        self.bars[0] += (target - self.bars[0]) * (0.75 if target > self.bars[0] else 0.4)        # تطاير فوري مع المقطع وانكماش سريع بعده
        self.cv.itemconfigure(self.orb_item, image=self.orb.render(self.bars, color if level else IDLE, level, self.phase))

    def fade(self):
        """تلاشٍ تلقائي: بعد FADE_AFTER ثانية بلا صوت ولا فأرة تخفت البطاقة إلى FADE_ALPHA وتعود تدريجيًا مع أي نشاط."""
        target = 0.97 if self.hover or time.time() - self.last_active < FADE_AFTER else FADE_ALPHA
        if abs(target - self.alpha) > 0.005:
            self.alpha += (target - self.alpha) * 0.2
            self.root.attributes("-alpha", round(self.alpha, 3))

    def karaoke(self):
        """نص سلمى يضيء كلمةً كلمة: تقدّم الطاقة في غلاف الجملة يحدد الكلمة المنطوقة الآن؛ وإلا يظهر آخر نص باهتًا."""
        speaker = self.app.speaker
        if speaker.speaking and speaker.now_text:
            words = speaker.now_text.split()
            total = sum(len(w) for w in words) or 1
            progress = speaker.progress()
            done, acc = 0, 0
            for i, w in enumerate(words):
                acc += len(w)
                if acc / total <= progress:
                    done = i + 1
            key = (speaker.now_text, min(done, len(words) - 1))
            if key != self.said_key:
                self.said_key = key
                now = key[1]
                accent = CHAT if self.app.mode == "chat" else "#34a853"
                colors = [INK if i < now else accent if i == now else DIM for i in range(len(words))]
                self.said_photo = self.text_image.render(words, (CARD - 24) * self.s, colors, bold_at=now)
                self.said.configure(image=self.said_photo)
        else:
            key = ("plain", self.app.last_text[-120:])
            if key != self.said_key:
                self.said_key = key
                words = key[1].split() or [""]
                self.said_photo = self.text_image.render(words, (CARD - 24) * self.s, [DIM] * len(words))
                self.said.configure(image=self.said_photo)

    def refresh(self):
        self.ticks = getattr(self, "ticks", 0) + 1
        if self.ticks % TOPMOST_EVERY == 0 and user32.IsWindowVisible(self.hwnd):
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        while not self.events.empty():
            event = self.events.get()
            if event == "toggle":
                self.show()
                self.app.toggle()
            elif event == "mode":
                self.show()
                self.app.switch_mode()
            elif event == "compact":
                self.show()
                self.set_compact(not self.compact)
            elif event == "show":
                self.hide() if user32.IsWindowVisible(self.hwnd) else self.show()
            elif event == "quit":
                self.quit()
            else:
                self.app.tell(event, 8)
        if self.app.due():
            self.app.ask()
        if self.ticks % 25 == 0 and SAY_FILE.exists():                                   # نطق نص من ملف say.txt (كل ثانية)
            try:
                text = SAY_FILE.read_text(encoding="utf-8").strip()
                SAY_FILE.unlink()
            except OSError:
                text = ""
            if text:
                logging.info("نطق من say.txt: %d حرفًا", len(text))
                self.app.tell("أنطق نصًا من say.txt")
                self.app.speaker.say(text)
        if self.app.recorder.idle or (self.app.want_stop and self.app.recorder.active):
            self.app.tell("توقفت بأمرك" if self.app.want_stop else "أوقفت الاستماع بعد صمت طويل")
            self.app.want_stop = False
            self.app.toggle()
        state = self.app.status()
        if state != self.shown:
            self.shown = state
            self.touch()
            self.cv.itemconfigure(self.status, text=state[0])
            mic = LISTEN if self.app.recorder.active else state[1]
            self.cv.itemconfigure(self.mic_item, image=self.mic_photo(mic))
            if self.tray:
                self.tray.update(mic, "سلمى: " + state[0])
        if self.app.mode != self.shown_mode:
            self.shown_mode = self.app.mode
            chat, k = self.app.mode == "chat", self.s
            self.said_key = None                                                           # لون الكلمة الجارية يتبع الوضع
            if not self.compact:
                x0, y0, w, h = (CARD - SWITCH_W - 10) * k, 7 * k, SWITCH_W * k, SWITCH_H * k
                self.cv.coords(self.knob, *rounded_points(x0 if chat else x0 + w / 2, y0, x0 + w / 2 if chat else x0 + w, y0 + h, h / 2))
                self.cv.itemconfigure(self.knob, fill=CHAT if chat else "#5f6368")
                self.cv.itemconfigure(self.seg_chat, fill=INK if chat else DIM, font=self.font_bold if chat else self.font_small)
                self.cv.itemconfigure(self.seg_dictate, fill=DIM if chat else INK, font=self.font_small if chat else self.font_bold)
        if self.show_text and not self.compact:
            self.karaoke()
        self.animate(state[1] if state[1] != IDLE else CHAT if self.app.mode == "chat" else "#34a853")
        self.fade()


def listen_hotkey(events):
    """الاختصار مربوط بالخيط الذي يسجله، فله حلقة رسائله."""
    if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_F9):
        logging.error("الاختصار محجوز من برنامج آخر")
        events.put("الاختصار محجوز، استعمل الزر")
        return
    if not user32.RegisterHotKey(None, HOTKEY_ID + 1, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_F10):
        logging.error("اختصار التبديل Ctrl+Alt+F10 محجوز من برنامج آخر")
        events.put("اختصار التبديل محجوز، قل «محادثة» أو «إملاء»")
    if not user32.RegisterHotKey(None, HOTKEY_ID + 2, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_F11):
        logging.error("اختصار التصغير Ctrl+Alt+F11 محجوز من برنامج آخر")
    message = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
        if message.message == WM_HOTKEY:
            events.put({HOTKEY_ID + 1: "mode", HOTKEY_ID + 2: "compact"}.get(message.wParam, "toggle"))


def main():
    mutex = kernel32.CreateMutexW(None, False, "Local\\VoiceDictateMesue")
    if ctypes.get_last_error() == 183 or not mutex:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except OSError:
        pass
    app, events = App(), queue.Queue()
    threading.Thread(target=app.warm, daemon=True).start()
    threading.Thread(target=listen_hotkey, args=(events,), daemon=True).start()
    Panel(app, events).root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--file":
        began = time.time()
        loaded = load_model()
        print("load", round(time.time() - began, 1))
        began = time.time()
        text, logprob, no_speech = transcribe(loaded, read_wav(sys.argv[2]), load_prompt())
        print(tidy(text), "ثقة", round(logprob, 2), "لا كلام", round(no_speech, 2))
        print("transcribe", round(time.time() - began, 1))
    else:
        main()
