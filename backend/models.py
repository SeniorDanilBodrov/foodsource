from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, HttpUrl


class Supplier(BaseModel):
    id: str
    name: str
    type: str = "Поставщик"
    categories: list[str] = Field(default_factory=list)
    products: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    city: str = ""
    description: str = ""
    minimumOrder: str = "Не указано"
    priceRange: str = "По запросу"
    delivery: str = "Уточнить"
    certificates: list[str] = Field(default_factory=list)
    contact: str = ""
    phone: str = ""
    email: str = ""
    address: str = ""
    website: str = ""
    source: str = ""
    source_url: str = ""
    source_name: str = ""
    updated: str = ""
    completeness: int = 0


class ParseRequest(BaseModel):
    url: HttpUrl
    category: Optional[str] = None
    region: Optional[str] = None


class ImportRequest(BaseModel):
    suppliers: list[Supplier]


class ImportFileRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(min_length=1)


class SearchFilters(BaseModel):
    category: Optional[str] = None
    region: Optional[str] = None
    supplier_type: Optional[str] = None
    has_delivery: bool = False
    has_certificates: bool = False
    has_minimum_order: bool = False
    has_contact: bool = False
    sort: str = "match"


class SearchRequest(BaseModel):
    query: str = ""
    filters: SearchFilters = Field(default_factory=SearchFilters)


class CompareRequest(BaseModel):
    supplier_ids: list[str] = Field(min_length=2, max_length=4)


class SupplierStateUpdate(BaseModel):
    favourite: Optional[bool] = None
    status: Optional[str] = None
    note: Optional[str] = None


class ExportRequest(BaseModel):
    supplier_ids: list[str] = Field(min_length=1)
