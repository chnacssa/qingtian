"""
产品目录/价目表/图片/文档 — Pydantic 模型
"""

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field


# ── 产品目录 ──────────────────────────────────────


class ProductCreate(BaseModel):
    enterprise_id: str = Field(..., min_length=1)
    category: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    model: str = ""
    voltage_level: str = ""
    power_rating: str = ""
    standards: list[str] = []
    technical_params: dict[str, str] = {}
    accessories: list[str] = []
    certification_required: list[str] = []
    unit: str = "台"
    created_by: str = ""


class ProductUpdate(BaseModel):
    category: Optional[str] = None
    name: Optional[str] = None
    model: Optional[str] = None
    voltage_level: Optional[str] = None
    power_rating: Optional[str] = None
    standards: Optional[list[str]] = None
    technical_params: Optional[dict[str, str]] = None
    accessories: Optional[list[str]] = None
    certification_required: Optional[list[str]] = None
    unit: Optional[str] = None
    status: Optional[str] = None
    updated_by: str = ""


class ProductResponse(BaseModel):
    product_id: str
    enterprise_id: str
    category: str
    name: str
    model: str
    voltage_level: str
    power_rating: str
    standards: list[str]
    technical_params: dict
    accessories: list[str]
    certification_required: list[str]
    unit: str
    status: str
    created_by: str
    created_at: datetime
    updated_at: datetime


# ── 产品图片 ──────────────────────────────────────


class ProductImageCreate(BaseModel):
    """上传图片时从表单解析"""
    file_id: str = Field(..., min_length=1)
    filename: str = Field(..., min_length=1)
    is_primary: bool = False
    sort_order: int = 0
    file_size: int = 0
    image_sha256: str = ""
    mime_type: str = ""
    created_by: str = ""


class ProductImageResponse(BaseModel):
    image_id: str
    product_id: str
    file_id: str
    filename: str
    is_primary: bool
    sort_order: int
    file_size: int
    mime_type: str
    created_at: datetime


# ── 价目表 ────────────────────────────────────────


class PriceListCreate(BaseModel):
    enterprise_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    valid_from: date
    valid_until: Optional[date] = None
    daily_update: bool = False
    source: str = "manual"
    source_file_id: str = ""
    created_by: str = ""


class PriceListUpdate(BaseModel):
    name: Optional[str] = None
    valid_from: Optional[date] = None
    valid_until: Optional[date] = None
    daily_update: Optional[bool] = None
    approved_by: str = ""


class PriceListResponse(BaseModel):
    price_list_id: str
    enterprise_id: str
    name: str
    version: int
    valid_from: date
    valid_until: Optional[date]
    status: str
    source: str
    source_file_id: str
    daily_update: bool
    created_by: str
    approved_by: str
    approved_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    items: list["PriceListItemResponse"] = []


class PriceListItemCreate(BaseModel):
    product_id: Optional[str] = None
    product_spec: dict = {}
    unit_price: float = Field(..., gt=0)
    currency: str = "CNY"
    quantity_discount: dict = {}
    valid_from: Optional[date] = None
    valid_until: Optional[date] = None
    sort_order: int = 0


class PriceListItemUpdate(BaseModel):
    unit_price: Optional[float] = None
    currency: Optional[str] = None
    quantity_discount: Optional[dict] = None
    valid_from: Optional[date] = None
    valid_until: Optional[date] = None
    sort_order: Optional[int] = None


class PriceListItemResponse(BaseModel):
    item_id: int
    price_list_id: str
    product_id: Optional[str]
    product_spec: dict
    unit_price: float
    currency: str
    quantity_discount: dict
    valid_from: Optional[date]
    valid_until: Optional[date]
    sort_order: int
    created_at: datetime


# ── 企业文档 ──────────────────────────────────────


class DocumentCreate(BaseModel):
    enterprise_id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    document_type: str = Field(..., pattern=r'^(contract_template|qualification|certificate|brochure|spec_sheet|other)$')
    file_id: str = Field(..., min_length=1)
    filename: str = Field(..., min_length=1)
    file_size: int = 0
    file_sha256: str = ""
    tags: list[str] = []
    visibility: str = "enterprise"
    owner_agent: str = ""
    valid_until: Optional[date] = None
    description: str = ""


class DocumentUpdate(BaseModel):
    title: Optional[str] = None
    document_type: Optional[str] = None
    tags: Optional[list[str]] = None
    visibility: Optional[str] = None
    valid_until: Optional[date] = None
    description: Optional[str] = None
    status: Optional[str] = None


class DocumentResponse(BaseModel):
    document_id: str
    enterprise_id: str
    title: str
    document_type: str
    file_id: str
    filename: str
    file_size: int
    tags: list[str]
    visibility: str
    owner_agent: str
    valid_until: Optional[date]
    status: str
    description: str
    created_at: datetime
    updated_at: datetime
