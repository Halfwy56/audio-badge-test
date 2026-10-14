#!/usr/bin/env python3
"""audiokit — проверка и выгрузка записей с аудиобейджа (только stdlib; numpy ускоряет, если есть).

  python tools/audiokit.py gen    out.wav [--defect clip|silence|dc|quiet|none] [--sec 10] [--rate 16000]
  python tools/audiokit.py check  file.wav [...] [--json]
  python tools/audiokit.py upload file.wav [...] [--url http://127.0.0.1:8799] [--chunk 65536] [--check]

Код возврата check:  0 — OK, 1 — есть предупреждения, 2 — хотя бы один файл непригоден.
Код возврата upload: 0 — все файлы приняты (sha256_ok), 2 — хотя бы один не выгружен.
Инструмент никогда не падает трейсбеком на плохом входе: ошибка попадает в status/issues.
"""
import argparse, array, hashlib, json, math, os, random, sys, urllib.error, urllib.parse, urllib.request, wave

try:
    import numpy as np                   # необязательно: ускоряет check в ~50 раз на длинных файлах
except ImportError:                      # pragma: no cover
    np = None

# Пороги вынесены сюда, чтобы агент и человек меняли их в одном месте. Таблица в skill audio-qa — отсюда.
EXPECTED_RATES = (16000, 48000)      # Omi: 16 кГц Opus; 48 кГц — типичный PDM/I2S поток
CLIP_LEVEL = 32767 - 64              # |x| >= этого считаем клипом (16 бит)
CLIP_MAX_RATIO = 0.001               # >0.1 % клипнутых сэмплов — warning
SILENCE_DBFS = -60.0                 # окно тише этого — тишина
SILENCE_MAX_RATIO = 0.9              # >90 % тишины — запись, скорее всего, пустая
DC_MAX = 0.02                        # смещение >2 % FS — проблема микрофона/АЦП
LOW_PEAK_DBFS = -30.0                # пик тише этого при не-пустой записи — микрофон закрыт/далеко
WIN_MS = 50
MAX_RETRIES = 3                      # докачек подряд при обрыве связи


def dbfs(rms):
    return 20 * math.log10(rms) if rms > 0 else -120.0


def gen(path, defect="none", sec=10.0, rate=16000):
    """Синтетическая «речь»: смесь гармоник с АМ-огибающей + шум, плюс выбранный дефект."""
    rnd = random.Random(42)
    n = int(sec * rate)
    data = array.array("h")
    for i in range(n):
        t = i / rate
        env = 0.5 + 0.5 * math.sin(2 * math.pi * 3 * t)          # ~3 слога/с
        f0 = 140 + 20 * math.sin(2 * math.pi * 0.5 * t)
        s = env * sum(math.sin(2 * math.pi * f0 * k * t) / k for k in (1, 2, 3, 4))
        s = 0.25 * s + 0.01 * rnd.uniform(-1, 1)
        if defect == "clip":
            s *= 6                                                 # перегруз усилителя
        elif defect == "silence" and t > sec * 0.05:
            s = 0.0002 * rnd.uniform(-1, 1)                        # микрофон «отвалился»
        elif defect == "dc":
            s += 0.1
        elif defect == "quiet":
            s *= 0.02                                              # микрофон под одеждой: −34 dBFS пик
        data.append(max(-32768, min(32767, int(s * 32767))))
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(data.tobytes())
    print(f"written {path}: {sec}s @ {rate} Hz, defect={defect}")


