from typing import Any

from src.core.contracts.boq_item import (
    CanonicalProject,
    QuotaResourceLine,
    RawBoqItem,
    UnitPriceBreakdown,
)
from src.core.contracts.project import (
    CostStage,
    EngineeringDomain,
    FeeRateRule,
    MaterialPriceRecord,
    ProjectContext,
)


def _make_bd(
    quota_code: str,
    unit: str,
    unit_price: float,
    main_mat_name: str,
    main_mat_unit: str,
    main_mat_qty: float,
    main_mat_price: float,
    labor_days: float,
    labor_rate: float = 128.0,
    diesel_kg: float = 0.0,
) -> UnitPriceBreakdown:
    labor_sub = round(labor_days * labor_rate, 2)
    mat_sub = round(main_mat_qty * main_mat_price, 2)
    mach_sub = round(max(0.0, unit_price * 0.85 - labor_sub - mat_sub), 2)
    lines: list[QuotaResourceLine] = [
        QuotaResourceLine(
            resource_type="人工",
            name="综合工日",
            unit="工日",
            quantity=labor_days,
            unit_price=labor_rate,
            subtotal=labor_sub,
        ),
        QuotaResourceLine(
            resource_type="材料",
            name=main_mat_name,
            unit=main_mat_unit,
            quantity=main_mat_qty,
            unit_price=main_mat_price,
            subtotal=mat_sub,
        ),
    ]
    if diesel_kg > 0:
        lines.append(
            QuotaResourceLine(
                resource_type="价差",
                name="柴油",
                unit="kg",
                quantity=diesel_kg,
                unit_price=7.4,
                subtotal=round(diesel_kg * 7.4, 2),
            )
        )
    if mach_sub > 0:
        lines.append(
            QuotaResourceLine(
                resource_type="机械",
                name="专业施工机械",
                unit="台班",
                quantity=round(mach_sub / 420.0, 4),
                unit_price=420.0,
                subtotal=mach_sub,
            )
        )
    return UnitPriceBreakdown(
        quota_codes=[quota_code],
        quota_unit=f"1{unit}",
        direct_labor_fee=labor_sub,
        direct_material_fee=mat_sub,
        direct_machinery_fee=mach_sub,
        measure_rate_pct=4.5,
        indirect_rate_pct=7.5,
        profit_rate_pct=5.0,
        tax_rate_pct=9.0,
        quota_total_yuan=round(unit_price, 2),
        resource_lines=lines,
    )


