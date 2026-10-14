# Инструкции для coding agent

Проект: desktop-клиент, который выгружает записи с аудиобейджа и передаёт их в собственный backend.
Направление из задания: **desktop-клиент для выгрузки аудио и передачи в backend**.

## Правила
- Только Python 3.10+ stdlib. `numpy` — необязательное ускорение `check`, код обязан работать без него.
  Новые зависимости — только с обоснованием в PR.
- Любое утверждение о файле («запись нормальная», «загрузилось») подтверждай выводом `tools/audiokit.py`, а не на глаз.
- Не меняй пороги в `audiokit.py` без примера файла, на котором старый порог ошибается. Таблица в
  `.claude/skills/audio-qa/SKILL.md` описывает те же пороги — меняй оба места.
- Инструменты не должны падать трейсбеком на плохом входе: ошибка → `status: fail` / код 2 и одна строка причины.
- Реального устройства нет. Для тестов генерируй данные: `python tools/audiokit.py gen ...`.
- Не пиши в `samples/` руками; `received/` — выход заглушки, в git не коммитится.
- Перед коммитом: `python -m unittest discover -s tests`. Новое поведение — новый тест.

## Команды
```
python tools/audiokit.py gen samples/x.wav --defect clip|silence|dc|quiet|none
python tools/audiokit.py check samples/*.wav [--json]        # 0 ok / 1 warn / 2 fail
python tools/mock_backend.py [--fail-after N]                # http://127.0.0.1:8799
python tools/audiokit.py upload samples/*.wav [--check]      # 0 все приняты / 2 есть непринятые
python -m unittest discover -s tests -v
```

## Протокол загрузки (контракт с backend)
1. `PUT /uploads/{sha256}?name=&size=` → `{"offset": n}`: сколько байт уже есть. `name` URL-кодирован;
   сервер берёт только базовое имя (`..` и разделители → 400).
2. `POST /uploads/{sha256}/chunk?offset=n` (тело — байты) → новый offset; при несовпадении — 409 с актуальным offset.
3. `POST /uploads/{sha256}/complete` → `{"sha256_ok": bool, "stored": path}`.
   При `false` сервер удаляет `.part` (`"reset": true`); клиент один раз повторяет с нуля.
   Если на сервере уже есть другой файл с таким именем, новый сохраняется как `name.<sha8>.ext`.
4. `DELETE /uploads/{sha256}` → сброс незавершённой загрузки вручную.
Обрыв связи → повторить шаг 1 и продолжить с offset сервера (до 3 попыток подряд).

## Skills
- `.claude/skills/audio-qa` — проверка записей перед выгрузкой и разбор предупреждений.
