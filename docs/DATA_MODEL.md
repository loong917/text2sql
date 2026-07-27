# 数据模型说明

本文档供开发、业务和审核人员阅读，不参与训练或运行时检索。数据库实时
Schema 是结构事实来源，机器使用的业务定义位于 `knowledge/`。

## 核心模型

### `Stat_Collection`

采集事实表，一条记录代表一次采集事实。

关键字段：

- `CollectionID`：采集记录唯一标识。
- `BTSID`：采集机构编号。
- `BCDate`：采集日期，业务时间统计优先使用该字段。
- `BCType`：采集类型，`0` 表示全血，`1` 表示成分血。
- `BCPVolume`：采集量；不同采集类型的计量口径可能不同，跨类型汇总前需要业务确认。
- `DonorID`：献血者标识。
- `ABO`、`RhD`、`Sex`、`Age`：献血者相关分析维度。
- `TeamFlag`：是否团队采集。

`BTSName` 是冗余机构名称。涉及正式机构名称和城市统计时，以机构维度表为准。

### `Pub_OrgAddress`

机构维度表，一条记录代表一个机构。

关键字段：

- `InstID`：机构主键。
- `OrgCode`：机构代码。
- `OrgName`：机构名称。
- `OrgShortName`：机构简称。
- `City`、`District`：机构所在地理维度。
- `IsCentral`：是否中心机构。
- `RUsingFlag`：数据有效标识。

## 关联关系

采集事实与机构维度的标准关联为：

```text
Stat_Collection.BTSID -> Pub_OrgAddress.InstID
```

机构名称、城市和地区必须从 `Pub_OrgAddress` 获取。不要使用不存在的
`Stat_Collection.InstID` 或 `Stat_Collection.City`。

## 指标口径

- 采集人次：对符合条件的采集事实记录执行 `COUNT(*)`。
- 采集量：对符合条件的 `BCPVolume` 执行 `SUM`。
- 全血、成分血是采集类型过滤条件，不决定聚合函数。
- 没有明确“人次”或“采集量”时，不应自行猜测指标。

机器可执行的指标定义以
[`knowledge/domain/metrics.json`](../knowledge/domain/metrics.json) 为准。

## 数据质量注意事项

- 查询是否需要过滤 `RUsingFlag` 应由业务规则明确，不能仅凭字段名称推断。
- `BCPVolume` 在全血和成分血下可能使用不同业务单位，展示结果时应说明口径。
- `BTSName` 与机构主数据名称不一致时，以 `Pub_OrgAddress.OrgName` 为准。
- 业务时间过滤建议使用左闭右开的日期区间，避免时间分量造成遗漏。
