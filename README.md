# Qmeka

**Статус: paused** — конструктор пайплайнов из AI-агентов. Код восстановлен из логов чатов после сбоя
системы 15.11.2026, работоспособность не подтверждена (см. `docs/RECOVERY.md`).

Локальный конструктор пайплайнов: граф из нод, у каждой — роль (скилл), вендор (локальный `opencode` CLI
или браузерный сервис — z.ai, DeepSeek, Qwen, Google), входы/выходы-артефакты и промпт. Запуск: задача +
число итераций, конвейер гоняет её по графу, ветвится, судья сводит ветки.

Исторически это тот же проект, что «orchestrator»: сначала был жёсткий цикл opencode ↔ z.ai
(`docs/ORCHESTRATOR_DESIGN.md`), затем он обобщился в DAG-конструктор с веб-UI.

## Что внутри

| путь | что |
|---|---|
| `ui\app.py` | FastAPI-приложение: старт, столы-проекты, канвас, сессии, API ролей и пресетов |
| `ui\templates\` | 9 шаблонов: dashboard, pipeline, sessions, debug, backlog, wizard, шапка, рейл |
| `ui\static\` | `qmeka-chrome.js/css` — UI-кит, `pipeline-builder.js` — канвас-DAG |
| `fsm.py`, `fsm_v2.py` | исполнитель: линейный цикл и DAG со стадиями, `artifacts{}`, `parallel_group` |
| `opencode_driver.py` | вызов локального агента headless (`opencode run … --format json`) |
| `chat_driver.py` | браузерные ревьюеры через Playwright/CDP, оверлеи, блокировка ввода |
| `session_pool.py` | интроспекция браузеров, профили, детект логина по cookie-БД |
| `debug_bootstrap.py` | изолированный Chrome, проверка CDP, клонирование куки |
| `pipeline.json` | дефолтный пайплайн (grill → answers → meta-review) |
| `docs\ORCHESTRATOR_DESIGN.md` | дизайн исходного цикла, 62 КБ — главный документ замысла |

## Чего не хватает

`zai_driver.py`, `cdp_ws.py`, `_log.py`, `_metrics.py`, `prompt_builder.py`, `md2docx.py`,
`session_manager.py`, `login_gate.py`, `prompts.json`, `roles\` — не восстановлены. Без них
приложение не импортируется. Подробности — `docs/RECOVERY.md`.

## Идеи, которые здесь реализованы

- роль × вендор × порты, а не линейная цепочка;
- судья на 2–4 входа (сводит параллельные ветки);
- «слепые» планеры: ветки не видят исходный план, чтобы не якориться;
- инжекция коррекций между итерациями без остановки цикла;
- контракт артефактов через файлы, а не через сообщения;
- столы-проекты и отдельный профиль Chrome с логинами моделей.

Полный разбор замысла — в воркспейсе: `_notes\QMEKA-VISION.md`.

## Запуск (после восстановления дыр)

```
pip install -r requirements.txt
python -m uvicorn ui.app:app --host 127.0.0.1 --port 8080
```

Chrome-профиль оркестратора: `C:\Orchestrator\ChromeProfile`, CDP-порт 9222.