def _metrics(s, rate):
    """peak, mean, rms, clipped, silence_ratio по массиву int16 (numpy, если доступен)."""
    win = max(1, rate * WIN_MS // 1000)
    n_w = len(s) // win
    if np is not None:
        a = np.frombuffer(s.tobytes(), dtype="<i2").astype(np.float64)
        peak = float(np.abs(a).max()); mean = float(a.mean())
        rms = float(np.sqrt(np.mean(a * a)))
        clipped = int((np.abs(a) >= CLIP_LEVEL).sum())
        if n_w:
            w = a[:n_w * win].reshape(n_w, win)
            wrms = np.sqrt(np.mean(w * w, axis=1)) / 32768
            silent = int((wrms < 10 ** (SILENCE_DBFS / 20)).sum())
        else:
            silent = 0
        return peak, mean, rms, clipped, silent, n_w
    total = len(s)
    sq = array.array("d", (x * x for x in s))                     # один проход; дальше срезы — в C
    peak = max(abs(max(s)), abs(min(s))); mean = sum(s) / total
    rms = math.sqrt(sum(sq) / total)
    clipped = sum(1 for x in s if x >= CLIP_LEVEL or x <= -CLIP_LEVEL)
    thr = (10 ** (SILENCE_DBFS / 20) * 32768) ** 2 * win         # сравниваем сумму квадратов, без sqrt/log
    silent = sum(1 for i in range(0, n_w * win, win) if sum(sq[i:i + win]) < thr)
    return peak, mean, rms, clipped, silent, n_w


def check(path):
    issues, warns = [], []
    info = {"file": os.path.basename(path)}
    try:
        with wave.open(path, "rb") as w:
            ch, sw, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = w.readframes(n)
    except (wave.Error, EOFError, OSError, ValueError) as e:      # не WAV / обрезан / нет файла / не PCM
        why = str(e) or {"EOFError": "файл обрезан или пустой"}.get(e.__class__.__name__, e.__class__.__name__)
        return {**info, "status": "fail", "issues": [f"не удалось прочитать как WAV: {why}"], "warnings": []}
    info.update(channels=ch, sample_width_bits=sw * 8, sample_rate=rate,
                duration_s=round(n / rate, 3) if rate else 0)
    if sw != 2:
        issues.append(f"ожидается 16 бит PCM, получено {sw*8} бит")
    if rate not in EXPECTED_RATES:
        warns.append(f"частота {rate} Гц не из {EXPECTED_RATES} — backend будет ресемплировать")
    if n == 0:
        issues.append("файл пустой")
    if issues:
        return {**info, "status": "fail", "issues": issues, "warnings": warns}

    s = array.array("h", raw[:len(raw) // 2 * 2])
    if ch > 1:
        s = s[0::ch]                                               # анализируем первый канал
    peak, mean, rms, clipped, silent_w, n_w = _metrics(s, rate)
    info.update(peak_dbfs=round(dbfs(peak / 32768), 2), rms_dbfs=round(dbfs(rms / 32768), 2),
                dc_offset=round(mean / 32768, 4), clipped_ratio=round(clipped / len(s), 5),
                silence_ratio=round(silent_w / n_w, 3) if n_w else 1.0)

    if info["clipped_ratio"] > CLIP_MAX_RATIO:
        warns.append(f"клиппинг {info['clipped_ratio']:.2%} сэмплов — снизить PGA/gain")
    if info["silence_ratio"] > SILENCE_MAX_RATIO:
        warns.append(f"тишина {info['silence_ratio']:.0%} записи — проверить микрофон/VAD")
    elif info["peak_dbfs"] < LOW_PEAK_DBFS:
        warns.append(f"пик {info['peak_dbfs']} dBFS слишком тихий — микрофон закрыт/акустический порт")
    if abs(info["dc_offset"]) > DC_MAX:
        warns.append(f"DC-смещение {info['dc_offset']:+.3f} FS — нужен HPF/проверка АЦП")
    status = "warn" if warns else "ok"
    return {**info, "status": status, "issues": issues, "warnings": warns}


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _call(req, timeout=10):
    """Один HTTP-вызов → dict. HTTP 4xx/5xx и сетевые ошибки пробрасываются как OSError."""
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def upload_one(path, url, chunk=65536, _restart=True):
    """Чанковая выгрузка с докачкой: спрашиваем у backend offset, шлём остаток файлом, не в памяти."""
    if not os.path.isfile(path):
        print(f"  no such file: {path}"); return 2
    sha = _sha256_file(path)
    size = os.path.getsize(path)
    name = urllib.parse.quote(os.path.basename(path))             # пробелы/кириллица в имени
    base = f"{url.rstrip('/')}/uploads/{sha}"

    def server_offset():
        return _call(urllib.request.Request(f"{base}?name={name}&size={size}", method="PUT"))["offset"]

    try:
        offset = server_offset()
    except OSError as e:                                           # backend недоступен — без трейсбека
        print(f"  backend unreachable ({e.__class__.__name__}: {e}) — upload not started"); return 2
    if offset:
        print(f"  resume from offset {offset}/{size}")
    retries = 0
    with open(path, "rb") as f:
        while offset < size:
            f.seek(offset)
            part = f.read(chunk)
            r = urllib.request.Request(f"{base}/chunk?offset={offset}", data=part, method="POST",
                                       headers={"Content-Type": "application/octet-stream"})
            try:
                offset = _call(r)["offset"]
                retries = 0
                print(f"  sent {offset}/{size}")
            except (OSError, ValueError, KeyError) as e:           # обрыв связи или 409: спросить offset и докачать
                retries += 1
                if retries > MAX_RETRIES:
                    print(f"  giving up after {MAX_RETRIES} retries: {e}"); return 2
                try:
                    offset = server_offset()
                except OSError as e2:
                    print(f"  connection lost ({e2.__class__.__name__}), backend still unreachable"); continue
                print(f"  connection lost ({e.__class__.__name__}), resume from {offset}")
    try:
        res = _call(urllib.request.Request(f"{base}/complete", method="POST", data=b""), timeout=60)
    except OSError as e:
        print(f"  complete failed ({e.__class__.__name__}: {e})"); return 2
    print("  " + json.dumps(res, ensure_ascii=False))
    if res.get("sha256_ok"):
        return 0
    if _restart:                                                   # сервер сбросил битый .part → один раз заново
        print("  sha256 mismatch, server reset the upload — retrying from scratch")
        return upload_one(path, url, chunk, _restart=False)
    return 2


def upload(files, url, chunk=65536, precheck=False):
    worst = 0
    for path in files:
        print(f"{os.path.basename(path)}:")
        if precheck:
            r = check(path)
            if r["status"] == "fail":
                print(f"  skipped (check: fail): {'; '.join(r['issues'])}"); worst = 2; continue
        worst = max(worst, upload_one(path, url, chunk))
    return worst


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")                   # Windows-консоль в cp1251 не ломает JSON
    except (AttributeError, ValueError):                           # pragma: no cover
        pass
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen"); g.add_argument("out"); g.add_argument("--defect", default="none",
        choices=["none", "clip", "silence", "dc", "quiet"]); g.add_argument("--sec", type=float, default=10)
    g.add_argument("--rate", type=int, default=16000)
    c = sub.add_parser("check"); c.add_argument("files", nargs="+"); c.add_argument("--json", action="store_true")
    u = sub.add_parser("upload"); u.add_argument("files", nargs="+")
    u.add_argument("--url", default="http://127.0.0.1:8799"); u.add_argument("--chunk", type=int, default=65536)
    u.add_argument("--check", action="store_true", help="сначала check; файлы со status=fail не выгружать")
    a = p.parse_args()
    if a.cmd == "gen":
        gen(a.out, a.defect, a.sec, a.rate); return 0
    if a.cmd == "upload":
        return upload(a.files, a.url, a.chunk, a.check)
    worst = 0
    for f in a.files:
        r = check(f)
        worst = max(worst, {"ok": 0, "warn": 1, "fail": 2}[r["status"]])
        if a.json:
            print(json.dumps(r, ensure_ascii=False))
        elif "peak_dbfs" not in r:
            print(f"[{r['status'].upper()}] {r['file']}")
        else:
            print(f"[{r['status'].upper()}] {r['file']}: {r['sample_rate']} Hz, {r['duration_s']} s, "
                  f"peak {r['peak_dbfs']} dBFS, rms {r['rms_dbfs']} dBFS")
            for m in r["issues"] + r["warnings"]:
                print(f"    - {m}")
    return worst


if __name__ == "__main__":
    sys.exit(main())
