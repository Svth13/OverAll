"""
Ядро приложения: перенос логики из ноутбуков
«Парсинг_v4.ipynb» и «Парсинг_снепшот_v2.ipynb» в переиспользуемые модули.

Содержит:
  * константы подключения к Google Таблице;
  * функции очистки/нормализации данных (даты, ФИО, технологии);
  * парсинг листов таблицы (gspread);
  * генерацию payroll-отчета (ежедневный + сводка за месяц);
  * формирование снепшотов, истории и журнала изменений.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Константы подключения (синхронизированы с ноутбуками)
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent          # корень проекта
CREDENTIALS_FILE = BASE_DIR / "Credentials.json"           # ключ сервисного аккаунта
SNAPSHOT_DIR = BASE_DIR / "snapshots"                      # хранилище снепшотов
EXPORT_DIR = BASE_DIR / "exports"                          # сгенерированные отчеты

SPREADSHEET_ID = "1wxNIhrPjoFQf3vqatu9oK78W5RmNoI3YG6sf9i4GWAQ"

SHEET_NAMES = [
    "DEMO Лист", "A-Benefits", "1C", "Active Bank", "A-Sber",
    "Betta Bank", "Evergreen Bank+Laba ABS", "Liberty Bank",
    "Investment", "L&D", "MyCare", "SQL Trainee", "ProBusiness",
    "Уволенные 2026", "Стаффинг",
]

INTERIM_STATUS_SHEET = "Промежуточный статус стажера"

# Индексы столбцов основных листов (начинаются с 0)
COL_TECH = 1      # B
COL_TECHLEAD = 2  # C
COL_NAME = 3      # D
COL_START = 5     # F
COL_STOP = 6      # G
COL_REASON = 7    # H

# Индексы столбцов листа "Промежуточный статус стажера"
INTERIM_COL_TECH = 2         # C
INTERIM_COL_TECHLEAD = 3     # D
INTERIM_COL_NAME = 4         # E
INTERIM_COL_START = 6        # G
INTERIM_COL_STOP = 7         # H
INTERIM_COL_REASON = 8       # I

# Настройки отчетного периода по умолчанию
DEFAULT_REPORT_YEAR = 2026
DEFAULT_REPORT_MONTH = 10

# Состав колонок снепшота
SNAPSHOT_COLUMNS = [
    "Дата_снепшота", "Вид_записи", "Источник",
    "Технология_норм", "Технология_оригинал",
    "Техлид_норм", "Техлид_оригинал",
    "Лаборант_норм", "Лаборант_оригинал",
    "Дата_старта", "Дата_стопа", "Причина_статус",
]
KEY_COLS = ["Вид_записи", "Источник", "Технология_норм", "Техлид_норм", "Лаборант_норм"]
COMPARE_COLS = ["Дата_старта", "Дата_стопа", "Причина_статус"]
CHANGES_COLUMNS = ["Дата_снепшота", "Тип_изменения"] + KEY_COLS + ["Было", "Стало"]

HISTORY_FILE = SNAPSHOT_DIR / "history.csv"
CHANGES_FILE = SNAPSHOT_DIR / "changes_log.csv"


# ---------------------------------------------------------------------------
# Функции очистки и нормализации
# ---------------------------------------------------------------------------

def clean_date(val) -> date | None:
    """Преобразует значение в объект даты (поддержка разных форматов)."""
    if pd.isna(val) or str(val).strip() == "":
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    val_str = str(val).strip()
    for fmt in ["%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S"]:
        try:
            return datetime.strptime(val_str, fmt).date()
        except ValueError:
            continue
    return None


def parse_date_range(val) -> tuple[date | None, date | None]:
    """Парсит дату или диапазон дат. Возвращает (start_date, end_date)."""
    if pd.isna(val) or str(val).strip() == "":
        return None, None
    val_str = str(val).strip()
    if "-" in val_str and "." in val_str:
        parts = val_str.split("-")
        if len(parts) == 2:
            return clean_date(parts[0].strip()), clean_date(parts[1].strip())
    return clean_date(val_str), None


def clean_fio(val) -> str:
    """Очищает ФИО от лишних символов."""
    if pd.isna(val) or str(val).strip() == "":
        return ""
    val_str = str(val).strip()
    match = re.match(r"^[A-Za-zА-Яа-яЁё\s\-]+", val_str)
    if match:
        return match.group(0).strip()
    return val_str


def normalize_fio(fio: str) -> str:
    """Нормализует ФИО для корректного подсчета уникальных людей."""
    if not fio:
        return ""
    return re.sub(r"\s+", " ", fio.lower().strip()).title()


def clean_tech_name(tech_name):
    """
    Нормализует название технологии:
    - верхний регистр;
    - замена кириллических символов на латинские;
    - удаление спецсимволов и пробелов;
    - объединение синонимов (QAJAVA -> AQAJAVA).
    """
    if pd.isna(tech_name):
        return tech_name
    tech_str = str(tech_name).upper()
    cyrillic_to_latin = str.maketrans({
        "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M",
        "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T",
        "Х": "X",
    })
    tech_str = tech_str.translate(cyrillic_to_latin)
    tech_str = re.sub(r"[^A-Z0-9]", "", tech_str)
    tech_str = re.sub(r"^Q?AQ?AJAVA$", "AQAJAVA", tech_str)
    return tech_str


def is_valid_techlead(name: str) -> bool:
    """Проверяет, что имя техлида выглядит как реальное 'Имя Фамилия'."""
    if pd.isna(name):
        return False
    name = str(name).strip()
    if len(name) < 3:
        return False
    if " " not in name:
        return False
    blacklist = ["-", "х", "x", "2-я технология", "вторая технология", "команда", "techleads"]
    if name.lower() in blacklist:
        return False
    return True


# ---------------------------------------------------------------------------
# Аутентификация и парсинг Google Таблицы
# ---------------------------------------------------------------------------

def authenticate_with_service_account(credentials_file: str | Path = CREDENTIALS_FILE):
    import gspread
    from google.oauth2.service_account import Credentials

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
    ]
    creds = Credentials.from_service_account_file(str(credentials_file), scopes=scopes)
    return gspread.authorize(creds)


def extract_sheet_data(worksheet) -> pd.DataFrame:
    """Парсит основные листы гугл-дока."""
    values = worksheet.get_values()
    if not values:
        return pd.DataFrame()

    header_idx = -1
    for i, row in enumerate(values):
        if len(row) > COL_NAME:
            col_b = str(row[COL_TECH]).strip().lower()
            col_c = str(row[COL_TECHLEAD]).strip().lower()
            col_d = str(row[COL_NAME]).strip().lower()
            if ("технология" in col_b
                    and ("техлид" in col_c or "techlead" in col_c)
                    and ("фамилия" in col_d or "фио" in col_d)):
                header_idx = i
                break
    if header_idx == -1:
        return pd.DataFrame()

    data = []
    for i in range(header_idx + 1, len(values)):
        row = values[i]
        while len(row) <= COL_REASON:
            row.append("")
        col_b_val = str(row[COL_TECH]).strip()
        if not col_b_val:
            continue
        fio = clean_fio(row[COL_NAME]).strip()
        if not fio:
            continue
        data.append({
            "Технология": col_b_val,
            "Техлид": clean_fio(row[COL_TECHLEAD]),
            "Лаборант": fio,
            "Дата старта": clean_date(row[COL_START]),
            "Дата стопа": clean_date(row[COL_STOP]),
            "Причина_статус": str(row[COL_REASON]).strip().lower(),
            "Источник": worksheet.title,
        })
    return pd.DataFrame(data)


def extract_interim_status_data(worksheet, only_unpaid_leave: bool = True) -> pd.DataFrame:
    """
    Парсит лист 'Промежуточный статус стажера'.
    only_unpaid_leave=True — только 'Неоплачиваемый отпуск' (режим payroll-отчета);
    only_unpaid_leave=False — все промежуточные статусы (режим снепшота).
    """
    values = worksheet.get_values()
    if not values:
        return pd.DataFrame()

    header_idx = -1
    for i, row in enumerate(values):
        if len(row) > INTERIM_COL_NAME:
            col_c = str(row[INTERIM_COL_TECH]).strip().lower()
            col_d = str(row[INTERIM_COL_TECHLEAD]).strip().lower()
            col_e = str(row[INTERIM_COL_NAME]).strip().lower()
            if ("технология" in col_c
                    and ("техлид" in col_d or "techlead" in col_d)
                    and ("фамилия" in col_e or "фио" in col_e)):
                header_idx = i
                break
    if header_idx == -1:
        return pd.DataFrame()

    data = []
    for i in range(header_idx + 1, len(values)):
        row = values[i]
        while len(row) <= INTERIM_COL_REASON:
            row.append("")
        col_c_val = str(row[INTERIM_COL_TECH]).strip()
        if not col_c_val:
            continue
        fio = clean_fio(row[INTERIM_COL_NAME]).strip()
        if not fio:
            continue
        reason = str(row[INTERIM_COL_REASON]).strip().lower()
        if not reason:
            continue
        if only_unpaid_leave and "неоплачиваемый отпуск" not in reason:
            continue
        start_date, end_date = parse_date_range(row[INTERIM_COL_STOP])
        data.append({
            "Технология": col_c_val,
            "Техлид": clean_fio(row[INTERIM_COL_TECHLEAD]),
            "Лаборант": fio,
            "Дата старта": start_date if start_date else clean_date(row[INTERIM_COL_START]),
            "Дата стопа": end_date if end_date else clean_date(row[INTERIM_COL_STOP]),
            "Причина_статус": reason,
            "Источник": worksheet.title,
        })
    return pd.DataFrame(data)


def fetch_spreadsheet_data():
    """
    Подключается к таблице и возвращает (raw_df, interim_df, log).
    raw_df — данные всех основных листов, interim_df — лист промежуточных статусов.
    """
    client = authenticate_with_service_account()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    log = [f"Таблица открыта: {spreadsheet.title}"]

    all_data = []
    for sheet_name in SHEET_NAMES:
        try:
            df = extract_sheet_data(spreadsheet.worksheet(sheet_name))
            if not df.empty:
                all_data.append(df)
                log.append(f"Лист '{sheet_name}': {len(df)} строк")
            else:
                log.append(f"Лист '{sheet_name}': пусто / заголовок не найден")
        except Exception as e:
            log.append(f"Ошибка на листе '{sheet_name}': {e}")

    try:
        interim_df = extract_interim_status_data(
            spreadsheet.worksheet(INTERIM_STATUS_SHEET), only_unpaid_leave=True)
        log.append(
            f"Лист '{INTERIM_STATUS_SHEET}': {len(interim_df)} записей о неоплачиваемых отпусках")
    except Exception as e:
        interim_df = pd.DataFrame()
        log.append(f"Ошибка на листе '{INTERIM_STATUS_SHEET}': {e}")

    raw_df = pd.concat(all_data, ignore_index=True) if all_data else pd.DataFrame()
    return raw_df, interim_df, log


def fetch_snapshot_data(snapshot_date: date | None = None):
    """Собирает данные в формате снепшота (как Парсинг_снепшот_v2)."""
    snapshot_date = snapshot_date or date.today()
    client = authenticate_with_service_account()
    spreadsheet = client.open_by_key(SPREADSHEET_ID)
    log = [f"Таблица открыта: {spreadsheet.title}"]

    parts = []
    for sheet_name in SHEET_NAMES:
        try:
            df = extract_sheet_data(spreadsheet.worksheet(sheet_name))
            if not df.empty:
                parts.append(prepare_snapshot_df(df, snapshot_date, "проект"))
                log.append(f"Лист '{sheet_name}': {len(df)} строк (до фильтрации техлидов)")
        except Exception as e:
            log.append(f"Ошибка на листе '{sheet_name}': {e}")

    try:
        interim_df = extract_interim_status_data(
            spreadsheet.worksheet(INTERIM_STATUS_SHEET), only_unpaid_leave=False)
        if not interim_df.empty:
            parts.append(prepare_snapshot_df(interim_df, snapshot_date, "промежуточный статус"))
            log.append(f"Лист '{INTERIM_STATUS_SHEET}': {len(interim_df)} записей")
    except Exception as e:
        log.append(f"Ошибка на листе '{INTERIM_STATUS_SHEET}': {e}")

    snap_df = pd.concat(parts, ignore_index=True) if parts else \
        pd.DataFrame(columns=SNAPSHOT_COLUMNS)
    log.append(f"После фильтрации невалидных техлидов в снепшоте: {len(snap_df)} строк")
    return snap_df, log


# ---------------------------------------------------------------------------
# Payroll-отчет (логика Парсинг_v4)
# ---------------------------------------------------------------------------

def generate_payroll_reports(raw_df: pd.DataFrame, interim_df: pd.DataFrame | None,
                             year: int, month: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Возвращает (daily_df, summary_df) — ежедневный отчет и сводку за месяц."""
    df = raw_df.copy()

    # 1. Нормализация технологий
    df["Технология"] = df["Технология"].apply(clean_tech_name)
    # 2. Очистка и фильтрация техлидов
    df["Техлид"] = df["Техлид"].apply(clean_fio)
    df = df[df["Техлид"].apply(is_valid_techlead)].copy()
    # 3. Нормализация ФИО лаборантов
    df["Лаборант_норм"] = df["Лаборант"].apply(normalize_fio)
    # 4. Конвертация дат
    df["Дата старта"] = pd.to_datetime(df["Дата старта"], errors="coerce")
    df["Дата стопа"] = pd.to_datetime(df["Дата стопа"], errors="coerce")
    df = df.dropna(subset=["Дата старта"])

    # 5. Обработка неоплачиваемых отпусков
    unpaid_leave_periods = []
    if interim_df is not None and not interim_df.empty:
        interim_df = interim_df.copy()
        interim_df["Дата старта"] = pd.to_datetime(interim_df["Дата старта"], errors="coerce")
        interim_df["Дата стопа"] = pd.to_datetime(interim_df["Дата стопа"], errors="coerce")
        interim_df["Лаборант_норм"] = interim_df["Лаборант"].apply(normalize_fio)
        interim_df["Технология"] = interim_df["Технология"].apply(clean_tech_name)
        interim_df["Техлид"] = interim_df["Техлид"].apply(clean_fio)
        for _, row in interim_df.iterrows():
            if pd.notna(row["Дата старта"]) and pd.notna(row["Дата стопа"]):
                unpaid_leave_periods.append({
                    "Лаборант_норм": row["Лаборант_норм"],
                    "Технология": row["Технология"],
                    "Техлид": row["Техлид"],
                    "start": row["Дата старта"],
                    "end": row["Дата стопа"],
                })

    # 6. Генерация диапазона дат выбранного месяца
    start_date = pd.Timestamp(year=year, month=month, day=1)
    end_date = start_date + pd.DateOffset(months=1) - pd.DateOffset(days=1)
    dates_range = pd.date_range(start_date, end_date)

    daily_records = []
    # 7. Подсчет уникальных лаборантов для каждого дня
    for current_date in dates_range:
        mask = (df["Дата старта"] <= current_date) & \
               ((df["Дата стопа"] >= current_date) | df["Дата стопа"].isna())
        active_df = df[mask].copy()

        # Исключаем стажеров в неоплачиваемом отпуске
        if unpaid_leave_periods:
            for period in unpaid_leave_periods:
                if period["start"] <= current_date <= period["end"]:
                    exclude_mask = (
                        (active_df["Лаборант_норм"] == period["Лаборант_норм"])
                        & (active_df["Технология"] == period["Технология"])
                        & (active_df["Техлид"] == period["Техлид"])
                    )
                    active_df = active_df[~exclude_mask]

        if not active_df.empty:
            # nunique() считает уникальных лаборантов (не строки!)
            counts = active_df.groupby(["Технология", "Техлид"])["Лаборант_норм"] \
                .nunique().reset_index(name="количество лаборантов")
            counts["дата"] = current_date.date()
            daily_records.append(counts)

    if daily_records:
        daily_df = pd.concat(daily_records, ignore_index=True)
        daily_df = daily_df[["Технология", "Техлид", "дата", "количество лаборантов"]]
        daily_df = daily_df.sort_values(by=["дата", "Технология", "Техлид"]).reset_index(drop=True)
    else:
        daily_df = pd.DataFrame(columns=["Технология", "Техлид", "дата", "количество лаборантов"])

    # 8. Сводка за месяц
    summary_df = daily_df.groupby(["Технология", "Техлид"]).agg(
        дней_с_лаборантами=("дата", "count"),
        всего_лаборанто_дней=("количество лаборантов", "sum"),
    ).reset_index()

    period_days = len(dates_range)
    summary_df["среднее_лаборантов_в_день"] = (
        summary_df["всего_лаборанто_дней"] / max(period_days, 1)
    ).round(2)
    summary_df = summary_df.sort_values(by=["Технология", "Техлид"]).reset_index(drop=True)

    return daily_df, summary_df


