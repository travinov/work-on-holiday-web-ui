from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.etl_processor import append_delta_from_file

BASE_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = BASE_DIR / "src"
DB_PATH = BASE_DIR / "survey_results.db"
UPLOAD_DIR = BASE_DIR / "generated_exports"
REPORTS_DIR = BASE_DIR / "reports"
TEMPLATES_DIR = BASE_DIR / "templates"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Work On Holiday - Web UI")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def run_script(script_name: str, args: list[str]) -> str:
    cmd = [sys.executable, str(SRC_DIR / script_name), *args]
    completed = subprocess.run(cmd, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "Неизвестная ошибка")
    return (completed.stdout or "").strip()


def get_last_weekend(today: date) -> tuple[date, date]:
    # Always return the last completed Saturday/Sunday interval.
    days_since_sunday = (today.weekday() + 1) % 7
    if days_since_sunday == 0:
        days_since_sunday = 7
    last_sunday = today - timedelta(days=days_since_sunday)
    last_saturday = last_sunday - timedelta(days=1)
    return last_saturday, last_sunday


def read_db_stats() -> dict[str, Any]:
    if not DB_PATH.exists():
        return {"responses": 0, "processed_files": 0}

    with sqlite3.connect(DB_PATH) as conn:
        responses = conn.execute("SELECT COUNT(*) FROM survey_responses").fetchone()[0]
        processed_files = conn.execute("SELECT COUNT(*) FROM ingestion_files").fetchone()[0]

    return {"responses": int(responses), "processed_files": int(processed_files)}


def list_reports() -> list[dict[str, str]]:
    files = [p for p in REPORTS_DIR.glob("*.xlsx") if p.is_file() and not p.name.startswith("~$")]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    result: list[dict[str, str]] = []
    for file_path in files[:30]:
        result.append(
            {
                "name": file_path.name,
                "modified": datetime.fromtimestamp(file_path.stat().st_mtime).strftime("%d.%m.%Y %H:%M"),
            }
        )
    return result


@app.get("/", response_class=HTMLResponse)
def index(request: Request, msg: str | None = None, level: str = "info") -> HTMLResponse:
    weekend_from, weekend_to = get_last_weekend(date.today())
    context = {
        "request": request,
        "msg": msg,
        "level": level,
        "stats": read_db_stats(),
        "reports": list_reports(),
        "default_from": weekend_from.isoformat(),
        "default_to": weekend_to.isoformat(),
    }
    return templates.TemplateResponse("index.html", context)


@app.post("/upload")
async def upload_and_ingest(file: UploadFile = File(...)) -> RedirectResponse:
    if not file.filename:
        return RedirectResponse(url="/?msg=Не выбран файл&level=error", status_code=303)

    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".xlsx", ".xls", ".xlsm", ".csv"}:
        return RedirectResponse(url="/?msg=Неподдерживаемый формат файла&level=error", status_code=303)

    safe_name = file.filename.replace("/", "_").replace("\\", "_")
    stamped_name = f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}__{safe_name}"
    target_path = UPLOAD_DIR / stamped_name

    content = await file.read()
    target_path.write_bytes(content)

    try:
        inserted, total_rows = append_delta_from_file(target_path, str(DB_PATH))
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url=f"/?msg=Ошибка ETL: {exc}&level=error", status_code=303)

    if total_rows == -1:
        return RedirectResponse(url="/?msg=Файл уже был обработан ранее&level=info", status_code=303)

    return RedirectResponse(
        url=f"/?msg=Файл загружен. Новых записей: {inserted} из {total_rows}&level=success",
        status_code=303,
    )


@app.post("/generate/full")
def generate_full_report() -> RedirectResponse:
    output_name = f"Отчеты выхода выходные {date.today().isoformat()}.xlsx"
    output_path = REPORTS_DIR / output_name

    try:
        run_script(
            "build_weekend_reports.py",
            [
                "--db",
                str(DB_PATH),
                "--employees-csv",
                "",
                "--output",
                str(output_path),
            ],
        )
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url=f"/?msg=Ошибка генерации общего отчета: {exc}&level=error", status_code=303)

    return RedirectResponse(url=f"/?msg=Сформирован {output_name}&level=success", status_code=303)


@app.post("/generate/actual")
def generate_actual_report(date_from: str = Form(...), date_to: str = Form(...)) -> RedirectResponse:
    output_name = f"management_report_3_actual_{date_from}_{date_to}.xlsx"
    output_path = REPORTS_DIR / output_name

    try:
        run_script(
            "report_third_closure.py",
            [
                "--db",
                str(DB_PATH),
                "--date-from",
                date_from,
                "--date-to",
                date_to,
                "--output",
                str(output_path),
            ],
        )
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url=f"/?msg=Ошибка генерации отчета 3: {exc}&level=error", status_code=303)

    return RedirectResponse(url=f"/?msg=Сформирован {output_name}&level=success", status_code=303)


@app.post("/generate/reconciliation")
def generate_reconciliation_report(date_from: str = Form(...), date_to: str = Form(...)) -> RedirectResponse:
    output_name = f"management_report_4_reconciliation_{date_from}_{date_to}.xlsx"
    output_path = REPORTS_DIR / output_name

    try:
        run_script(
            "report_four_reconciliation.py",
            [
                "--db",
                str(DB_PATH),
                "--date-from",
                date_from,
                "--date-to",
                date_to,
                "--output",
                str(output_path),
            ],
        )
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(url=f"/?msg=Ошибка генерации сверки: {exc}&level=error", status_code=303)

    return RedirectResponse(url=f"/?msg=Сформирован {output_name}&level=success", status_code=303)


@app.get("/download/{filename}")
def download_report(filename: str) -> FileResponse:
    target = REPORTS_DIR / Path(filename).name
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(path=target, filename=target.name)
