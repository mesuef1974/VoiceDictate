"""خادم نطق دائم بصوت SILMA على كرت الشاشة؛ البروتوكول نفسه الذي تستعمله أداة الإملاء مع speak.ps1:
يقرأ من stdin سطرًا «مسار_wav<TAB>نص base64<TAB>سرعة» ويكتب الملف ثم يطبع «ok مسار» أو «err سبب». يطبع «ready silma» بعد التحميل والإحماء."""
import base64
import os
import pathlib
import re
import sys
import time

HERE = pathlib.Path(__file__).parent
sys.path.append(str(HERE / "stubs"))      # بدائل للحزم التي لا تُبنى على ويندوز؛ في آخر المسار فلا تحجب الحقيقية
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
sys.stdin.reconfigure(encoding="utf-8")

import soundfile  # noqa: E402
import torch  # noqa: E402
import torchaudio  # noqa: E402


def load_with_soundfile(path, *args, **kwargs):
    """torchaudio.load هنا يمرّ بـ torchcodec الذي يحتاج FFmpeg مشتركًا غير متوفر؛ soundfile يكفي لملف wav."""
    data, sr = soundfile.read(str(path), dtype="float32", always_2d=True)
    return torch.from_numpy(data.T.copy()), sr


torchaudio.load = load_with_soundfile
REF = HERE / ".venv" / "Lib" / "site-packages" / "silma_tts" / "infer" / "ref_audio_samples" / "ar.ref.24k.wav"
REF_TEXT = "ويدقق النظر في القرآن الكريم وسائر الكتب السماوية ويتبع مسالك الرسل العظام عليهم الصلاة والسلام."
DEVICE = sys.argv[1] if len(sys.argv) > 1 else "cuda"
NFE_STEP = int(os.environ.get("SILMA_NFE", "32"))    # خطوات التوليد: الافتراضي في المكتبة 16 (أسرع وأخشن)؛ 32 أنقى لفظًا بضعف الزمن
TASHKEEL = re.compile(r"[ً-ْ]")
PACE = float(os.environ.get("SILMA_PACE", "0.85"))   # إيقاع القراءة نسبةً إلى سرعة المتكلم المرجعي (12.4 حرفًا/ث)؛ أقل = أهدأ.
# مسح 7/11/2026 على 6 جمل بـ Whisper: 1.0 و0.9 تبتلعان كلمات أول الجملة، 0.85 صفر أخطاء في الست، 0.7-0.8 أخطاء صغيرة متفرقة.
MARGIN = int(os.environ.get("SILMA_MARGIN", "4"))     # محارف هامش ثابتة تُضاف إلى المدة (≈0.08 ث للمحرف) كي لا يُقصّ آخر الجملة
MAX_CHARS = int(os.environ.get("SILMA_CHUNK", "260"))  # المكتبة تقطع النص عند 120 محرفًا (بالحركات ≈ 70 حرفًا) فتُقسم الجملة الطويلة بوقفة
#                                                        وتوليدَين؛ الأداة ترسل جملةً جملة أصلًا، فنرفع الحد كي لا تُقطع الجملة أبدًا


def pace(text):
    """المكتبة تحسب مدة الصوت من عدد المحارف، والحركات محارف: نص مشكَّل بالكامل يحمل نحو 1.7 محرفًا لكل حرف فتطول المدة
    1.7 مرة فتمطّ سلمى الحروف وتتلعثم. المكتبة تعوّض ذلك تقريبًا بضرب السرعة في 1.3 إن تجاوزت الحركات 20%؛ هنا نعوّض بالنسبة
    الدقيقة لهذا النص بالذات فتصبح المدة = عدد الحروف ÷ سرعة المرجع، ونلغي عامل المكتبة التقريبي كي لا يُضاف فوقها."""
    bare = len(TASHKEEL.sub("", text))
    library = 1.3 if len(TASHKEEL.findall(text)) > 0.2 * len(text) else 1.0
    return PACE * len(text) / (bare + MARGIN) / library


def main():
    if DEVICE == "cuda" and not torch.cuda.is_available():
        print("err no cuda", flush=True)
        return 1
    (HERE / "server.pid").write_text(str(os.getpid()), encoding="utf-8")
    out = sys.stdout                                                     # قناة البروتوكول وحدها
    log = open(HERE / "server.err", "a", encoding="utf-8")               # noqa: SIM115  يبقى مفتوحًا طوال عمر الخادم
    sys.stdout = sys.stderr = log                                        # المكتبات تطبع كثيرًا على stdout؛ كله إلى السجل
    from silma_tts.api import SilmaTTS
    from silma_tts.infer import utils_infer
    chunk_text = utils_infer.chunk_text
    utils_infer.chunk_text = lambda text, max_chars=120: chunk_text(text, max_chars=max(max_chars, MAX_CHARS))
    tts = SilmaTTS(device=DEVICE, hf_cache_dir=str(HERE / "models"))
    began = time.time()
    tts.infer(ref_file=str(REF), ref_text=REF_TEXT, gen_text="جاهز.", file_wave=str(HERE / "warmup.wav"), seed=1, speed=1.0, show_info=lambda *a, **k: None, progress=None)
    print("ready silma (warmup %.1fs)" % (time.time() - began), file=out, flush=True)
    for line in sys.stdin:
        parts = line.rstrip("\r\n").split("\t", 2)
        if len(parts) < 2:
            continue
        path, speed = parts[0], float(parts[2]) if len(parts) > 2 and parts[2] else 1.0
        try:
            text = base64.b64decode(parts[1]).decode("utf-8")
            speed *= pace(text)
            tts.infer(ref_file=str(REF), ref_text=REF_TEXT, gen_text=text, file_wave=path, seed=1, speed=speed, nfe_step=NFE_STEP,
                      show_info=lambda *a, **k: None, progress=None)
            print("ok " + path, file=out, flush=True)
        except Exception as e:  # noqa: BLE001  أي خلل في جملة لا يُسقط الخادم؛ الأداة تعود إلى كريم لتلك الجملة
            print("err " + str(e).replace("\n", " ")[:200], file=out, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