def save_payroll_report(daily_df: pd.DataFrame, summary_df: pd.DataFrame,
                        year: int, month: int) -> Path:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    filename = EXPORT_DIR / f"payroll_report_{year}_{month:02d}.xlsx"
    with pd.ExcelWriter(filename, engine="openpyxl", datetime_format="YYYY-MM-DD") as writer:
        daily_df.to_excel(writer, sheet_name="Ежедневный_отчет", index=False)
        summary_df.to_excel(writer, sheet_name="Сводка_за_месяц", index=False)
        _autowidth(writer)
    return filename


def _autowidth(writer):
    for sheet_name in writer.sheets:
        ws = writer.sheets[sheet_name]
        for i, col in enumerate(ws.columns):
            max_len = max((len(str(cell.value)) for cell in col if cell.value is not None),
                          default=10)
            ws.column_dimensions[chr(65 + i)].width = min(max_len + 2, 30)


# ---------------------------------------------------------------------------
# Снепшоты (логика Парсинг_снепшот_v2)
# ---------------------------------------------------------------------------

def prepare_snapshot_df(df: pd.DataFrame, snapshot_date: date, kind: str) -> pd.DataFrame:
    """Приводит спарсенные данные к единому формату снепшота."""
    df = df[df["Техлид"].apply(is_valid_techlead)].copy()
    out = pd.DataFrame({
        "Дата_снепшота": snapshot_date.isoformat(),
        "Вид_записи": kind,
        "Источник": df["Источник"].values,
        "Технология_норм": df["Технология"].apply(clean_tech_name).values,
        "Технология_оригинал": df["Технология"].str.strip().values,
        "Техлид_норм": df["Техлид"].apply(clean_fio).values,
        "Техлид_оригинал": df["Техлид"].str.strip().values,
        "Лаборант_норм": df["Лаборант"].apply(normalize_fio).values,
        "Лаборант_оригинал": df["Лаборант"].str.strip().values,
        "Дата_старта": df["Дата старта"].apply(lambda d: d.isoformat() if d else "").values,
        "Дата_стопа": df["Дата стопа"].apply(lambda d: d.isoformat() if d else "").values,
        "Причина_статус": df["Причина_статус"].fillna("").astype(str).str.strip().values,
    })
    return out[SNAPSHOT_COLUMNS]


