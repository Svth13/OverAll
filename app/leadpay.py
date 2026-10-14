"""
Расчёт оплаты (ЗП) техлидов по методике из ТЗ.

Формула за один период:
    Сумма_за_период = Рейт × Коэффициент_Экспертности
                      × Кол-во_календарных_дней_в_периоде × Кол-во_стажеров / 88

где:
  * Рейт — месячная базовая ставка технологии (справочник, можно переопределить);
  * Коэффициент экспертности — L1=0, L2=1, L3=1.5 (назначается техлиду);
  * Кол-во календарных дней = (Дата_окончания - Дата_начала + 1), включая выходные;
  * 88 — константа-делитель из ТЗ;
  * каждый период округляется до копеек отдельно;
  * Итог за месяц = сумма значений по всем периодам месяца.

Данные о стажировках берутся из той же Google Таблицы, что и payroll-отчёт
(app.core): активные записи «технология + техлид» с датами старта/стопа.
Периоды считаются в границах отчётного месяца (как в ежедневном payroll-отчёте),
стажеры в неоплачиваемом отпуске исключаются из периодов.

Настройки (рейты, уровни, override рейта для конкретного техлида) хранятся
в JSON-файле app/config/leadpay.json.
"""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = APP_DIR / "config" / "leadpay.json"
EXPORT_DIR = APP_DIR.parent / "exports"

DAYS_DIVISOR = 88  # константа-делитель из ТЗ

# ---------------------------------------------------------------------------
# Справочники (Таблица 1 и Таблица 2 из ТЗ)
# ---------------------------------------------------------------------------

DEFAULT_TECH_RATES = {
    "BA": 25000.0,
    "SA": 25000.0,
    "JAVA": 22000.0,
    "REACT": 17250.0,
    "DEVOPS": 25000.0,
    "QAENGINEER": 23000.0,   # в файле встречается написание "QA Engenier"
    "ARCHITECTOR": 27000.0,
}

LEVEL_COEFFICIENTS = {"L1": 0.0, "L2": 1.0, "L3": 1.5}

DEFAULT_CONFIG = {
    "tech_rates": dict(DEFAULT_TECH_RATES),
    "level_coefficients": dict(LEVEL_COEFFICIENTS),
    # назначения по умолчанию для всех технологий техлида
    "leads": {},
    # точечные переопределения: "Техлид|ТЕХНОЛОГИЯ" -> {"rate": float, "level": "L3"}
    "overrides": {},
}


def _round2(x: float) -> float:
    """Математическое округление до копеек (0.5 -> вверх)."""
    return math.floor(x * 100 + 0.5) / 100


# ---------------------------------------------------------------------------
# Хранилище настроек
# ---------------------------------------------------------------------------

def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # глубокая копия дефолтов
    if CONFIG_FILE.exists():
        try:
            stored = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            stored = {}
        for key in ("tech_rates", "level_coefficients", "leads", "overrides"):
            if isinstance(stored.get(key), dict):
                cfg[key].update(stored[key])
    return cfg


def save_config(cfg: dict):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    clean = {
        "tech_rates": {str(k).upper(): float(v) for k, v in cfg.get("tech_rates", {}).items()},
        "level_coefficients": {str(k).upper(): float(v)
                               for k, v in cfg.get("level_coefficients", {}).items()},
        "leads": cfg.get("leads", {}),
        "overrides": cfg.get("overrides", {}),
    }
    CONFIG_FILE.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")


def get_rate(cfg: dict, lead_norm: str, tech_norm: str) -> tuple[float, str]:
    """Возвращает (рейт, источник значения) для пары техлид+технология."""
    ov = cfg["overrides"].get(f"{lead_norm}|{tech_norm}", {})
    if "rate" in ov and ov["rate"] is not None:
        return float(ov["rate"]), "override"
    return float(cfg["tech_rates"].get(tech_norm, 0.0)), "справочник"


def get_level(cfg: dict, lead_norm: str, tech_norm: str) -> tuple[str, float, str]:
    """Возвращает (уровень, коэффициент, источник) для пары техлид+технология."""
    level = None
    source = "не задан"
    ov = cfg["overrides"].get(f"{lead_norm}|{tech_norm}", {})
    if ov.get("level"):
        level, source = str(ov["level"]).upper(), "override"
    elif lead_norm in cfg["leads"] and cfg["leads"][lead_norm].get(tech_norm):
        level, source = str(cfg["leads"][lead_norm][tech_norm]).upper(), "назначение"
    if level is None:
        return "", 0.0, "не задан"
    coef = cfg["level_coefficients"].get(level)
    if coef is None:
        return level, 0.0, "уровень не в справочнике"
    return level, float(coef), source


