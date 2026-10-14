# Инструкции для coding agent

Проект: desktop-клиент, который выгружает записи с аудиобейджа и передаёт их в собственный backend.
Направление из задания: **desktop-клиент для выгрузки аудио и передачи в backend**.

## Правила
- Только Python 3.10+ stdlib. Новые зависимости — только с обоснованием в PR.
- Любое утверждение о файле («запись нормальная», «загрузилось») подтверждай выводом `tools/audiokit.py`, а не на глаз.
- Не меняй пороги в `audiokit.py` без примера файла, на котором старый порог ошибается.
- Реального устройства нет. Для тестов генерируй данные: `python tools/audiokit.py gen ...`.
- Не пиши в `samples/` руками; `received/` — выход заглушки, в git не коммитится.

## Команды
```
python tools/audiokit.py gen samples/x.wav --defect clip|silence|dc|none
python tools/audiokit.py check samples/*.wav [--json]   # 0 ok / 1 warn / 2 fail
python tools/mock_backend.py [--fail-after N]           # http://127.0.0.1:8799
python tools/audiokit.py upload samples/x.wav
```

## Протокол загрузки (контракт с backend)
1. `PUT /uploads/{sha256}?name=&size=` → `{"offset": n}`: сколько байт уже есть.
2. `POST /uploads/{sha256}/chunk?offset=n` (тело — байты) → новый offset; при несовпадении — 409.
3. `POST /uploads/{sha256}/complete` → `{"sha256_ok": bool}`.
Обрыв связи → повторить шаг 1 и продолжить с offset сервера.

## Skills
- `.claude/skills/audio-qa` — проверка записей перед выгрузкой и разбор предупреждений.