def save_daily_file(snap_df: pd.DataFrame, snapshot_date: date) -> Path:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOT_DIR / f"snap_{snapshot_date.isoformat()}.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        snap_df.to_excel(writer, sheet_name="Снепшот", index=False)
        for i, col in enumerate(writer.sheets["Снепшот"].columns):
            max_len = max((len(str(c.value)) for c in col if c.value is not None), default=10)
            writer.sheets["Снепшот"].column_dimensions[chr(65 + i)].width = min(max_len + 2, 35)
    return path


def load_history() -> pd.DataFrame:
    if HISTORY_FILE.exists():
        return pd.read_csv(HISTORY_FILE, dtype=str, keep_default_na=False)
    return pd.DataFrame(columns=SNAPSHOT_COLUMNS)


def append_to_history(snap_df: pd.DataFrame, snapshot_date: date) -> int:
    hist = load_history()
    hist = hist[hist["Дата_снепшота"] != snapshot_date.isoformat()]
    combined = pd.concat([hist, snap_df], ignore_index=True)
    combined.to_csv(HISTORY_FILE, index=False, encoding="utf-8-sig")
    return len(combined)


def get_previous_snapshot(snapshot_date: date):
    hist = load_history()
    hist = hist[hist["Дата_снепшота"] < snapshot_date.isoformat()]
    if hist.empty:
        return None, None
    prev_date = hist["Дата_снепшота"].max()
    return prev_date, hist[hist["Дата_снепшота"] == prev_date]


