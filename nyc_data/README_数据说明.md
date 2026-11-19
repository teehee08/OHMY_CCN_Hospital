# Brooklyn 医院与 SNF 数据包（Brooklyn_Hospital_SNF_Data.xlsx）

Brooklyn（Kings County）13 家医院、40 家 SNF 的公开数据，2026-11-18 拉取。表中蓝色字为原始数据，黑色字为公式。

## 工作表

| 工作表 | 内容 |
|---|---|
| README | 每项数据对应的工作表、来源链接和注意事项 |
| Hospitals | 13 家医院：经纬度；2026-11-18 在用急症床位、已占用、空床、占用率；8/1–11/18 平均占用率；2024 全年出院人数、每天出院/入院、去 SNF 人数及占比 |
| Hospital_Daily | 每家医院 2026-08-01 至 11-18 每天的在用床位、已占用、空床，以及已占用床位的日变化（≈ 当天入院 − 出院） |
| SNFs | 40 家 SNF：认证床位、日均住户；空床（报告值和修正值）；呼吸机床、透析床；RN / LPN / CNA 每日工时及折算 8 小时班次；实际与按病情预期的每人每天护理工时；Medicare 入住次数、计费天数、平均住院天数；星级评分 |
| Distance_km | 13 家医院 × 40 家 SNF 的直线距离（公里） |
| LOS_Before_SNF | 2024 年去 SNF 的病人在医院住了几天的分布（中位数 9 天，平均 12.2 天） |
| SNF_Patient_Mix | 按医院统计去 SNF 病人的年龄组、性别、病情严重度、付款方、入院类型 |

## 主要数值

| 指标 | 数值 |
|---|---|
| 医院在用急症床位 / 空床（2026-11-18） | 3,986 / 623，占用率 84% |
| 医院每天出院 | 约 547 人 |
| 其中去 SNF | 约 55 人/天（10.1%） |
| 去 SNF 前住院天数 | 中位数 9 天，平均 12.2 天 |
| SNF 空床（报告 / 修正后） | 881 / 549 张 |
| SNF 每日护理工时（合计） | RN 约 6,334 h、LPN 约 5,638 h、CNA 约 22,442 h |
| 每位住户每天护理工时（实际 / 按病情预期） | 3.47 h / 4.33 h |
| SNF 平均住院天数（Medicare） | 约 41 天 |

## 数据来源

| 数据 | 数据集 |
|---|---|
| 出院人数、去 SNF 人数、住院天数、病人构成 | [SPARCS Hospital Inpatient Discharges 2024](https://health.data.ny.gov/d/sf4k-39ay) |
| 医院床位与占用（每日） | [NYS Statewide Hospital Bed Capacity](https://health.data.ny.gov/d/2dbc-sqe7) |
| SNF 空床（每周） | [Nursing Home Weekly Bed Census: Last Submission](https://health.data.ny.gov/d/izta-vnpq) |
| 医院、SNF 坐标 | [Health Facility General Information](https://health.data.ny.gov/d/vn5v-hh5r) |
| SNF 床位、护理工时、星级 | [CMS Nursing Home Provider Information](https://data.cms.gov/provider-data/dataset/4pq5-n9py) |
| SNF 每日护理人员（2026 Q2） | [CMS Payroll Based Journal Daily Nurse Staffing](https://data.cms.gov/data-api/v1/dataset/7e0d53ba-8f02-4c66-98a5-14a1c997c50d/data?filter[STATE]=NY&filter[COUNTY_NAME]=Kings) |
| SNF 住院天数 | [CMS Medicare Post-Acute Care Utilization – SNF 2024](https://data.cms.gov/data-api/v1/dataset/e912267c-e686-441e-b4b4-880d6ae4138f/data?filter[STATE]=NY) |

## 注意事项

1. SPARCS 公开数据没有入院、出院日期，每天的出院和去 SNF 人数为 2024 全年 ÷ 366；入院 ≈ 出院为稳态假设，真实的每日变化见 Hospital_Daily。
2. SNF 床位普查为 2026-10-07 那一周的数据。部分 SNF 报告的空床与在住人数矛盾（如 Sheepshead 报 193/200 空床，而实际约 187 位住户），"修正后空床" = MIN(报告空床, 认证床位 − 最低在住人数)。
3. SNF 护理工时为 2026-04-01 至 06-30（91 天）的日均值；班次 = 工时 ÷ 8。
4. 去 SNF 前的住院天数为 13 家医院合计，未按医院区分。
5. 距离为直线距离，实际道路距离约长 30%–50%。
6. Wyckoff Heights 在纽约州数据中缺坐标，按其地址（374 Stockholm St）补充。
7. 未包含 Calvary Hospital（安宁疗护，每年仅 7 人去 SNF）和 NYU Cobble Hill（门诊中心，无床位）。
8. 医院床位（11 月）、SNF 空床（10 月）、护理工时（4–6 月）、出院（2024 全年）的时间点不一致。
