import asyncio
import os
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .collector import collect_sources
from .database import (delete_fixture_suppliers, get_state, get_supplier, init_db,
                       list_categories, list_states, list_suppliers, list_types,
                       list_search_history, clear_search_history, record_search,
                       update_state, upsert_many, safe_upsert,
                       list_pending_updates, resolve_pending_update,
                       archive_supplier, list_archived_suppliers,
                       restore_archived_supplier, delete_archived_supplier,
                       storage_status)
from .importers import parse_import_file
from .models import (CompareRequest, ExportRequest, ImportFileRequest, ImportRequest,
                     ParseRequest, SearchRequest, SupplierStateUpdate)
from .parsers.generic import parse_supplier
from .search import missing_fields, search
from .semantic import semantic_engine
from .xlsx_export import export_suppliers_xlsx

app = FastAPI(title="FoodSource API", version="0.1.0", description="Поиск и сбор данных о поставщиках продуктов питания")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

collection_job = {
    "status": "idle",
    "started_at": None,
    "finished_at": None,
    "result": None,
    "error": None,
}
collection_task: asyncio.Task | None = None
parse_jobs: dict[str, dict] = {}
parse_tasks: dict[str, asyncio.Task] = {}


def collection_status() -> dict:
    """Return a JSON-safe snapshot without exposing the asyncio task itself."""
    return dict(collection_job)


async def run_collection_job() -> None:
    collection_job.update({
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "result": None,
        "error": None,
    })
    try:
        result = await collect_sources()
        collection_job.update({
            "status": "completed",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "result": result,
        })
    except Exception as exc:
        collection_job.update({
            "status": "failed",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "error": str(exc),
        })


def parse_job_status(job_id: str) -> dict:
    job = parse_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Задача парсинга не найдена")
    return dict(job)


async def run_parse_job(job_id: str, request: ParseRequest) -> None:
    job = parse_jobs[job_id]
    job.update({"status": "running", "started_at": datetime.now(timezone.utc).isoformat()})
    try:
        item = await parse_supplier(str(request.url), request.category, request.region)
        job.update({
            "status": "completed",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "result": safe_upsert(item, source_mode="manual_parse"),
        })
    except Exception as exc:
        job.update({
            "status": "failed",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "error": f"Не удалось разобрать страницу: {exc}",
        })
    finally:
        # Keep recent results available for polling, but bound process memory.
        completed = [key for key, value in parse_jobs.items() if value["status"] in {"completed", "failed"}]
        for old_id in completed[:-20]:
            parse_jobs.pop(old_id, None)
            parse_tasks.pop(old_id, None)


@app.on_event("startup")
def startup() -> None:
    init_db()
    if os.getenv("FOODSOURCE_PREWARM", "1").strip().lower() in {"1", "true", "yes", "on"}:
        semantic_engine.prewarm(list_suppliers())


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "foodsource-api",
        "storage": storage_status(),
        "search": semantic_engine.status(load=True),
        "query_understanding": {
            "ontology": "foodsource-domain-v1",
            "typo_correction": "rapidfuzz",
            "morphology": "pymorphy3",
            "strict_category_guardrails": True,
            "explainable_ranking": True,
        },
    }


@app.get("/api/suppliers")
def suppliers() -> dict:
    items = list_suppliers()
    return {"items": items, "count": len(items)}


@app.get("/api/suppliers/{supplier_id}")
def supplier(supplier_id: str) -> dict:
    item = get_supplier(supplier_id)
    if not item:
        raise HTTPException(status_code=404, detail="Поставщик не найден")
    return {"item": item, "state": get_state(supplier_id), "missing_fields": missing_fields(item)}


@app.get("/api/archive")
def archived_suppliers() -> dict:
    items = list_archived_suppliers()
    return {"items": items, "count": len(items)}


@app.post("/api/suppliers/{supplier_id}/archive")
def archive_supplier_card(supplier_id: str) -> dict:
    result = archive_supplier(supplier_id)
    if not result:
        raise HTTPException(status_code=404, detail="Поставщик не найден")
    return result


@app.post("/api/archive/{supplier_id}/restore")
def restore_supplier_card(supplier_id: str) -> dict:
    item = restore_archived_supplier(supplier_id)
    if not item:
        raise HTTPException(status_code=404, detail="Карточка не найдена в архиве")
    return {"item": item}


@app.delete("/api/archive/{supplier_id}")
def delete_supplier_card(supplier_id: str) -> dict:
    try:
        item = delete_archived_supplier(supplier_id)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not item:
        raise HTTPException(status_code=404, detail="Карточка не найдена в архиве")
    return {"deleted": True, "id": supplier_id, "name": item.get("name", "")}


@app.post("/api/search")
def supplier_search(request: SearchRequest) -> dict:
    normalized, items = search(list_suppliers(), request.query, request.filters.model_dump())
    record_search(request.query, normalized, request.filters.model_dump(), len(items))
    return {
        "query": request.query,
        "normalized_query": normalized,
        "search": semantic_engine.status(),
        "items": items,
        "count": len(items),
    }


@app.get("/api/search/status")
def search_status() -> dict:
    """Expose the active search engine for local diagnostics and the UI."""
    return semantic_engine.status(load=True)


@app.get("/api/search-history")
def search_history(limit: int = 5) -> dict:
    return {"items": list_search_history(limit)}