# ---------------------------------------------------------------------------
# Сбор пар (технология, техлид) со стажемёрами из данных таблицы
# ---------------------------------------------------------------------------

def collect_lead_pairs(raw_df: pd.DataFrame, interim_df: pd.DataFrame | None,
                      year: int, month: int) -> pd.DataFrame:
    """
    Возвращает DataFrame [Технология, Техлид, Лаборант_норм, eff_start, eff_end] —
    эффективные (склеенные через перерывы) интервалы каждой связки
    «техлид → стажер» внутри отчётного месяца, с учётом неоплачиваемых отпусков.
    """
    from app import core  # локальный импорт, чтобы избежать цикла

    df = raw_df.copy()
    df["Технология"] = df["Технология"].apply(core.clean_tech_name)
    df["Техлид"] = df["Техлид"].apply(core.clean_fio)
    df = df[df["Техлид"].apply(core.is_valid_techlead)].copy()
    df["Лаборант_норм"] = df["Лаборант"].apply(core.normalize_fio)
    df["Дата старта"] = pd.to_datetime(df["Дата старта"], errors="coerce")
    df["Дата стопа"] = pd.to_datetime(df["Дата стопа"], errors="coerce")
    df = df.dropna(subset=["Дата старта"])

    m_start = pd.Timestamp(year=year, month=month, day=1)
    m_end = m_start + pd.DateOffset(months=1) - pd.DateOffset(days=1)

    # Окна неоплачиваемых отпусков (как в generate_payroll_reports)
    leave_windows: dict[tuple, list[tuple]] = {}
    if interim_df is not None and not interim_df.empty:
        idf = interim_df.copy()
        idf["Дата старта"] = pd.to_datetime(idf["Дата старта"], errors="coerce")
        idf["Дата стопа"] = pd.to_datetime(idf["Дата стопа"], errors="coerce")
        idf["Лаборант_норм"] = idf["Лаборант"].apply(core.normalize_fio)
        idf["Технология"] = idf["Технология"].apply(core.clean_tech_name)
        idf["Техлид"] = idf["Техлид"].apply(core.clean_fio)
        for _, r in idf.iterrows():
            if pd.notna(r["Дата старта"]) and pd.notna(r["Дата стопа"]):
                leave_windows.setdefault(
                    (r["Лаборант_норм"], r["Технология"], r["Техлид"]), []
                ).append((r["Дата старта"], r["Дата стопа"]))

    rows = []
    for (tech, lead, lab), grp in df.groupby(["Технология", "Техлид", "Лаборант_норм"]):
        # 1. Склейка интервалов стажировки (пересечения или стык)
        ivs = sorted((max(g["Дата старта"], m_start),
                      min(g["Дата стопа"], m_end) if pd.notna(g["Дата стопа"]) else m_end)
                     for _, g in grp.iterrows())
        merged = []
        for s, e in ivs:
            if s > m_end or e < m_start:
                continue
            if merged and s <= merged[-1][1] + pd.Timedelta(days=1):
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        # 2. Вычитание окон неоплачиваемого отпуска
        for ls, le in leave_windows.get((lab, tech, lead), []):
            nxt = []
            for s, e in merged:
                if le < s or ls > e:
                    nxt.append([s, e]); continue
                if ls > s:
                    nxt.append([s, min(ls - pd.Timedelta(days=1), e)])
                if le < e:
                    nxt.append([max(le + pd.Timedelta(days=1), s), e])
            merged = nxt
        for s, e in merged:
            if s <= e:
                rows.append({"Технология": tech, "Техлид": lead,
                             "Лаборант_норм": lab, "start": s, "end": e})
    return pd.DataFrame(rows, columns=["Технология", "Техлид", "Лаборант_норм",
                                       "start", "end"])


