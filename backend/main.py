import json
import shutil
import subprocess
import sys
import uuid
from datetime import date
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .collector import collect_sources
from .database import (delete_fixture_suppliers, get_state, get_supplier, init_db,
                       list_categories, list_states, list_suppliers, list_types,
                       list_search_history, clear_search_history, record_search, update_state, upsert_many)
from .importers import parse_import_file
from .models import (CompareRequest, ExportRequest, ImportFileRequest, ImportRequest,
                     ParseRequest, SearchRequest, SupplierStateUpdate)
from .parsers.generic import parse_supplier
from .search import missing_fields, search

app = FastAPI(title="FoodSource API", version="0.1.0", description="Поиск и сбор данных о поставщиках продуктов питания")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "service": "foodsource-api"}


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


@app.post("/api/search")
def supplier_search(request: SearchRequest) -> dict:
    normalized, items = search(list_suppliers(), request.query, request.filters.model_dump())
    record_search(request.query, normalized, request.filters.model_dump(), len(items))
    return {"query": request.query, "normalized_query": normalized, "items": items, "count": len(items)}


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
    request_path = export_dir / f"request-{token}.json"
    output_path = export_dir / f"foodsource-selection-{date.today().isoformat()}-{token[:8]}.xlsx"
    request_path.write_text(json.dumps({"title": "Выборка поставщиков FoodSource", "generatedAt": date.today().isoformat(), "suppliers": items}, ensure_ascii=False), encoding="utf-8")
    node = shutil.which("node")
    if not node:
        request_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="Node.js не найден: экспорт XLSX недоступен")
    try:
        subprocess.run([node, str(project_root / "backend" / "export_xlsx.mjs"), str(request_path), str(output_path)], cwd=project_root, check=True, capture_output=True, text=True, timeout=30)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        request_path.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Не удалось сформировать XLSX: {getattr(exc, 'stderr', '') or exc}") from exc
    request_path.unlink(missing_ok=True)
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
    return {"total": len(items), "average_completeness": round(sum(item.get("completeness", 0) for item in items) / len(items)) if items else 0, "suppliers": cards, "last_collection": report}


@app.post("/api/parse")
async def parse(request: ParseRequest) -> dict:
    try:
        item = await parse_supplier(str(request.url), request.category, request.region)
        upsert_many([item])
        return {"item": item}
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Не удалось разобрать страницу: {exc}") from exc


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


@app.post("/api/collect")
async def collect() -> dict:
    """Refresh only the reviewed public URLs listed in data/sources.json."""
    return await collect_sources()
