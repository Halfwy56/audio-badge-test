#!/usr/bin/env python3
"""audiokit — проверка и выгрузка записей с аудиобейджа (только stdlib).

  python tools/audiokit.py gen    out.wav [--defect clip|silence|dc|none] [--sec 10] [--rate 16000]
  python tools/audiokit.py check  file.wav [--json]
  python tools/audiokit.py upload file.wav --url http://127.0.0.1:8799 [--chunk 65536]

Код возврата check: 0 — OK, 1 — есть предупреждения, 2 — файл непригоден.
"""
import argparse, array, hashlib, json, math, os, random, sys, urllib.request, wave

# Пороги вынесены сюда, чтобы агент и человек меняли их в одном месте.
EXPECTED_RATES = (16000, 48000)      # Omi: 16 кГц Opus; 48 кГц — типичный PDM/I2S поток
CLIP_LEVEL = 32767 - 64              # |x| >= этого считаем клипом (16 бит)
CLIP_MAX_RATIO = 0.001               # >0.1 % клипнутых сэмплов — warning
SILENCE_DBFS = -60.0                 # окно тише этого — тишина
SILENCE_MAX_RATIO = 0.9              # >90 % тишины — запись, скорее всего, пустая
DC_MAX = 0.02                        # смещение >2 % FS — проблема микрофона/АЦП
WIN_MS = 50


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
        data.append(max(-32768, min(32767, int(s * 32767))))
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(data.tobytes())
    print(f"written {path}: {sec}s @ {rate} Hz, defect={defect}")


def check(path):
    issues, warns = [], []
    with wave.open(path, "rb") as w:
        ch, sw, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    info = {"file": os.path.basename(path), "channels": ch, "sample_width_bits": sw * 8,
            "sample_rate": rate, "duration_s": round(n / rate, 3) if rate else 0}
    if sw != 2:
        issues.append(f"ожидается 16 бит PCM, получено {sw*8} бит")
    if rate not in EXPECTED_RATES:
        warns.append(f"частота {rate} Гц не из {EXPECTED_RATES} — backend будет ресемплировать")
    if n == 0:
        issues.append("файл пустой")
    if issues:
        return {**info, "status": "fail", "issues": issues, "warnings": warns}

    s = array.array("h", raw)
    if ch > 1:
        s = s[0::ch]                                               # анализируем первый канал
    total = len(s)
    mean = sum(s) / total
    peak = max(abs(x) for x in s)
    clipped = sum(1 for x in s if abs(x) >= CLIP_LEVEL)
    win = max(1, rate * WIN_MS // 1000)
    silent_w = n_w = 0
    for i in range(0, total - win + 1, win):
        seg = s[i:i + win]
        rms = math.sqrt(sum(x * x for x in seg) / win) / 32768
        n_w += 1
        silent_w += dbfs(rms) < SILENCE_DBFS
    rms_all = math.sqrt(sum(x * x for x in s) / total) / 32768
    info.update(peak_dbfs=round(dbfs(peak / 32768), 2), rms_dbfs=round(dbfs(rms_all), 2),
                dc_offset=round(mean / 32768, 4), clipped_ratio=round(clipped / total, 5),
                silence_ratio=round(silent_w / n_w, 3) if n_w else 1.0)

    if info["clipped_ratio"] > CLIP_MAX_RATIO:
        warns.append(f"клиппинг {info['clipped_ratio']:.2%} сэмплов — снизить PGA/gain")
    if info["silence_ratio"] > SILENCE_MAX_RATIO:
        warns.append(f"тишина {info['silence_ratio']:.0%} записи — проверить микрофон/VAD")
    if abs(info["dc_offset"]) > DC_MAX:
        warns.append(f"DC-смещение {info['dc_offset']:+.3f} FS — нужен HPF/проверка АЦП")
    status = "warn" if warns else "ok"
    return {**info, "status": status, "issues": issues, "warnings": warns}


def upload(path, url, chunk=65536):
    """Чанковая выгрузка с докачкой: спрашиваем у backend offset, шлём остаток."""
    data = open(path, "rb").read()
    sha = hashlib.sha256(data).hexdigest()
    name = os.path.basename(path)

    def server_offset():
        req = urllib.request.Request(f"{url}/uploads/{sha}?name={name}&size={len(data)}", method="PUT")
        return json.load(urllib.request.urlopen(req, timeout=10))["offset"]

    offset = server_offset()
    if offset:
        print(f"resume from offset {offset}/{len(data)}")
    retries = 0
    while offset < len(data):
        part = data[offset:offset + chunk]
        r = urllib.request.Request(f"{url}/uploads/{sha}/chunk?offset={offset}", data=part,
                                   method="POST", headers={"Content-Type": "application/octet-stream"})
        try:
            offset = json.load(urllib.request.urlopen(r, timeout=10))["offset"]
            print(f"  sent {offset}/{len(data)}")
        except (OSError, ValueError) as e:                         # обрыв связи: спросить offset и докачать
            retries += 1
            if retries > 3:
                print(f"  giving up: {e}"); return 2
            offset = server_offset()
            print(f"  connection lost ({e.__class__.__name__}), resume from {offset}")
    r = urllib.request.Request(f"{url}/uploads/{sha}/complete", method="POST", data=b"")
    res = json.load(urllib.request.urlopen(r))
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("sha256_ok") else 2


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen"); g.add_argument("out"); g.add_argument("--defect", default="none",
        choices=["none", "clip", "silence", "dc"]); g.add_argument("--sec", type=float, default=10)
    g.add_argument("--rate", type=int, default=16000)
    c = sub.add_parser("check"); c.add_argument("files", nargs="+"); c.add_argument("--json", action="store_true")
    u = sub.add_parser("upload"); u.add_argument("file"); u.add_argument("--url", default="http://127.0.0.1:8799")
    u.add_argument("--chunk", type=int, default=65536)
    a = p.parse_args()
    if a.cmd == "gen":
        gen(a.out, a.defect, a.sec, a.rate); return 0
    if a.cmd == "upload":
        return upload(a.file, a.url, a.chunk)
    worst = 0
    for f in a.files:
        r = check(f)
        worst = max(worst, {"ok": 0, "warn": 1, "fail": 2}[r["status"]])
        if a.json:
            print(json.dumps(r, ensure_ascii=False))
        else:
            print(f"[{r['status'].upper()}] {r['file']}: {r.get('sample_rate')} Hz, {r.get('duration_s')} s, "
                  f"peak {r.get('peak_dbfs')} dBFS, rms {r.get('rms_dbfs')} dBFS")
            for m in r["issues"] + r["warnings"]:
                print(f"    - {m}")
    return worst


if __name__ == "__main__":
    sys.exit(main())