def build_periods(pairs: pd.DataFrame, year: int, month: int) -> pd.DataFrame:
    """
    Разрезает месяц на периоды постоянным составом стажеров для каждой пары
    (Технология, Техлид). Возвращает строки периодов с колонками:
    Начало, Окончание, Дней, Стажеров, Список стажеров.
    """
    m_start = pd.Timestamp(year=year, month=month, day=1)
    m_end = m_start + pd.DateOffset(months=1) - pd.Timedelta(days=1)
    after_month = m_end + pd.Timedelta(days=1)

    out = []
    for (tech, lead), grp in pairs.groupby(["Технология", "Техлид"]):
        adds: dict[pd.Timestamp, set] = {}
        rems: dict[pd.Timestamp, set] = {}
        for _, p in grp.iterrows():
            adds.setdefault(p["start"], set()).add(p["Лаборант_норм"])
            rems.setdefault(min(p["end"] + pd.Timedelta(days=1), after_month),
                            set()).add(p["Лаборант_норм"])

        active: set = set()
        boundaries = sorted(set(adds) | set(rems) | {m_start})
        prev_day = None
        for b in boundaries:
            if b > m_end:
                break
            # закрытие периода, закончившегося накануне
            if prev_day is not None and active:
                out.append({
                    "Технология": tech, "Техлид": lead,
                    "Начало": prev_day.date(), "Окончание": (b - pd.Timedelta(days=1)).date(),
                    "Дней": int((b - prev_day).days),
                    "Стажеров": len(active),
                    "Список стажеров": ", ".join(sorted(active)),
                })
            # события на начало дня b: сначала удаления, затем добавления
            active -= rems.get(b, set())
            active |= adds.get(b, set())
            prev_day = b
        # последний период месяца
        if prev_day is not None and active:
            out.append({
                "Технология": tech, "Техлид": lead,
                "Начало": prev_day.date(), "Окончание": m_end.date(),
                "Дней": int((after_month - prev_day).days),
                "Стажеров": len(active),
                "Список стажеров": ", ".join(sorted(active)),
            })
    return pd.DataFrame(out)


# ---------------------------------------------------------------------------
# Основной расчёт
# ---------------------------------------------------------------------------

def calculate_lead_payments(pairs: pd.DataFrame, cfg: dict,
                            year: int, month: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Возвращает (periods_df, totals_df) согласно формуле из ТЗ."""
    periods = build_periods(pairs, year, month)
    if periods.empty:
        empty_cols = ["Технология", "Техлид", "Рейт", "Уровень", "Коэф.",
                      "Начало", "Окончание", "Дней", "Стажеров", "Сумма"]
        return pd.DataFrame(columns=empty_cols), pd.DataFrame(
            columns=["Технология", "Техлид", "Рейт", "Уровень", "Коэф.",
                     "Периодов", "Сумма за месяц"])

    rows = []
    for _, p in periods.iterrows():
        lead_n = p["Техлид"]
        rate, rate_src = get_rate(cfg, lead_n, p["Технология"])
        level, coef, lvl_src = get_level(cfg, lead_n, p["Технология"])
        amount = _round2(rate * coef * p["Дней"] * p["Стажеров"] / DAYS_DIVISOR)
        rows.append({
            "Технология": p["Технология"], "Техлид": lead_n,
            "Рейт": rate, "Рейт_источник": rate_src,
            "Уровень": level, "Коэф.": coef, "Уровень_источник": lvl_src,
            "Начало": p["Начало"], "Окончание": p["Окончание"],
            "Дней": p["Дней"], "Стажеров": p["Стажеров"],
            "Список стажеров": p["Список стажеров"],
            "Сумма": amount,
        })
    pdf = pd.DataFrame(rows).sort_values(
        ["Техлид", "Технология", "Начало"]).reset_index(drop=True)

    totals = pdf.groupby(["Технология", "Техлид"]).agg(
        Рейт=("Рейт", "first"), Уровень=("Уровень", "first"),
        Коэф=("Коэф.", "first"), Периодов=("Сумма", "count"),
        **{"Сумма за месяц": ("Сумма", lambda s: _round2(float(s.sum())))},
    ).reset_index().sort_values(["Технология", "Техлид"]).reset_index(drop=True)

    return pdf, totals


def run_leadpay(raw_df: pd.DataFrame, interim_df: pd.DataFrame | None,
                year: int, month: int, cfg: dict | None = None):
    """Полный цикл: сбор пар -> периоды -> расчёт. Возвращает (periods, totals, pairs)."""
    cfg = cfg or load_config()
    pairs = collect_lead_pairs(raw_df, interim_df, year, month)
    periods, totals = calculate_lead_payments(pairs, cfg, year, month)
    return periods, totals, pairs


def save_leadpay_report(periods: pd.DataFrame, totals: pd.DataFrame,
                        year: int, month: int) -> Path:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    filename = EXPORT_DIR / f"leadpay_{year}_{month:02d}.xlsx"
    with pd.ExcelWriter(filename, engine="openpyxl", datetime_format="YYYY-MM-DD") as w:
        periods.to_excel(w, sheet_name="Периоды", index=False)
        totals.to_excel(w, sheet_name="Итоги по техлидам", index=False)
        for ws in w.sheets.values():
            for i, col in enumerate(ws.columns):
                mx = max((len(str(c.value)) for c in col if c.value is not None), default=10)
                ws.column_dimensions[chr(65 + i)].width = min(mx + 2, 40)
    return filename