def compute_changes(prev_df: pd.DataFrame, curr_df: pd.DataFrame) -> pd.DataFrame:
    prev = prev_df[KEY_COLS + COMPARE_COLS].drop_duplicates(KEY_COLS)
    curr = curr_df[KEY_COLS + COMPARE_COLS].drop_duplicates(KEY_COLS)
    m = prev.merge(curr, on=KEY_COLS, how="outer",
                   suffixes=("_prev", "_curr"), indicator=True)
    rows = []
    for _, r in m.iterrows():
        key = {c: r[c] for c in KEY_COLS}
        old = " | ".join(str(r[f"{c}_prev"]) for c in COMPARE_COLS)
        new = " | ".join(str(r[f"{c}_curr"]) for c in COMPARE_COLS)
        if r["_merge"] == "right_only":
            rows.append({**key, "Тип_изменения": "добавлен", "Было": "", "Стало": new})
        elif r["_merge"] == "left_only":
            rows.append({**key, "Тип_изменения": "удален", "Было": old, "Стало": ""})
        else:
            if any(r[f"{c}_prev"] != r[f"{c}_curr"] for c in COMPARE_COLS):
                rows.append({**key, "Тип_изменения": "изменен", "Было": old, "Стало": new})
    cols = KEY_COLS + ["Тип_изменения", "Было", "Стало"]
    return pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)


