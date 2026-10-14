"""
Веб-приложение «OverAll» поверх скриптов парсинга Google Таблицы стажёров.

Запуск:
    uvicorn app.main:app --host 0.0.0.0 --port 8000
или:
    python -m app.main

Страницы:
    /                — главная (статус, быстрый запуск задач)
    /report          — формирование payroll-отчета за месяц
    /snapshots       — снепшоты, история и журнал изменений
"""

from __future__ import annotations

import asyncio
import traceback
from datetime import date, datetime

import pandas as pd
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from app import core
from app import leadpay

APP_DIR = Path(__file__).resolve().parent

app = FastAPI(title="OverAll — учёт стажёров", version="1.0")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")

MONTH_NAMES = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]

# Пространство кэша данных, чтобы не дёргать Google API на каждой странице
_cache: dict = {"raw_df": None, "interim_df": None, "fetched_at": None, "log": []}
_jobs: dict = {}          # фоновые задачи: id -> статус
_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def df_to_html(df: pd.DataFrame, max_rows: int = 500) -> str:
    if df is None or df.empty:
        return '<p class="empty">Нет данных.</p>'
    return df.head(max_rows).to_html(
        classes="table", index=False, border=0, na_rep="", escape=True)


def credentials_ok() -> bool:
    return core.CREDENTIALS_FILE.exists()


async def run_job(job_id: str, func, *args):
    """Выполняет синхронную функцию в отдельном потоке с логом статуса."""
    _jobs[job_id] = {"status": "running", "started": datetime.now().isoformat(
        timespec="seconds"), "result": None, "error": None}
    try:
        result = await asyncio.to_thread(func, *args)
        _jobs[job_id].update(status="done", result=result)
    except Exception as e:
        _jobs[job_id].update(status="error", error=f"{e}",
                            trace=traceback.format_exc(limit=3))
    return _jobs[job_id]


def fetch_data_task():
    raw_df, interim_df, log = core.fetch_spreadsheet_data()
    _cache.update(raw_df=raw_df, interim_df=interim_df,
                  fetched_at=datetime.now(), log=log)
    return {"rows": len(raw_df), "interim": len(interim_df), "log": log}


def build_report_task(year: int, month: int):
    if _cache["raw_df"] is None:
        fetch_data_task()
    raw_df, interim_df = _cache["raw_df"], _cache["interim_df"]
    if raw_df is None or raw_df.empty:
        raise ValueError("Нет данных: таблица вернула пустой результат")
    daily_df, summary_df = core.generate_payroll_reports(raw_df, interim_df, year, month)
    path = core.save_payroll_report(daily_df, summary_df, year, month)
    return {
        "file": path.name,
        "daily_rows": len(daily_df),
        "summary_rows": len(summary_df),
        "techleads": int(daily_df["Техлид"].nunique()) if not daily_df.empty else 0,
    }


def snapshot_task(snapshot_date: date | None):
    return core.run_snapshot_job(snapshot_date)


def leadpay_task(year: int, month: int):
    """Расчёт ЗП техлидов за месяц по данным из кэша/таблицы."""
    if _cache["raw_df"] is None:
        fetch_data_task()
    raw_df, interim_df = _cache["raw_df"], _cache["interim_df"]
    if raw_df is None or raw_df.empty:
        raise ValueError("Нет данных: таблица вернула пустой результат")
    periods, totals, pairs = leadpay.run_leadpay(raw_df, interim_df, year, month)
    path = leadpay.save_leadpay_report(periods, totals, year, month)
    return {
        "file": path.name,
        "period_rows": len(periods),
        "lead_pairs": len(totals),
        "total_sum": float(totals["Сумма за месяц"].sum()) if not totals.empty else 0.0,
        "unset_levels": int((periods["Уровень_источник"] == "не задан").sum())
                        if not periods.empty else 0,
    }


# ---------------------------------------------------------------------------
# Маршруты
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    history = core.load_history()
    changes = core.load_changes_log()
    snapshots = core.list_snapshots()
    context = {
        "request": request,
        "credentials_ok": credentials_ok(),
        "cached": _cache["raw_df"] is not None and not _cache["raw_df"].empty,
        "cached_rows": len(_cache["raw_df"]) if _cache["raw_df"] is not None else 0,
        "fetched_at": _cache["fetched_at"].strftime("%d.%m.%Y %H:%M")
                      if _cache["fetched_at"] else None,
        "history_rows": len(history),
        "history_dates": int(history["Дата_снепшота"].nunique()) if not history.empty else 0,
        "changes_rows": len(changes),
        "snapshots_count": len(snapshots),
        "sheets": core.SHEET_NAMES,
        "jobs": _jobs,
    }
    return templates.TemplateResponse("index.html", context)


