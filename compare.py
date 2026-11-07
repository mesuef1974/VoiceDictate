"""مقارنة النموذجين بصوتك: تقرأ كل جملة بعد Enter، فيفرّغها النموذجان، ويُطبع جدول ونسبة الخطأ.

التشغيل: compare.cmd (أغلق أداة الإملاء أولًا لتتسع ذاكرة كرت الشاشة). التسجيلات لا تُحفظ.
"""
import importlib.machinery
import importlib.util
import pathlib
import re
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
ARABIC = HERE / "models" / "turbo-arabic"
PHRASES = [
    "كم المساحة الحرة في القرص الآن، وهل يوجد ما يبطئ الجهاز؟",
    "افتح بايونيك واسأل المرصد عن حمل المعالج وذاكرة كرت الشاشة.",
    "النموذج المحمّل هو كوين ثلاثة بحجم ثمانية مليارات، وسياقه أربعة وعشرون ألفًا.",
    "أرسل التقرير إلى المدرسة قبل الساعة الثالثة بعد الظهر يوم الخميس.",
    "لماذا ارتفع الالتزام إلى اثنين وثمانين بالمئة بعد تشغيل التنظيف الشهري؟",
]

loader = importlib.machinery.SourceFileLoader("dictate", str(HERE / "dictate.pyw"))
spec = importlib.util.spec_from_loader("dictate", loader)
d = importlib.util.module_from_spec(spec)
loader.exec_module(d)


def norm(text):
    text = re.sub("[ً-ْٰـ]", "", text)
    text = re.sub("[أإآ]", "ا", text).replace("ة", "ه").replace("ى", "ي")
    return re.sub(r"[^\w\s]", "", text).split()


def wer(ref, hyp):
    r, h = norm(ref), norm(hyp)
    prev = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        cur = [i]
        for j, hw in enumerate(h, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rw != hw)))
        prev = cur
    return prev[-1] / max(1, len(r))


def record(seconds_max=15):
    import numpy
    import sounddevice
    input("اضغط Enter ثم اقرأ الجملة، واضغط Enter عند الانتهاء... ")
    chunks = []
    stream = sounddevice.InputStream(samplerate=d.RATE, channels=1, dtype="float32", callback=lambda data, *_: chunks.append(data.copy()))
    stream.start()
    input()
    stream.stop()
    stream.close()
    audio = numpy.concatenate(chunks)[:, 0] if chunks else numpy.zeros(d.RATE, dtype="float32")
    peak = float(abs(audio).max())
    if peak > 0:
        audio = audio * min(d.MAX_GAIN, max(1.0, d.TARGET_PEAK / peak))
    return audio[: d.RATE * seconds_max], peak


def main():
    from faster_whisper import WhisperModel
    device = "cuda" if d.cuda_dirs() else "cpu"
    for folder in d.cuda_dirs():
        d.os.add_dll_directory(str(folder))
        d.os.environ["PATH"] = str(folder) + d.os.pathsep + d.os.environ["PATH"]
    compute = "int8_float16" if device == "cuda" else "int8"
    prompt = d.load_prompt()
    print("يحمّل النموذجين على", "كرت الشاشة" if device == "cuda" else "المعالج", "...")
    try:
        models = {"الحالي": WhisperModel(d.MODEL, device=device, compute_type=compute, local_files_only=True),
                  "العربي": WhisperModel(str(ARABIC), device=device, compute_type=compute, local_files_only=True)}
    except (RuntimeError, ValueError, OSError) as e:
        print("تعذر التحميل على كرت الشاشة (أغلق أداة الإملاء أولًا):", str(e)[:160])
        return
    totals = dict.fromkeys(models, 0.0)
    rows = []
    for i, ref in enumerate(PHRASES, 1):
        print("\n[%d/%d] %s" % (i, len(PHRASES), ref))
        audio, peak = record()
        print("قمة الصوت %.3f%s" % (peak, "  (ضعيف جدًا)" if peak < d.WEAK_PEAK else ""))
        row = {"ref": ref}
        for name, model in models.items():
            began = time.time()
            text, lp, ns = d.transcribe(model, audio, prompt)
            text = d.tidy(text)
            e = wer(ref, text)
            totals[name] += e
            row[name] = (text, e, time.time() - began)
            print("  %-7s %3.0f%%  %s" % (name, e * 100, text))
        rows.append(row)
    print("\nالمتوسط:")
    for name in models:
        print("  %-7s WER %.1f%%" % (name, totals[name] / len(PHRASES) * 100))
    best = min(totals, key=totals.get)
    print("\nالأفضل بصوتك:", best)
    out = HERE / "compare_result.txt"
    with out.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(row["ref"] + "\n")
            for name in models:
                fh.write("  %s: %.0f%% %s\n" % (name, row[name][1] * 100, row[name][0]))
        fh.write("المتوسط: " + "، ".join("%s %.1f%%" % (n, totals[n] / len(PHRASES) * 100) for n in models) + "\n")
    print("حُفظت النتيجة في", out)
    input("\nاضغط Enter للخروج.")


if __name__ == "__main__":
    sys.exit(main())