def append_changes(changes_df: pd.DataFrame, snapshot_date: date):
    if changes_df.empty:
        return
    changes_df = changes_df.copy()
    changes_df.insert(0, "Дата_снепшота", snapshot_date.isoformat())
    changes_df = changes_df[CHANGES_COLUMNS]
    if CHANGES_FILE.exists():
        log = pd.read_csv(CHANGES_FILE, dtype=str, keep_default_na=False)
        log = log[log["Дата_снепшота"] != snapshot_date.isoformat()]
        changes_df = pd.concat([log, changes_df], ignore_index=True)
    changes_df.to_csv(CHANGES_FILE, index=False, encoding="utf-8-sig")


def load_changes_log() -> pd.DataFrame:
    if CHANGES_FILE.exists():
        return pd.read_csv(CHANGES_FILE, dtype=str, keep_default_na=False)
    return pd.DataFrame(columns=CHANGES_COLUMNS)


def run_snapshot_job(snapshot_date: date | None = None) -> dict:
    """Полный цикл снепшота: сбор -> сравнение -> сохранение. Возвращает сводку."""
    snapshot_date = snapshot_date or date.today()
    snap_df, log = fetch_snapshot_data(snapshot_date)
    result = {"date": snapshot_date.isoformat(), "rows": len(snap_df), "log": log}

    if snap_df.empty:
        result["error"] = "Нет данных для снепшота"
        return result

    prev_date, prev_df = get_previous_snapshot(snapshot_date)
    if prev_df is not None:
        changes = compute_changes(prev_df, snap_df)
        result["prev_snapshot"] = prev_date
        result["changes"] = {
            t: int((changes["Тип_изменения"] == t).sum())
            for t in ["добавлен", "удален", "изменен"]
        }
        append_changes(changes, snapshot_date)
    else:
        result["prev_snapshot"] = None
        result["changes"] = None

    path = save_daily_file(snap_df, snapshot_date)
    result["file"] = path.name
    result["history_total"] = append_to_history(snap_df, snapshot_date)
    return result


def list_snapshots() -> list[dict]:
    """Список дневных файлов-снепшотов."""
    if not SNAPSHOT_DIR.exists():
        return []
    items = []
    for p in sorted(SNAPSHOT_DIR.glob("snap_*.xlsx"), reverse=True):
        d = p.stem.replace("snap_", "")
        items.append({"name": p.name, "date": d, "size_kb": round(p.stat().st_size / 1024, 1)})
    return items