@app.post("/fetch")
async def fetch_data():
    if not credentials_ok():
        return JSONResponse({"error": "Файл Credentials.json не найден"}, status_code=400)
    job_id = f"fetch_{datetime.now().strftime('%H%M%S')}"
    asyncio.create_task(run_job(job_id, fetch_data_task))
    return JSONResponse({"job_id": job_id})


@app.get("/report", response_class=HTMLResponse)
async def report_page(request: Request, year: int | None = None, month: int | None = None,
                      job: str | None = None):
    year = year or core.DEFAULT_REPORT_YEAR
    month = month or core.DEFAULT_REPORT_MONTH
    years = list(range(datetime.now().year - 2, datetime.now().year + 3))

    job_status = _jobs.get(job) if job else None
    result = job_status["result"] if job_status and job_status["status"] == "done" else None

    daily_df = summary_df = None
    if result:
        p = core.EXPORT_DIR / result["file"]
        if p.exists():
            daily_df = pd.read_excel(p, sheet_name="Ежедневный_отчет")
            summary_df = pd.read_excel(p, sheet_name="Сводка_за_месяц")
            daily_df["дата"] = pd.to_datetime(daily_df["дата"]).dt.strftime("%d.%m.%Y")

    context = {
        "request": request,
        "year": year, "month": month, "years": years,
        "month_names": MONTH_NAMES,
        "credentials_ok": credentials_ok(),
        "cached": _cache["raw_df"] is not None and not _cache["raw_df"].empty,
        "job_status": job_status,
        "result": result,
        "daily_html": df_to_html(daily_df),
        "summary_html": df_to_html(summary_df),
        "download_url": f"/download/{result['file']}" if result else None,
    }
    return templates.TemplateResponse("report.html", context)


@app.post("/report/run")
async def report_run(year: int = Form(...), month: int = Form(...)):
    if not credentials_ok():
        return JSONResponse({"error": "Файл Credentials.json не найден"}, status_code=400)
    job_id = f"report_{year}_{month:02d}_{datetime.now().strftime('%H%M%S')}"
    asyncio.create_task(run_job(job_id, build_report_task, year, month))
    return JSONResponse({"job_id": job_id})


# ---------------------------------------------------------------------------
# Расчёт ЗП техлидов
# ---------------------------------------------------------------------------

def _leadpay_pairs():
    """Уникальные пары (Технология, Техлид) из кэша данных таблицы."""
    if _cache["raw_df"] is None or _cache["raw_df"].empty:
        return None
    df = _cache["raw_df"].copy()
    df["Технология"] = df["Технология"].apply(core.clean_tech_name)
    df["Техлид"] = df["Техлид"].apply(core.clean_fio)
    df = df[df["Техлид"].apply(core.is_valid_techlead)]
    return df[["Технология", "Техлид"]].drop_duplicates().reset_index(drop=True)


@app.get("/leadpay", response_class=HTMLResponse)
async def leadpay_page(request: Request, year: int | None = None, month: int | None = None,
                       job: str | None = None):
    year = year or core.DEFAULT_REPORT_YEAR
    month = month or core.DEFAULT_REPORT_MONTH
    years = list(range(datetime.now().year - 2, datetime.now().year + 3))
    cfg = leadpay.load_config()

    job_status = _jobs.get(job) if job else None
    result = job_status["result"] if job_status and job_status["status"] == "done" else None

    periods_df = totals_df = None
    if result:
        p = core.EXPORT_DIR / result["file"]
        if p.exists():
            periods_df = pd.read_excel(p, sheet_name="Периоды")
            totals_df = pd.read_excel(p, sheet_name="Итоги по техлидам")
            for c in ("Начало", "Окончание"):
                periods_df[c] = pd.to_datetime(periods_df[c]).dt.strftime("%d.%m.%Y")

    # таблица назначений: все известные пары техлид+технология
    rows = []
    seen = set()
    known = _leadpay_pairs()
    if known is not None and not known.empty:
        for _, r in known.iterrows():
            key = (r["Техлид"], r["Технология"])
            if key in seen:
                continue
            seen.add(key)
            rate, rate_src = leadpay.get_rate(cfg, r["Техлид"], r["Технология"])
            level, coef, lvl_src = leadpay.get_level(cfg, r["Техлид"], r["Технология"])
            ov = cfg["overrides"].get(f"{r['Техлид']}|{r['Технология']}", {})
            rows.append({"lead": r["Техлид"], "tech": r["Технология"],
                         "rate": rate, "rate_src": rate_src,
                         "level": level, "coef": coef, "level_src": lvl_src,
                         "ov_rate": ov.get("rate", "")})

    context = {
        "request": request,
        "year": year, "month": month, "years": years, "month_names": MONTH_NAMES,
        "credentials_ok": credentials_ok(),
        "cached": _cache["raw_df"] is not None and not _cache["raw_df"].empty,
        "job_status": job_status,
        "result": result,
        "periods_html": df_to_html(periods_df),
        "totals_html": df_to_html(totals_df),
        "total_sum": float(totals_df["Сумма за месяц"].sum())
                     if totals_df is not None and not totals_df.empty else None,
        "download_url": f"/download/{result['file']}" if result else None,
        "cfg": cfg,
        "assign_rows": rows,
        "levels": list(cfg["level_coefficients"].keys()),
        "formula": ("Сумма_за_период = Рейт × Коэф. уровня × Кол-во календарных дней "
                    "× Кол-во стажеров ÷ 88"),
    }
    return templates.TemplateResponse("leadpay.html", context)


