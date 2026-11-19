# Brooklyn 真实数据场景：数据来源与假设

数据拉取日期：2026-11-18。范围：Brooklyn（Kings County）13 家医院、40 家 SNF（场景中 37 家有空床）。

## 文件

| 文件 | 内容 |
|---|---|
| `brooklyn_real_scenario.json` | 可直接在 demo 里 Import 的场景（紧急度 + 距离，无人员约束） |
| `brooklyn_real_scenario_with_staffing.json` | 同一场景加上真实人员数据（目前运行超过 10 分钟，见"局限"） |
| `brooklyn_real_meta.json` | 每家医院、SNF 的原始数值（占用率、床位、人员、住院时长等） |
| `cms_snf_kings.psv` | CMS 数据汇总（40 家 SNF） |
| `build_brooklyn.py` | 生成脚本：`python build_brooklyn.py --scale 3 --days 7 --referrals 5 --seed 11` |
| `../nyc_brooklyn_nydoh_raw.json` | 纽约州卫生厅原始查询结果 |

## 每个需求对应的数据

| 需求 | 用的数据 | Brooklyn 的数值 | 真实 / 假设 |
|---|---|---|---|
| 医院床位、初始占用 | NYS HCDA 医院床位容量（2dbc-sqe7，每日更新） | 13 家医院 2026-11-18 的在用急症床位占用率 74.5%–92.3% | 真实 |
| 医院每天出院、去 SNF 的人数 | SPARCS 2024 住院出院（sf4k-39ay） | 全年出院约 23.4 万人次，其中去 SNF 20,257 人，约 55 人/天 | 真实（年度平均，日内和工作日波动未知） |
| 出院前住院多久 | SPARCS 2024 去 SNF 病人的住院天数 | 中位数 9 天，平均 12.2 天 | 真实 |
| 病人特征 | SPARCS 按医院的年龄、性别、病情严重度、付款方 | 70 岁以上 71%，Medicare 82%、Medicaid 13%；严重度 Extreme 20% | 真实分布，单个病人为抽样 |
| SNF 位置、医院位置 | NYS 卫生设施信息（vn5v-hh5r）、CMS Provider Info | 经纬度 → 距离（km） | 真实（Wyckoff 坐标由地址补） |
| SNF 可用床位（初始值） | NYS 养老院每周床位普查（izta-vnpq，2026-10-07 那一周） | 报告空床 881 张；按"认证床位 − 实际住户"修正后 549 张 | 真实，有修正（如 Sheepshead 报 193 张空床，与在住人数矛盾） |
| 呼吸机、透析床 | 同上，床位类型 VENTBEDSAV / DIALYSIS | 6 家有呼吸机床，9 家有透析 | 真实 |
| SNF 人力（每天多少护士） | CMS PBJ 每日护理人员（2026 Q2，91 天） | 每家每天的 RN / LPN / CNA 工时，平均每位住户 3.47 小时/天 | 真实 |
| 每个病人需要多少护理时间 | CMS 病例组合预期工时（case-mix HPRD） | 平均 4.33 小时/天，RN 18% / LPN 16% / CNA 65%；按严重度 ×0.85–1.25 | 平均值真实，按严重度分配为假设 |
| 在 SNF 住多久 | CMS Medicare PAC SNF 利用 2024 | 平均 41.7 天（Medicare 计费天数 ÷ 入住次数） | 真实均值，个人为对数正态抽样（7–100 天） |
| 距离纳入优先级 | 紧急度目标中的距离项 | λ = 6 小时，30 km 封顶 | 权重为假设 |

## 主要假设（向导师说明时要讲）

1. **比例缩放 1:3、一周：** 病人数 = 每天去 SNF 人数 × 7 ÷ 3 ≈ 129 人；床位 = 修正后空床 ÷ 3 ≈ 181 张。模型上限是 500 张床、单次运行时间也有限。
2. **转介名单：** 每个病人发给离家最近的 3–7 家有对应护理能力的 SNF。公开数据里没有这项，**结果对它非常敏感**（见下）。
3. **病人住址：** SPARCS 只有 3 位邮编，住址用"所在医院附近 ±2.5 km"近似。
4. **保险：** 40 家 SNF 都同时有 Medicare 和 Medicaid 认证，假设都接受所有付款方；商业保险网络未知。
5. **房间性别：** 没有数据，假设床位不限性别。
6. **病人在一周内均匀出现**；不含其他区医院转来的竞争需求，也不含周内 SNF 现有住户出院腾出的床。
7. **临床风险：** SPARCS 严重度 Extreme → high、Major → medium，其余 → low。没有活动分和授权到期时间。

## 运行结果（无人员约束，约 6 秒）

| 指标 | Baseline | CCN |
|---|---|---|
| 总等待 | 2,827 h | 177 h |
| 平均等待 | 21.9 h | 1.4 h |
| 需要等待的病人 | 13 人 | 1 人 |
| 最长等待 | 527 h（22 天） | 177 h |
| 被 SNF 拒绝的申请 | 16 次 | 0 |
| 高紧急度病人平均等待（33 人） | 3.6 h | 5.4 h |

解读：按这周的空床快照，Brooklyn 的床位总量并不缺（129 人对 181 张床），等待几乎全部来自**协调失败**：几家医院把病人同时推给同几家最近的 SNF，先到先得导致少数人被反复拒绝、等很久。CCN 统一分配后几乎没人等。但高紧急度病人的平均等待略增，说明这个场景里 CCN 的收益主要在"避免极端长等"。

**敏感性（不同转介名单长度）：**

| 每个病人发给几家 SNF | Baseline 总等待 | CCN 总等待 |
|---|---|---|
| 约 3 家 | 12,996 h | 4,082 h |
| 约 5 家（默认） | 2,119–5,456 h（不同随机种子） | 177–430 h |
| 约 8 家 | 159 h | 0 h |

转介范围越窄，等待越严重，CCN 的价值越大。**转介名单长度是需要向医院个案管理部门或导师求证的关键参数。**

## 局限

- **人员约束跑不动：** 加上真实人员数据后，即使缩小到 1:6（64 人）也超过 3 分钟、1:3 超过 10 分钟。原因是 SNF 住院期长（平均 42 天），现有算法逐个班次检查人员，计算量随住院天数增长。代码里已找到一处可提速的地方（每次检查都遍历所有 SNF 的班次），但单靠这处还不够，需要改算法。
- **人员数据的含义：** 模型假设床位一开始是空的；真实 SNF 的护士大部分在照顾现有住户。我用"该 SNF 每位住户的实际工时 × 空床数"估算可用于新病人的人力，这是近似。CMS 数据显示 Brooklyn SNF 实际工时（3.47 h）普遍低于病例组合预期（4.33 h），一旦加入人员约束，人手很可能成为主要瓶颈。
- 床位普查是 10 月 7 日那一周的快照，医院占用率是 11 月 18 日的数据，时间点不完全一致。

## 数据来源

- NYS DOH Open Data：[health.data.ny.gov](https://health.data.ny.gov)（sf4k-39ay、2dbc-sqe7、izta-vnpq、vn5v-hh5r）
- CMS Provider Data Catalog：[Provider Information](https://data.cms.gov/provider-data/dataset/4pq5-n9py)
- CMS：[Payroll Based Journal Daily Nurse Staffing](https://catalog-beta.data.gov/dataset/payroll-based-journal-daily-nurse-staffing)（2026 Q2）
- CMS：Medicare Post-Acute Care Utilization – SNF by Geography and Provider（2024）