@app.delete("/api/search-history")
def delete_search_history() -> dict:
    return {"deleted": clear_search_history()}


@app.get("/api/categories")
def categories() -> dict:
    return {"categories": list_categories(), "supplier_types": list_types()}


@app.get("/api/supplier-states")
def supplier_states() -> dict:
    return {"items": list_states()}


@app.get("/api/suppliers/{supplier_id}/state")
def supplier_state(supplier_id: str) -> dict:
    if not get_supplier(supplier_id):
        raise HTTPException(status_code=404, detail="Поставщик не найден")
    return get_state(supplier_id)


@app.put("/api/suppliers/{supplier_id}/state")
def save_supplier_state(supplier_id: str, request: SupplierStateUpdate) -> dict:
    if not get_supplier(supplier_id):
        raise HTTPException(status_code=404, detail="Поставщик не найден")
    try:
        return update_state(supplier_id, request.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/compare")
def compare(request: CompareRequest) -> dict:
    items = []
    for supplier_id in request.supplier_ids:
        item = get_supplier(supplier_id)
        if not item:
            raise HTTPException(status_code=404, detail=f"Поставщик {supplier_id} не найден")
        items.append({**item, "missing_fields": missing_fields(item)})
    return {"items": items}


@app.post("/api/export/xlsx")
def export_xlsx(request: ExportRequest, background_tasks: BackgroundTasks):
    items = []
    for supplier_id in request.supplier_ids:
        item = get_supplier(supplier_id)
        if item:
            items.append(item)
    if not items:
        raise HTTPException(status_code=404, detail="Для экспорта не найдено ни одного поставщика")
    project_root = Path(__file__).resolve().parents[1]
    export_dir = project_root / "data" / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    output_path = export_dir / f"foodsource-selection-{date.today().isoformat()}-{token[:8]}.xlsx"
    try:
        export_suppliers_xlsx(items, output_path)
    except Exception as exc:
        output_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Не удалось сформировать XLSX: {exc}") from exc
    background_tasks.add_task(output_path.unlink, missing_ok=True)
    return FileResponse(output_path, filename="foodsource-selection.xlsx", media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", background=background_tasks)


@app.get("/api/data-quality")
def data_quality() -> dict:
    items = list_suppliers()
    report_path = __import__("pathlib").Path(__file__).resolve().parents[1] / "data" / "collection-report.json"
    try:
        report = __import__("json").loads(report_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        report = None
    cards = [{"id": item["id"], "name": item["name"], "updated": item.get("updated", ""), "completeness": item.get("completeness", 0), "missing_fields": missing_fields(item), "source": item.get("source", "")} for item in items]
    pending = list_pending_updates()
    return {"total": len(items), "average_completeness": round(sum(item.get("completeness", 0) for item in items) / len(items)) if items else 0, "suppliers": cards, "last_collection": report, "pending_updates": pending, "pending_count": len(pending)}


@app.get("/api/pending-updates")
def pending_updates() -> dict:
    items = list_pending_updates()
    return {"items": items, "count": len(items)}


@app.post("/api/pending-updates/{review_id}/approve")
def approve_pending_update(review_id: int) -> dict:
    result = resolve_pending_update(review_id, approve=True)
    if not result:
        raise HTTPException(status_code=404, detail="Обновление не найдено или уже обработано")
    return result


@app.post("/api/pending-updates/{review_id}/reject")
def reject_pending_update(review_id: int) -> dict:
    result = resolve_pending_update(review_id, approve=False)
    if not result:
        raise HTTPException(status_code=404, detail="Обновление не найдено или уже обработано")
    return result


@app.post("/api/parse", status_code=202)
async def parse(request: ParseRequest) -> dict:
    job_id = uuid.uuid4().hex
    parse_jobs[job_id] = {
        "id": job_id, "status": "queued", "started_at": None,
        "finished_at": None, "result": None, "error": None,
    }
    parse_tasks[job_id] = asyncio.create_task(run_parse_job(job_id, request))
    await asyncio.sleep(0)
    return parse_job_status(job_id)


@app.get("/api/parse/status/{job_id}")
def parse_status(job_id: str) -> dict:
    return parse_job_status(job_id)


@app.post("/api/import")
def import_suppliers(request: ImportRequest) -> dict:
    count = upsert_many([item.model_dump() for item in request.suppliers])
    return {"imported": count, "total": len(list_suppliers())}


@app.post("/api/import/file")
def import_supplier_file(request: ImportFileRequest) -> dict:
    try:
        items, errors = parse_import_file(request.filename, request.content_base64)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    count = upsert_many(items)
    return {"imported": count, "skipped": len(errors), "errors": errors, "total": len(list_suppliers())}


@app.delete("/api/suppliers/demo")
def delete_demo_suppliers() -> dict:
    deleted = delete_fixture_suppliers()
    return {"deleted": deleted, "total": len(list_suppliers())}


@app.post("/api/collect", status_code=202)
async def collect() -> dict:
    """Start a non-blocking refresh of the reviewed URLs in sources.json."""
    global collection_task
    if collection_task is None or collection_task.done():
        collection_task = asyncio.create_task(run_collection_job())
        # Give the task one event-loop turn so the returned state is `running`.
        await asyncio.sleep(0)
    return collection_status()


@app.get("/api/collect/status")
def collect_status() -> dict:
    return collection_status()