@app.post("/leadpay/run")
async def leadpay_run(year: int = Form(...), month: int = Form(...)):
    if not credentials_ok():
        return JSONResponse({"error": "Файл Credentials.json не найден"}, status_code=400)
    job_id = f"leadpay_{year}_{month:02d}_{datetime.now().strftime('%H%M%S')}"
    asyncio.create_task(run_job(job_id, leadpay_task, year, month))
    return JSONResponse({"job_id": job_id})


@app.post("/leadpay/rates")
async def leadpay_rates(tech: str = Form(...), rate: float = Form(...)):
    cfg = leadpay.load_config()
    cfg["tech_rates"][tech.strip().upper()] = rate
    leadpay.save_config(cfg)
    return JSONResponse({"ok": True})


@app.post("/leadpay/assign")
async def leadpay_assign(lead: str = Form(...), tech: str = Form(...),
                        level: str = Form(""), rate: str = Form("")):
    cfg = leadpay.load_config()
    lead_n, tech_n = lead.strip(), tech.strip().upper()
    key = f"{lead_n}|{tech_n}"
    ov = cfg["overrides"].get(key, {})
    if level.strip():
        cfg["leads"].setdefault(lead_n, {})[tech_n] = level.strip().upper()
    elif tech_n in cfg["leads"].get(lead_n, {}):
        del cfg["leads"][lead_n][tech_n]
    if rate.strip():
        try:
            ov["rate"] = float(rate.replace(",", "."))
        except ValueError:
            return JSONResponse({"error": f"Некорректный рейт: {rate}"}, status_code=400)
    else:
        ov.pop("rate", None)
    if ov:
        cfg["overrides"][key] = ov
    else:
        cfg["overrides"].pop(key, None)
    leadpay.save_config(cfg)
    return JSONResponse({"ok": True})


@app.get("/snapshots", response_class=HTMLResponse)
async def snapshots_page(request: Request, job: str | None = None):
    job_status = _jobs.get(job) if job else None
    result = job_status["result"] if job_status and job_status["status"] == "done" else None

    history = core.load_history()
    changes = core.load_changes_log()
    snapshots = core.list_snapshots()

    hist_dates = pd.DataFrame(columns=["Дата_снепшота", "Записей"])
    if not history.empty:
        hist_dates = (history.groupby("Дата_снепшота").size()
                      .reset_index(name="Записей")
                      .sort_values("Дата_снепшота", ascending=False))

    context = {
        "request": request,
        "credentials_ok": credentials_ok(),
        "today": date.today().isoformat(),
        "job_status": job_status,
        "result": result,
        "snapshots": snapshots,
        "history_dates_html": df_to_html(hist_dates),
        "history_rows": len(history),
        "changes_html": df_to_html(changes.tail(200).sort_values(
            "Дата_снепшота", ascending=False)) if not changes.empty else None,
        "changes_rows": len(changes),
    }
    return templates.TemplateResponse("snapshots.html", context)


@app.post("/snapshots/run")
async def snapshots_run(snap_date: str = Form("")):
    if not credentials_ok():
        return JSONResponse({"error": "Файл Credentials.json не найден"}, status_code=400)
    d = None
    if snap_date.strip():
        try:
            d = date.fromisoformat(snap_date.strip())
        except ValueError:
            return JSONResponse({"error": f"Некорректная дата: {snap_date}"}, status_code=400)
    job_id = f"snap_{datetime.now().strftime('%H%M%S')}"
    asyncio.create_task(run_job(job_id, snapshot_task, d))
    return JSONResponse({"job_id": job_id})


@app.get("/job/{job_id}", response_class=JSONResponse)
async def job_status(job_id: str):
    return _jobs.get(job_id, {"status": "unknown"})


@app.get("/download/{filename}")
async def download(filename: str):
    from fastapi.responses import FileResponse
    for base in (core.EXPORT_DIR, core.SNAPSHOT_DIR):
        p = (base / filename).resolve()
        if str(p).startswith(str(base.resolve())) and p.exists():
            return FileResponse(p, filename=filename)
    return JSONResponse({"error": "Файл не найден"}, status_code=404)


def main():
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()