def build_four_domain_reference_projects(standardizer: Any) -> list[CanonicalProject]:
    """生成 02城乡给排水、03水利灌区、04煤矿疏干水、06海洋生态 4 大缺失领域共 80 条标准含 L2 定额构成本底项目."""
    specs: list[tuple[ProjectContext, list[RawBoqItem]]] = [
        # 1. 02_urban_water: 20 项
        (
            ProjectContext(
                project_id="ref_urban_water_02",
                project_name="西安市皂河片区城乡雨污分流管网改造工程",
                domain=EngineeringDomain.URBAN_WATER,
                province="陕西省",
                city="西安市",
                stage=CostStage.PRELIMINARY_DESIGN,
                price_base_month="2026-08",
                quota_standards=["《陕西省市政工程消耗量定额》", "GB50500-2024 建设工程工程量清单计价标准"],
                labor_rates={"综合工日": 135.0},
                fee_rules=[FeeRateRule(engineering_category="市政给排水工程", temp_facility_rate=2.2, indirect_fee_rate=6.8, profit_rate=5.0, tax_rate=9.0)],
                base_materials=[
                    MaterialPriceRecord(material_name="商品混凝土", unit="m³", base_price=370.0, budget_price_excl_tax=415.0),
                    MaterialPriceRecord(material_name="钢筋", unit="t", base_price=3500.0, budget_price_excl_tax=3780.0),
                    MaterialPriceRecord(material_name="柴油", unit="kg", base_price=6.5, budget_price_excl_tax=7.4),
                ],
            ),
            [
                RawBoqItem(seq_no="1.1", section_path=["市政污水管网"], raw_name="HDPE双壁波纹管安装", raw_features="管材:HDPE双壁波纹管 管径:DN300 环刚度:SN8 接口:橡胶圈承插", raw_unit="m", quantity=680.0, unit_price_yuan=182.40, total_price_yuan=124032.0, breakdown=_make_bd("SZ-5-12", "m", 182.40, "HDPE双壁波纹管 DN300", "m", 1.02, 118.0, 0.24, 135.0)),
                RawBoqItem(seq_no="1.2", section_path=["市政污水管网"], raw_name="HDPE双壁波纹管安装", raw_features="管材:HDPE双壁波纹管 管径:DN400 环刚度:SN8 接口:橡胶圈承插", raw_unit="m", quantity=1240.0, unit_price_yuan=268.50, total_price_yuan=332940.0, breakdown=_make_bd("SZ-5-14", "m", 268.50, "HDPE双壁波纹管 DN400", "m", 1.02, 185.0, 0.30, 135.0)),
                RawBoqItem(seq_no="1.3", section_path=["市政污水管网"], raw_name="HDPE双壁波纹管安装", raw_features="管材:HDPE双壁波纹管 管径:DN600 环刚度:SN12.5 接口:橡胶圈承插", raw_unit="m", quantity=520.0, unit_price_yuan=516.80, total_price_yuan=268736.0, breakdown=_make_bd("SZ-5-18", "m", 516.80, "HDPE双壁波纹管 DN600", "m", 1.02, 376.0, 0.48, 135.0)),
                RawBoqItem(seq_no="1.4", section_path=["市政给水管网"], raw_name="球墨铸铁给水管道安装", raw_features="管材:球墨铸铁管 管径:DN200 压力等级:K9 接口:橡胶圈承插", raw_unit="m", quantity=850.0, unit_price_yuan=312.60, total_price_yuan=265710.0, breakdown=_make_bd("SZ-5-41", "m", 312.60, "球墨铸铁管 DN200", "m", 1.01, 224.0, 0.28, 135.0)),
                RawBoqItem(seq_no="1.5", section_path=["市政给水管网"], raw_name="球墨铸铁给水管道安装", raw_features="管材:球墨铸铁管 管径:DN400 压力等级:K9 接口:橡胶圈承插", raw_unit="m", quantity=640.0, unit_price_yuan=695.40, total_price_yuan=445056.0, breakdown=_make_bd("SZ-5-44", "m", 695.40, "球墨铸铁管 DN400", "m", 1.01, 520.0, 0.52, 135.0)),
                RawBoqItem(seq_no="1.6", section_path=["市政给水管网"], raw_name="PE给水管安装", raw_features="管材:PE给水管 管径:DN160 压力等级:PN1.6 接口:热熔连接", raw_unit="m", quantity=920.0, unit_price_yuan=118.50, total_price_yuan=109020.0, breakdown=_make_bd("SZ-5-62", "m", 118.50, "PE管 DN160", "m", 1.03, 74.0, 0.16, 135.0)),
                RawBoqItem(seq_no="1.7", section_path=["市政雨水管网"], raw_name="钢筋混凝土排水管安装", raw_features="管材:钢筋混凝土管 管径:DN800 环刚度:SN10 接口:柔性接口", raw_unit="m", quantity=410.0, unit_price_yuan=642.00, total_price_yuan=263220.0, breakdown=_make_bd("SZ-5-75", "m", 642.00, "钢筋混凝土管 DN800", "m", 1.01, 412.0, 0.75, 135.0)),
                RawBoqItem(seq_no="1.8", section_path=["市政雨水管网"], raw_name="钢筋混凝土排水管安装", raw_features="管材:钢筋混凝土管 管径:DN1200 环刚度:SN12 接口:柔性接口", raw_unit="m", quantity=280.0, unit_price_yuan=1140.00, total_price_yuan=319200.0, breakdown=_make_bd("SZ-5-79", "m", 1140.00, "钢筋混凝土管 DN1200", "m", 1.01, 765.0, 1.15, 135.0)),
                RawBoqItem(seq_no="2.1", section_path=["管网检查井与顶管"], raw_name="砖砌检查井", raw_features="砖砌检查井 井径:DN1000 井深:3.0m", raw_unit="座", quantity=28.0, unit_price_yuan=4680.00, total_price_yuan=131040.0, breakdown=_make_bd("SZ-5-102", "座", 4680.00, "标准砖", "千块", 3.2, 410.0, 14.5, 135.0)),
                RawBoqItem(seq_no="2.2", section_path=["管网检查井与顶管"], raw_name="钢筋混凝土检查井", raw_features="钢筋混凝土检查井 井径:DN1500 井深:4.5m", raw_unit="座", quantity=16.0, unit_price_yuan=11250.00, total_price_yuan=180000.0, breakdown=_make_bd("SZ-5-108", "座", 11250.00, "商品混凝土", "m³", 9.8, 415.0, 26.0, 135.0)),
                RawBoqItem(seq_no="2.3", section_path=["管网检查井与顶管"], raw_name="矩形检查井", raw_features="矩形检查井 井径:1800×1800 井深:4.0m", raw_unit="座", quantity=8.0, unit_price_yuan=14800.00, total_price_yuan=118400.0, breakdown=_make_bd("SZ-5-110", "座", 14800.00, "商品混凝土", "m³", 13.4, 415.0, 32.0, 135.0)),
                RawBoqItem(seq_no="2.4", section_path=["管网检查井与顶管"], raw_name="非开挖拉管工程", raw_features="非开挖拉管 PE管 管径:DN315", raw_unit="m", quantity=160.0, unit_price_yuan=890.00, total_price_yuan=142400.0, breakdown=_make_bd("SZ-5-128", "m", 890.00, "PE管 DN315", "m", 1.03, 310.0, 1.2, 135.0, diesel_kg=4.2)),
                RawBoqItem(seq_no="2.5", section_path=["管网检查井与顶管"], raw_name="顶管工程", raw_features="顶管 钢筋混凝土管 管径:DN1000 井深:5.5m", raw_unit="m", quantity=210.0, unit_price_yuan=3280.00, total_price_yuan=688800.0, breakdown=_make_bd("SZ-5-135", "m", 3280.00, "顶管钢筋混凝土管 DN1000", "m", 1.02, 1180.0, 4.6, 135.0, diesel_kg=9.5)),
                RawBoqItem(seq_no="2.6", section_path=["管网检查井与顶管"], raw_name="偏沟式雨水口", raw_features="偏沟式雨水口 双箅 井深:1.2m", raw_unit="座", quantity=64.0, unit_price_yuan=1560.00, total_price_yuan=99840.0, breakdown=_make_bd("SZ-5-142", "座", 1560.00, "标准砖", "千块", 0.65, 410.0, 4.8, 135.0)),
                RawBoqItem(seq_no="3.1", section_path=["阀门与厂站设备"], raw_name="弹性座封闸阀", raw_features="弹性座封闸阀 口径:DN200 PN1.6 法兰连接", raw_unit="个", quantity=14.0, unit_price_yuan=2460.00, total_price_yuan=34440.0, breakdown=_make_bd("SZ-5-161", "个", 2460.00, "弹性座封闸阀 DN200", "个", 1.0, 1980.0, 1.4, 135.0)),
                RawBoqItem(seq_no="3.2", section_path=["阀门与厂站设备"], raw_name="蝶阀安装", raw_features="蝶阀 口径:DN400 PN1.6 法兰连接", raw_unit="个", quantity=8.0, unit_price_yuan=4820.00, total_price_yuan=38560.0, breakdown=_make_bd("SZ-5-165", "个", 4820.00, "法兰蝶阀 DN400", "个", 1.0, 3950.0, 2.2, 135.0)),
                RawBoqItem(seq_no="3.3", section_path=["阀门与厂站设备"], raw_name="复合式排气阀", raw_features="复合式排气阀 口径:DN100 法兰连接", raw_unit="个", quantity=12.0, unit_price_yuan=1680.00, total_price_yuan=20160.0, breakdown=_make_bd("SZ-5-171", "个", 1680.00, "排气阀 DN100", "个", 1.0, 1290.0, 0.9, 135.0)),
                RawBoqItem(seq_no="3.4", section_path=["阀门与厂站设备"], raw_name="地上式消火栓", raw_features="地上式消火栓 口径:DN150 SS150", raw_unit="套", quantity=18.0, unit_price_yuan=2380.00, total_price_yuan=42840.0, breakdown=_make_bd("SZ-5-178", "套", 2380.00, "地上消火栓 SS150", "套", 1.0, 1740.0, 1.8, 135.0)),
                RawBoqItem(seq_no="3.5", section_path=["阀门与厂站设备"], raw_name="潜污泵安装", raw_features="潜污泵 功率:22kW 扬程18m", raw_unit="台", quantity=4.0, unit_price_yuan=28600.00, total_price_yuan=114400.0, breakdown=_make_bd("SZ-6-18", "台", 28600.00, "潜污泵 22kW", "台", 1.0, 24200.0, 10.5, 135.0)),
                RawBoqItem(seq_no="3.6", section_path=["阀门与厂站设备"], raw_name="一体化污水处理设备", raw_features="一体化污水处理设备 处理量:200m³/d A/O工艺", raw_unit="套", quantity=2.0, unit_price_yuan=348000.00, total_price_yuan=696000.0, breakdown=_make_bd("SZ-6-45", "套", 348000.00, "一体化污水处理设备 200m³/d", "套", 1.0, 298000.0, 85.0, 135.0)),
            ],
        ),
        # 2. 03_irrigation: 20 项
        (
            ProjectContext(
                project_id="ref_irrigation_03",
                project_name="引黄现代节水灌区续建配套与渠道衬砌改造工程",
                domain=EngineeringDomain.IRRIGATION,
                province="宁夏回族自治区",
                city="吴忠市",
                stage=CostStage.PRELIMINARY_DESIGN,
                price_base_month="2026-06",
                quota_standards=["水总〔2024〕323号《水利工程设计概（估）算编制规定》", "水利建筑工程概算定额"],
                labor_rates={"综合工日": 116.0},
                fee_rules=[FeeRateRule(engineering_category="灌区建筑工程", temp_facility_rate=2.0, indirect_fee_rate=8.5, profit_rate=5.0, tax_rate=9.0)],
                base_materials=[
                    MaterialPriceRecord(material_name="商品混凝土", unit="m³", base_price=360.0, budget_price_excl_tax=395.0),
                    MaterialPriceRecord(material_name="钢筋", unit="t", base_price=3450.0, budget_price_excl_tax=3720.0),
                    MaterialPriceRecord(material_name="柴油", unit="kg", base_price=6.4, budget_price_excl_tax=7.3),
                ],
            ),
            [
                RawBoqItem(seq_no="1.1", section_path=["灌区输水渠道衬砌"], raw_name="混凝土渠道衬砌", raw_features="现浇混凝土渠道 C25 厚10cm F200 W6 干渠衬砌", raw_unit="m³", quantity=2400.0, unit_price_yuan=618.50, total_price_yuan=1484400.0, breakdown=_make_bd("SL-GQ-101", "m³", 618.50, "商品混凝土", "m³", 1.02, 395.0, 0.85, 116.0)),
                RawBoqItem(seq_no="1.2", section_path=["灌区输水渠道衬砌"], raw_name="混凝土渠道衬砌", raw_features="梯形现浇渠道 C20 厚8cm F150 支渠衬砌", raw_unit="m³", quantity=1650.0, unit_price_yuan=572.00, total_price_yuan=943800.0, breakdown=_make_bd("SL-GQ-103", "m³", 572.00, "商品混凝土", "m³", 1.02, 375.0, 0.78, 116.0)),
                RawBoqItem(seq_no="1.3", section_path=["灌区输水渠道衬砌"], raw_name="混凝土渠道衬砌", raw_features="矩形现浇渠道 C25 断面0.8×0.6 F200", raw_unit="m³", quantity=980.0, unit_price_yuan=654.00, total_price_yuan=640920.0, breakdown=_make_bd("SL-GQ-105", "m³", 654.00, "商品混凝土", "m³", 1.02, 395.0, 1.05, 116.0)),
                RawBoqItem(seq_no="1.4", section_path=["灌区输水渠道衬砌"], raw_name="预制U型混凝土槽", raw_features="预制U型槽 C25 D600 壁厚6cm 斗渠", raw_unit="m", quantity=3200.0, unit_price_yuan=124.80, total_price_yuan=399360.0, breakdown=_make_bd("SL-GQ-112", "m", 124.80, "预制U型槽 D600", "m", 1.01, 78.0, 0.22, 116.0)),
                RawBoqItem(seq_no="1.5", section_path=["灌区输水渠道衬砌"], raw_name="预制U型混凝土槽", raw_features="预制U型槽 C25 D800 壁厚7cm 支渠", raw_unit="m", quantity=1850.0, unit_price_yuan=176.40, total_price_yuan=326340.0, breakdown=_make_bd("SL-GQ-114", "m", 176.40, "预制U型槽 D800", "m", 1.01, 114.0, 0.28, 116.0)),
                RawBoqItem(seq_no="1.6", section_path=["灌区输水渠道衬砌"], raw_name="渠道防冻聚苯乙烯保温板", raw_features="现浇混凝土渠道 C25 厚5cm 聚苯保温板衬垫", raw_unit="m²", quantity=12000.0, unit_price_yuan=28.60, total_price_yuan=343200.0, breakdown=_make_bd("SL-GQ-118", "m²", 28.60, "XPS挤塑保温板 5cm", "m²", 1.03, 19.5, 0.04, 116.0)),
                RawBoqItem(seq_no="1.7", section_path=["灌区输水渠道衬砌"], raw_name="渠道一布一膜复合土工膜", raw_features="矩形现浇渠道 厚8cm 复合土工膜防渗衬砌", raw_unit="m²", quantity=18500.0, unit_price_yuan=21.40, total_price_yuan=395900.0, breakdown=_make_bd("SL-GQ-122", "m²", 21.40, "一布一膜复合土工膜", "m²", 1.05, 13.8, 0.03, 116.0)),
                RawBoqItem(seq_no="1.8", section_path=["灌区输水渠道衬砌"], raw_name="渠道伸缩缝闭孔泡沫板嵌缝", raw_features="现浇混凝土渠道 断面1.0×0.8 聚硫密封胶", raw_unit="m", quantity=2400.0, unit_price_yuan=36.50, total_price_yuan=87600.0, breakdown=_make_bd("SL-GQ-126", "m", 36.50, "聚硫密封胶", "kg", 0.55, 42.0, 0.06, 116.0)),
                RawBoqItem(seq_no="2.1", section_path=["渠系建筑物"], raw_name="现浇混凝土渡槽", raw_features="现浇混凝土渡槽 C30 跨度12m 槽身及排架", raw_unit="m³", quantity=360.0, unit_price_yuan=1460.00, total_price_yuan=525600.0, breakdown=_make_bd("SL-GQ-201", "m³", 1460.00, "商品混凝土", "m³", 1.02, 425.0, 3.8, 116.0)),
                RawBoqItem(seq_no="2.2", section_path=["渠系建筑物"], raw_name="预制U型渡槽", raw_features="预制U型渡槽 C30 跨度8m 安装就位", raw_unit="m³", quantity=220.0, unit_price_yuan=1285.00, total_price_yuan=282700.0, breakdown=_make_bd("SL-GQ-204", "m³", 1285.00, "预制U型渡槽 C30", "m³", 1.01, 780.0, 2.1, 116.0)),
                RawBoqItem(seq_no="2.3", section_path=["渠系建筑物"], raw_name="钢筋混凝土倒虹吸", raw_features="钢筋混凝土倒虹吸 C30 DN1200 暗涵穿越", raw_unit="m³", quantity=410.0, unit_price_yuan=1180.00, total_price_yuan=483800.0, breakdown=_make_bd("SL-GQ-211", "m³", 1180.00, "商品混凝土", "m³", 1.02, 425.0, 2.6, 116.0)),
                RawBoqItem(seq_no="2.4", section_path=["渠系建筑物"], raw_name="跨渠农桥", raw_features="跨渠农桥 C30 跨度6m 装配式板桥", raw_unit="座", quantity=14.0, unit_price_yuan=68500.00, total_price_yuan=959000.0, breakdown=_make_bd("SL-GQ-218", "座", 68500.00, "商品混凝土", "m³", 48.0, 425.0, 140.0, 116.0)),
                RawBoqItem(seq_no="2.5", section_path=["渠系建筑物"], raw_name="混凝土跌水建筑物", raw_features="跌水 C25 断面2.0×1.5", raw_unit="座", quantity=18.0, unit_price_yuan=18400.00, total_price_yuan=331200.0, breakdown=_make_bd("SL-GQ-224", "座", 18400.00, "商品混凝土", "m³", 16.5, 395.0, 38.0, 116.0)),
                RawBoqItem(seq_no="2.6", section_path=["渠系建筑物"], raw_name="矩形量水堰", raw_features="量水堰 C25 断面1.2×0.8 含水尺", raw_unit="座", quantity=22.0, unit_price_yuan=7850.00, total_price_yuan=172700.0, breakdown=_make_bd("SL-GQ-229", "座", 7850.00, "商品混凝土", "m³", 6.2, 395.0, 18.0, 116.0)),
                RawBoqItem(seq_no="2.7", section_path=["渠系建筑物"], raw_name="灌区钢筋制安", raw_features="钢筋制安 HRB400 渠系建筑物配筋", raw_unit="t", quantity=135.0, unit_price_yuan=5890.00, total_price_yuan=795150.0, breakdown=_make_bd("SL-GQ-235", "t", 5890.00, "钢筋", "t", 1.02, 3720.0, 8.2, 116.0)),
                RawBoqItem(seq_no="3.1", section_path=["闸门启闭与节水灌溉"], raw_name="平面铸铁闸门安装", raw_features="平面铸铁闸门 0.8×0.8 手动控制", raw_unit="扇", quantity=24.0, unit_price_yuan=6420.00, total_price_yuan=154080.0, breakdown=_make_bd("SL-GQ-301", "扇", 6420.00, "铸铁闸门 0.8×0.8", "扇", 1.0, 4850.0, 4.5, 116.0)),
                RawBoqItem(seq_no="3.2", section_path=["闸门启闭与节水灌溉"], raw_name="平面钢闸门安装", raw_features="平面钢闸门 2.0×1.5 手电两用控制", raw_unit="扇", quantity=8.0, unit_price_yuan=26800.00, total_price_yuan=214400.0, breakdown=_make_bd("SL-GQ-306", "扇", 26800.00, "钢闸门 Q235", "t", 1.85, 9600.0, 18.0, 116.0)),
                RawBoqItem(seq_no="3.3", section_path=["闸门启闭与节水灌溉"], raw_name="螺杆启闭机安装", raw_features="螺杆启闭机 50kN 手电两用", raw_unit="台", quantity=24.0, unit_price_yuan=5920.00, total_price_yuan=142080.0, breakdown=_make_bd("SL-GQ-311", "台", 5920.00, "螺杆启闭机 50kN", "台", 1.0, 4680.0, 3.6, 116.0)),
                RawBoqItem(seq_no="3.4", section_path=["闸门启闭与节水灌溉"], raw_name="高效节水滴灌管带敷设", raw_features="滴灌管带敷设 Φ16 滴头间距300mm 遥测遥控", raw_unit="hm²", quantity=120.0, unit_price_yuan=11400.00, total_price_yuan=1368000.0, breakdown=_make_bd("SL-GQ-320", "hm²", 11400.00, "迷宫式滴灌带 Φ16", "m", 12500.0, 0.58, 18.0, 116.0)),
                RawBoqItem(seq_no="3.5", section_path=["闸门启闭与节水灌溉"], raw_name="砂石离心首部过滤系统", raw_features="砂石离心首部过滤系统 处理能力80m³/h 遥测遥控", raw_unit="套", quantity=6.0, unit_price_yuan=46800.00, total_price_yuan=280800.0, breakdown=_make_bd("SL-GQ-328", "套", 46800.00, "砂石离心过滤首部 80m³/h", "套", 1.0, 38500.0, 22.0, 116.0)),
            ],
        ),
        # 3. 04_coal_mine_water: 20 项
        (
            ProjectContext(
                project_id="ref_coal_water_04",
                project_name="榆神矿区煤矿疏干水综合利用与深度处理工程",
                domain=EngineeringDomain.COAL_MINE_WATER,
                province="陕西省",
                city="榆林市",
                stage=CostStage.PRELIMINARY_DESIGN,
                price_base_month="2026-09",
                quota_standards=["《煤炭建设机电安装工程消耗量定额》", "GB50500-2024 建设工程工程量清单计价标准"],
                labor_rates={"综合工日": 146.0},
                fee_rules=[FeeRateRule(engineering_category="矿井机电安装与水处理", temp_facility_rate=2.5, indirect_fee_rate=9.2, profit_rate=5.0, tax_rate=9.0)],
                base_materials=[
                    MaterialPriceRecord(material_name="钢筋", unit="t", base_price=3500.0, budget_price_excl_tax=3820.0),
                    MaterialPriceRecord(material_name="商品混凝土", unit="m³", base_price=380.0, budget_price_excl_tax=430.0),
                    MaterialPriceRecord(material_name="柴油", unit="kg", base_price=6.5, budget_price_excl_tax=7.4),
                ],
            ),
            [
                RawBoqItem(seq_no="1.1", section_path=["井上下高压输水管网"], raw_name="矿用无缝钢管敷设", raw_features="无缝钢管 管径:DN150 工作压力:4.0MPa 井下", raw_unit="m", quantity=1200.0, unit_price_yuan=426.00, total_price_yuan=511200.0, breakdown=_make_bd("MT-JS-101", "m", 426.00, "无缝钢管 DN150", "m", 1.02, 265.0, 0.58, 146.0)),
                RawBoqItem(seq_no="1.2", section_path=["井上下高压输水管网"], raw_name="矿用无缝钢管敷设", raw_features="无缝钢管 管径:DN200 工作压力:6.4MPa 竖井", raw_unit="m", quantity=680.0, unit_price_yuan=768.00, total_price_yuan=522240.0, breakdown=_make_bd("MT-JS-105", "m", 768.00, "高压无缝钢管 DN200", "m", 1.02, 485.0, 1.05, 146.0)),
                RawBoqItem(seq_no="1.3", section_path=["井上下高压输水管网"], raw_name="矿用无缝钢管敷设", raw_features="无缝钢管 管径:DN250 工作压力:4.0MPa 巷道", raw_unit="m", quantity=1450.0, unit_price_yuan=892.00, total_price_yuan=1293400.0, breakdown=_make_bd("MT-JS-108", "m", 892.00, "无缝钢管 DN250", "m", 1.02, 580.0, 1.15, 146.0)),
                RawBoqItem(seq_no="1.4", section_path=["井上下高压输水管网"], raw_name="矿用涂塑复合钢管安装", raw_features="矿用涂塑复合钢管 管径:DN200 工作压力:2.5MPa 井下", raw_unit="m", quantity=2100.0, unit_price_yuan=518.00, total_price_yuan=1087800.0, breakdown=_make_bd("MT-JS-114", "m", 518.00, "矿用涂塑复合钢管 DN200", "m", 1.02, 356.0, 0.62, 146.0)),
                RawBoqItem(seq_no="1.5", section_path=["井上下高压输水管网"], raw_name="矿用涂塑复合钢管安装", raw_features="矿用涂塑复合钢管 管径:DN300 工作压力:2.5MPa 巷道", raw_unit="m", quantity=950.0, unit_price_yuan=845.00, total_price_yuan=802750.0, breakdown=_make_bd("MT-JS-118", "m", 845.00, "矿用涂塑复合钢管 DN300", "m", 1.02, 595.0, 0.92, 146.0)),
                RawBoqItem(seq_no="1.6", section_path=["井上下高压输水管网"], raw_name="3PE防腐螺旋钢管安装", raw_features="3PE防腐螺旋钢管 管径:DN400 工作压力:1.6MPa 地面 直埋", raw_unit="m", quantity=1800.0, unit_price_yuan=792.00, total_price_yuan=1425600.0, breakdown=_make_bd("MT-JS-124", "m", 792.00, "3PE防腐螺旋钢管 DN400", "m", 1.02, 560.0, 0.85, 146.0)),
                RawBoqItem(seq_no="1.7", section_path=["井上下高压输水管网"], raw_name="3PE防腐螺旋钢管安装", raw_features="3PE防腐螺旋钢管 管径:DN500 工作压力:1.6MPa 地面 架空", raw_unit="m", quantity=740.0, unit_price_yuan=1085.00, total_price_yuan=802900.0, breakdown=_make_bd("MT-JS-129", "m", 1085.00, "3PE防腐螺旋钢管 DN500", "m", 1.02, 765.0, 1.12, 146.0)),
                RawBoqItem(seq_no="2.1", section_path=["矿用防爆排水泵站"], raw_name="矿用耐磨多级离心泵", raw_features="矿用耐磨多级离心泵 扬程520m 560kW 井下中央水泵房", raw_unit="台", quantity=4.0, unit_price_yuan=386000.00, total_price_yuan=1544000.0, breakdown=_make_bd("MT-JD-201", "台", 386000.00, "矿用耐磨多级离心泵 560kW", "台", 1.0, 328000.0, 96.0, 146.0)),
                RawBoqItem(seq_no="2.2", section_path=["矿用防爆排水泵站"], raw_name="矿用耐磨多级离心泵", raw_features="矿用耐磨多级离心泵 扬程320m 280kW 井下采区泵房", raw_unit="台", quantity=3.0, unit_price_yuan=218000.00, total_price_yuan=654000.0, breakdown=_make_bd("MT-JD-204", "台", 218000.00, "矿用耐磨多级离心泵 280kW", "台", 1.0, 182000.0, 62.0, 146.0)),
                RawBoqItem(seq_no="2.3", section_path=["矿用防爆排水泵站"], raw_name="矿用隔爆型潜水电泵", raw_features="矿用隔爆潜水泵 扬程180m 132kW 抢险排水", raw_unit="台", quantity=4.0, unit_price_yuan=116000.00, total_price_yuan=464000.0, breakdown=_make_bd("MT-JD-209", "台", 116000.00, "矿用隔爆潜水泵 132kW", "台", 1.0, 94500.0, 36.0, 146.0)),
                RawBoqItem(seq_no="2.4", section_path=["矿用防爆排水泵站"], raw_name="矿用高压闸阀安装", raw_features="矿用高压闸阀 DN200 6.4MPa", raw_unit="个", quantity=12.0, unit_price_yuan=14200.00, total_price_yuan=170400.0, breakdown=_make_bd("MT-JD-215", "个", 14200.00, "高压闸阀 DN200", "个", 1.0, 11800.0, 5.2, 146.0)),
                RawBoqItem(seq_no="2.5", section_path=["矿用防爆排水泵站"], raw_name="缓闭止回阀安装", raw_features="缓闭止回阀 DN250 4.0MPa", raw_unit="个", quantity=8.0, unit_price_yuan=18600.00, total_price_yuan=148800.0, breakdown=_make_bd("MT-JD-218", "个", 18600.00, "缓闭止回阀 DN250", "个", 1.0, 15200.0, 6.4, 146.0)),
                RawBoqItem(seq_no="2.6", section_path=["矿用防爆排水泵站"], raw_name="水锤消除器安装", raw_features="水锤消除器 DN200 6.4MPa", raw_unit="台", quantity=4.0, unit_price_yuan=22400.00, total_price_yuan=89600.0, breakdown=_make_bd("MT-JD-222", "台", 22400.00, "水锤消除器 DN200", "台", 1.0, 18500.0, 7.5, 146.0)),
                RawBoqItem(seq_no="3.1", section_path=["疏干水深度处理车间"], raw_name="高密度澄清沉淀池", raw_features="高密度澄清沉淀池 处理量800m³/h 钢筋混凝土池体", raw_unit="座", quantity=2.0, unit_price_yuan=1420000.00, total_price_yuan=2840000.0, breakdown=_make_bd("MT-CL-301", "座", 1420000.00, "商品混凝土", "m³", 920.0, 430.0, 2150.0, 146.0)),
                RawBoqItem(seq_no="3.2", section_path=["疏干水深度处理车间"], raw_name="V型滤池土建及滤料安装", raw_features="V型滤池 处理量600m³/h 全自动化反冲洗", raw_unit="座", quantity=2.0, unit_price_yuan=980000.00, total_price_yuan=1960000.0, breakdown=_make_bd("MT-CL-305", "座", 980000.00, "石英砂滤料与滤板", "t", 210.0, 1850.0, 1450.0, 146.0)),
                RawBoqItem(seq_no="3.3", section_path=["疏干水深度处理车间"], raw_name="超滤膜组件及装置", raw_features="超滤UF膜系统 处理量500m³/h 全自动化", raw_unit="套", quantity=2.0, unit_price_yuan=1680000.00, total_price_yuan=3360000.0, breakdown=_make_bd("MT-CL-312", "套", 1680000.00, "超滤UF膜组件成套", "套", 1.0, 1420000.0, 280.0, 146.0)),
                RawBoqItem(seq_no="3.4", section_path=["疏干水深度处理车间"], raw_name="反渗透RO膜脱盐系统", raw_features="反渗透RO膜系统 处理量300m³/h 回收率75%", raw_unit="套", quantity=2.0, unit_price_yuan=2350000.00, total_price_yuan=4700000.0, breakdown=_make_bd("MT-CL-318", "套", 2350000.00, "反渗透RO膜系统成套", "套", 1.0, 1980000.0, 390.0, 146.0)),
                RawBoqItem(seq_no="3.5", section_path=["疏干水深度处理车间"], raw_name="隔膜板框压滤机", raw_features="隔膜板框压滤机 过滤面积200m² 污泥脱水", raw_unit="台", quantity=2.0, unit_price_yuan=345000.00, total_price_yuan=690000.0, breakdown=_make_bd("MT-CL-324", "台", 345000.00, "隔膜板框压滤机 200m²", "台", 1.0, 292000.0, 56.0, 146.0)),
                RawBoqItem(seq_no="3.6", section_path=["疏干水深度处理车间"], raw_name="二氧化氯加药消毒装置", raw_features="二氧化氯加药消毒 全自动化", raw_unit="套", quantity=2.0, unit_price_yuan=96000.00, total_price_yuan=192000.0, breakdown=_make_bd("MT-CL-329", "套", 96000.00, "二氧化氯发生器", "套", 1.0, 78500.0, 18.0, 146.0)),
                RawBoqItem(seq_no="3.7", section_path=["井下水仓清淤"], raw_name="井下水仓机械清淤", raw_features="井下水仓机械清淤 井下防爆 清淤转运", raw_unit="m³", quantity=1800.0, unit_price_yuan=246.00, total_price_yuan=442800.0, breakdown=_make_bd("MT-CL-336", "m³", 246.00, "防爆清淤耗材", "元", 1.0, 48.0, 0.85, 146.0)),
            ],
        ),
        # 4. 06_marine_ecology: 20 项
        (
            ProjectContext(
                project_id="ref_marine_06",
                project_name="湛江湾海岸带蓝色海湾整治与红树林生态修复工程",
                domain=EngineeringDomain.MARINE_ECOLOGY,
                province="广东省",
                city="湛江市",
                stage=CostStage.PRELIMINARY_DESIGN,
                price_base_month="2026-07",
                quota_standards=["《海岸带生态减灾修复技术导则》", "水运工程定额标准"],
                labor_rates={"综合工日": 142.0},
                fee_rules=[FeeRateRule(engineering_category="海洋生态修复工程", temp_facility_rate=2.8, indirect_fee_rate=9.0, profit_rate=5.0, tax_rate=9.0)],
                base_materials=[
                    MaterialPriceRecord(material_name="商品混凝土", unit="m³", base_price=390.0, budget_price_excl_tax=460.0),
                    MaterialPriceRecord(material_name="钢筋", unit="t", base_price=3500.0, budget_price_excl_tax=3920.0),
                    MaterialPriceRecord(material_name="柴油", unit="kg", base_price=6.6, budget_price_excl_tax=7.6),
                ],
            ),
            [
                RawBoqItem(seq_no="1.1", section_path=["红树林营造工程"], raw_name="红树林营造-秋茄", raw_features="秋茄 营养杯苗 中潮带 造林密度1m×1m 管护3年", raw_unit="hm²", quantity=18.5, unit_price_yuan=148500.00, total_price_yuan=2747250.0, breakdown=_make_bd("HY-ST-101", "hm²", 148500.00, "秋茄营养杯苗", "株", 10500.0, 7.2, 320.0, 142.0)),
                RawBoqItem(seq_no="1.2", section_path=["红树林营造工程"], raw_name="红树林营造-木榄", raw_features="木榄 营养杯苗 中潮带 造林密度1.5m×1.5m 管护3年", raw_unit="hm²", quantity=12.0, unit_price_yuan=118200.00, total_price_yuan=1418400.0, breakdown=_make_bd("HY-ST-103", "hm²", 118200.00, "木榄营养杯苗", "株", 4800.0, 12.5, 250.0, 142.0)),
                RawBoqItem(seq_no="1.3", section_path=["红树林营造工程"], raw_name="红树林营造-白骨壤", raw_features="白骨壤 低潮带 造林密度1m×1m 管护3年", raw_unit="hm²", quantity=14.0, unit_price_yuan=162000.00, total_price_yuan=2268000.0, breakdown=_make_bd("HY-ST-105", "hm²", 162000.00, "白骨壤抗浪大苗", "株", 10500.0, 8.2, 340.0, 142.0)),
                RawBoqItem(seq_no="1.4", section_path=["红树林营造工程"], raw_name="桐花树潮沟补植", raw_features="桐花树 高潮带 造林密度1m×1.5m 管护2年", raw_unit="hm²", quantity=8.5, unit_price_yuan=109600.00, total_price_yuan=931600.0, breakdown=_make_bd("HY-ST-108", "hm²", 109600.00, "桐花树苗木", "株", 7000.0, 7.6, 220.0, 142.0)),
                RawBoqItem(seq_no="1.5", section_path=["盐沼与入侵种整治"], raw_name="互花米草物理刈割除治", raw_features="互花米草物理刈割 中潮带 旋耕深翻 监测2年", raw_unit="hm²", quantity=35.0, unit_price_yuan=48600.00, total_price_yuan=1701000.0, breakdown=_make_bd("HY-ST-115", "hm²", 48600.00, "机械刈割耗材", "元", 1.0, 8600.0, 95.0, 142.0, diesel_kg=450.0)),
                RawBoqItem(seq_no="1.6", section_path=["盐沼与入侵种整治"], raw_name="盐地碱蓬盐沼恢复", raw_features="盐地碱蓬 宜林滩涂 高潮带 撒播 监测2年", raw_unit="hm²", quantity=22.0, unit_price_yuan=32400.00, total_price_yuan=712800.0, breakdown=_make_bd("HY-ST-119", "hm²", 32400.00, "碱蓬草籽与基质", "kg", 180.0, 86.0, 68.0, 142.0)),
                RawBoqItem(seq_no="1.7", section_path=["红树林营造工程"], raw_name="防浪竹木桩消浪围栏", raw_features="红树林消浪桩 宜林滩涂 中潮带 管护3年", raw_unit="m", quantity=3800.0, unit_price_yuan=168.00, total_price_yuan=638400.0, breakdown=_make_bd("HY-ST-124", "m", 168.00, "防浪杉木桩", "m", 4.0, 22.0, 0.36, 142.0)),
                RawBoqItem(seq_no="2.1", section_path=["人工鱼礁与海草床"], raw_name="钢筋混凝土箱型人工鱼礁", raw_features="钢筋混凝土箱型人工鱼礁 30空m³ 水深12m 乘潮施工", raw_unit="个", quantity=120.0, unit_price_yuan=18400.00, total_price_yuan=2208000.0, breakdown=_make_bd("HY-YJ-201", "个", 18400.00, "商品混凝土", "m³", 9.6, 460.0, 26.0, 142.0)),
                RawBoqItem(seq_no="2.2", section_path=["人工鱼礁与海草床"], raw_name="钢筋混凝土箱型人工鱼礁", raw_features="钢筋混凝土箱型人工鱼礁 64空m³ 水深15m 海洋投放", raw_unit="个", quantity=80.0, unit_price_yuan=36800.00, total_price_yuan=2944000.0, breakdown=_make_bd("HY-YJ-204", "个", 36800.00, "商品混凝土", "m³", 18.5, 460.0, 48.0, 142.0)),
                RawBoqItem(seq_no="2.3", section_path=["人工鱼礁与海草床"], raw_name="牡蛎礁生态构件制作投放", raw_features="牡蛎礁生态构件 2.5空m³ 低潮带 乘潮施工", raw_unit="个", quantity=450.0, unit_price_yuan=2680.00, total_price_yuan=1206000.0, breakdown=_make_bd("HY-YJ-209", "个", 2680.00, "牡蛎壳多孔混凝土", "m³", 1.4, 920.0, 4.5, 142.0)),
                RawBoqItem(seq_no="2.4", section_path=["人工鱼礁与海草床"], raw_name="海草床人工移植修复", raw_features="海草床人工移植 泰来草 密度25株/m² 水深1.5m", raw_unit="hm²", quantity=6.5, unit_price_yuan=286000.00, total_price_yuan=1859000.0, breakdown=_make_bd("HY-YJ-215", "hm²", 286000.00, "海草植株及固定锚", "株", 250000.0, 0.52, 620.0, 142.0)),
                RawBoqItem(seq_no="2.5", section_path=["人工鱼礁与海草床"], raw_name="透水消浪构筑物安装", raw_features="透水消浪构筑物 2m×2m×1.5m 水深3m", raw_unit="座", quantity=90.0, unit_price_yuan=14200.00, total_price_yuan=1278000.0, breakdown=_make_bd("HY-YJ-220", "座", 14200.00, "商品混凝土", "m³", 4.2, 460.0, 18.0, 142.0)),
                RawBoqItem(seq_no="3.1", section_path=["生态海堤与沙滩养护"], raw_name="海堤消浪护面块体安装", raw_features="扭王字块预制安装 6t C35 水上安装", raw_unit="m³", quantity=2800.0, unit_price_yuan=1120.00, total_price_yuan=3136000.0, breakdown=_make_bd("HY-HD-301", "m³", 1120.00, "商品混凝土", "m³", 1.02, 480.0, 1.85, 142.0)),
                RawBoqItem(seq_no="3.2", section_path=["生态海堤与沙滩养护"], raw_name="四脚空心方块护面安装", raw_features="四脚空心方块安装 4t C30 水上安装", raw_unit="m³", quantity=1950.0, unit_price_yuan=985.00, total_price_yuan=1920750.0, breakdown=_make_bd("HY-HD-304", "m³", 985.00, "商品混凝土", "m³", 1.02, 460.0, 1.65, 142.0)),
                RawBoqItem(seq_no="3.3", section_path=["生态海堤与沙滩养护"], raw_name="抛石防冲齿槽及潜堤", raw_features="抛石防冲齿槽 块石200~400kg 水下抛填", raw_unit="m³", quantity=6500.0, unit_price_yuan=342.00, total_price_yuan=2223000.0, breakdown=_make_bd("HY-HD-308", "m³", 342.00, "海堤块石", "m³", 1.15, 168.0, 0.28, 142.0, diesel_kg=2.6)),
                RawBoqItem(seq_no="3.4", section_path=["生态海堤与沙滩养护"], raw_name="植生型生态挡墙护坡", raw_features="植生型生态挡墙 C25 陆上施工", raw_unit="m³", quantity=1400.0, unit_price_yuan=768.00, total_price_yuan=1075200.0, breakdown=_make_bd("HY-HD-312", "m³", 768.00, "多孔植生混凝土 C25", "m³", 1.02, 485.0, 1.35, 142.0)),
                RawBoqItem(seq_no="3.5", section_path=["生态海堤与沙滩养护"], raw_name="中粗砂吹填养滩", raw_features="中粗砂吹填养滩 d50=0.45mm 陆上施工", raw_unit="m³", quantity=45000.0, unit_price_yuan=68.50, total_price_yuan=3082500.0, breakdown=_make_bd("HY-HD-318", "m³", 68.50, "海砂中粗砂吹填", "m³", 1.12, 34.0, 0.04, 142.0, diesel_kg=1.4)),
                RawBoqItem(seq_no="3.6", section_path=["生态海堤与沙滩养护"], raw_name="滩涂潮沟开挖疏浚", raw_features="潮沟开挖疏浚 乘潮施工 宽6m深1.5m", raw_unit="m³", quantity=18000.0, unit_price_yuan=36.80, total_price_yuan=662400.0, breakdown=_make_bd("HY-HD-322", "m³", 36.80, "疏浚机械辅材", "元", 1.0, 6.0, 0.05, 142.0, diesel_kg=1.8)),
                RawBoqItem(seq_no="3.7", section_path=["生态海堤与沙滩养护"], raw_name="海堤干砌块石护坡", raw_features="干砌块石护坡 厚40cm 陆上施工", raw_unit="m³", quantity=2200.0, unit_price_yuan=298.00, total_price_yuan=655600.0, breakdown=_make_bd("HY-HD-326", "m³", 298.00, "花岗岩块石", "m³", 1.12, 142.0, 0.65, 142.0)),
                RawBoqItem(seq_no="3.8", section_path=["生态监测设施"], raw_name="海洋生态在线监测浮标", raw_features="水质潮汐监测浮标 水深10m 遥测遥控", raw_unit="座", quantity=3.0, unit_price_yuan=264000.00, total_price_yuan=792000.0, breakdown=_make_bd("HY-JC-401", "座", 264000.00, "海洋生态多参数监测浮标", "座", 1.0, 224000.0, 35.0, 142.0)),
            ],
        ),
    ]

    projects: list[CanonicalProject] = []
    for ctx, raw_items in specs:
        std_items = [
            standardizer.standardize(
                item=r,
                domain=ctx.domain.value,
                project_id=ctx.project_id,
                idx=idx + 1,
            )
            for idx, r in enumerate(raw_items)
        ]
        projects.append(
            CanonicalProject(
                context=ctx,
                items=std_items,
                raw_items_count=len(std_items),
            )
        )
    return projects
