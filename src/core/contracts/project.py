from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field


class CostStage(str, Enum):
    ESTIMATE = "投资估算"
    PRELIMINARY_DESIGN = "初步设计概算"
    CONSTRUCTION_DRAWING = "施工图预算"
    BID_CONTROL = "招标控制价"
    BID_QUOTE = "投标报价"
    SETTLEMENT = "竣工结算"


class EngineeringDomain(str, Enum):
    WATER_ECOLOGY = "01_water_ecology"            # 水环境综合治理工程
    URBAN_WATER = "02_urban_water"                # 城乡给排水工程
    IRRIGATION = "03_irrigation"                  # 水利灌区工程
    COAL_MINE_WATER = "04_coal_mine_water"        # 煤矿疏干水工程
    LAND_CONSOLIDATION = "05_land_consolidation"  # 全域土地综合整治工程
    MARINE_ECOLOGY = "06_marine_ecology"          # 海洋生态修复工程
    INSTALLATION_GENERAL = "07_installation"      # 通用安装配套工程


class ProjectFileBundle(BaseModel):
    """文档摄入模块的唯一输入契约：支持成套项目文件聚合解析"""

    project_name: Optional[str] = None
    domain_hint: Optional[EngineeringDomain] = None
    files: list[Path]
    province_hint: Optional[str] = None
    city_hint: Optional[str] = None
    district_hint: Optional[str] = None
    stage_hint: Optional[CostStage] = None
    price_base_month_hint: Optional[str] = None  # YYYY-MM


class MaterialPriceRecord(BaseModel):
    """主要材料基准价契约"""

    material_code: Optional[str] = None
    material_name: str
    spec_model: Optional[str] = None
    unit: str
    base_price: Optional[float] = None             # 定额规定基价（元）
    budget_price_excl_tax: Optional[float] = None  # 除税预算价/市场信息价（元）
    tax_included: bool = False
    region: Optional[str] = None
    price_month: Optional[str] = None


class FeeRateRule(BaseModel):
    """工程类别取费费率契约"""

    engineering_category: str
    temp_facility_rate: Optional[float] = None
    indirect_fee_base: str = "直接费"
    indirect_fee_rate: Optional[float] = None
    profit_rate: Optional[float] = None
    tax_rate: Optional[float] = None


class PartialContext(BaseModel):
    """单个解析器插件抽取出的局部项目上下文，由 IngestionModule 合并"""

    source_file: str
    project_name: Optional[str] = None
    domain: Optional[EngineeringDomain] = None
    province: Optional[str] = None
    city: Optional[str] = None
    district: Optional[str] = None
    stage: Optional[CostStage] = None
    price_base_month: Optional[str] = None
    quota_standards: list[str] = Field(default_factory=list)
    labor_rates: dict[str, float] = Field(default_factory=dict)
    utility_rates: dict[str, float] = Field(default_factory=dict)
    fee_rules: list[FeeRateRule] = Field(default_factory=list)
    base_materials: list[MaterialPriceRecord] = Field(default_factory=list)
    total_investment_yuan: Optional[float] = None


class ProjectContext(BaseModel):
    """L0 项目宏观上下文契约"""

    project_id: str
    project_name: str
    domain: EngineeringDomain
    province: str = "未指定"
    city: Optional[str] = None
    district: Optional[str] = None
    stage: CostStage = CostStage.PRELIMINARY_DESIGN
    price_base_month: str = "2026-01"
    quota_standards: list[str] = Field(default_factory=list)
    labor_rates: dict[str, float] = Field(default_factory=dict)
    utility_rates: dict[str, float] = Field(default_factory=dict)
    fee_rules: list[FeeRateRule] = Field(default_factory=list)
    base_materials: list[MaterialPriceRecord] = Field(default_factory=list)
    total_investment_yuan: Optional[float] = None
    source_files: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
