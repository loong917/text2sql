# Text2SQL 业务问答规则

## 1. 问法归一化

以下问法在语义上等价，训练时应尽量映射到同一类 SQL 模式：

- 机构 = 单位 = 血站 = 采血机构 = 采供血机构
- 城市 = 地市 = 所属城市
- 采集人次 = 采血人次 = 献血人次 = 人次 = 次数
- 采集量 = 采血量 = 采集总量
- 全血 = `BCType = '0'`
- 成分血 = `BCType = '1'`
- 2024年 = `BCDate >= '2024-01-01' AND BCDate < '2025-01-01'`
- 2025年 = `BCDate >= '2025-01-01' AND BCDate < '2026-01-01'`

## 2. 指标与维度映射

### 2.1 指标映射

- 如果问题问“采集人次 / 献血人次 / 次数”，优先使用 `COUNT(*)`
- 如果问题问“采集量 / 成分血采集量 / 血量”，优先使用 `SUM(a.BCPVolume)`
- “全血 / 成分血”决定的是 `BCType` 过滤条件，不直接决定使用 `COUNT(*)` 还是 `SUM(a.BCPVolume)`
- 如果问题没有明确给出“人次”或“采集量”，不要自行猜测指标

### 2.2 维度映射

- 如果问题问“每个机构 / 各机构 / 每家单位 / 各血站”，优先使用 `b.InstID, b.OrgName`
- 如果问题问“每个城市 / 各城市 / 按城市”，优先使用 `b.City`

### 2.3 关联规则

- 涉及机构、城市等机构主数据时，必须关联 `Pub_OrgAddress`
- 标准关联方式：`Stat_Collection a JOIN Pub_OrgAddress b ON a.BTSID = b.InstID`
- 涉及采集全血统计时，必须关联 `Stat_Collection`, 使用 `a.BCType = '0'`
- 涉及采集成分血统计时，必须关联 `Stat_Collection`, 使用 `a.BCType = '1'`
- 不要把 `Stat_Collection.InstID` 当成真实字段使用，因为该表中用于关联机构的是 `BTSID`

## 3. 拒答边界

以下情况应直接拒答，不要捏造表名、字段名或业务规则：

- 问题中的指标在当前表结构里不存在，例如“回收量”
- 问题需要未知主题表，例如“火星基地血站”
- 问题没有给出足够维度或指标，且无法从训练规则中唯一确定 SQL
- 问题要求使用当前结构中不存在的字段，例如“回收量”

## 4. 标准 SQL 模板

### 模板1：按机构统计全血采集人次

问题模式：

- 统计每个机构某年的全血采集人次
- 统计各血站某年的全血采集人次

SQL语句：

```sql
SELECT b.InstID AS InstID, b.OrgName AS InstName, COUNT(*) AS Times
FROM Stat_Collection a
JOIN Pub_OrgAddress b ON a.BTSID = b.InstID
WHERE a.BCDate >= '2024-01-01' AND a.BCDate < '2025-01-01' AND a.BCType = '0'
GROUP BY b.InstID, b.OrgName
```

### 模板2：按机构统计全血采集量

问题模式：

- 统计每个机构某年的全血采集量
- 统计各血站某年的全血采集量

SQL语句：

```sql
SELECT b.InstID AS InstID, b.OrgName AS InstName, SUM(a.BCPVolume) AS Volume
FROM Stat_Collection a
JOIN Pub_OrgAddress b ON a.BTSID = b.InstID
WHERE a.BCDate >= '2024-01-01' AND a.BCDate < '2025-01-01' AND a.BCType = '0'
GROUP BY b.InstID, b.OrgName
```

### 模板3：按机构统计成分血采集人次

问题模式：

- 统计每个机构某年的成分血采集人次
- 统计各单位某年的成分血采集人次

SQL语句：

```sql
SELECT b.InstID AS InstID, b.OrgName AS InstName, COUNT(*) AS Times
FROM Stat_Collection a
JOIN Pub_OrgAddress b ON a.BTSID = b.InstID
WHERE a.BCDate >= '2024-01-01' AND a.BCDate < '2025-01-01' AND a.BCType = '1'
GROUP BY b.InstID, b.OrgName
```

### 模板4：按机构统计成分血采集量

问题模式：

- 统计每个机构某年的成分血采集量
- 统计各单位某年的成分血采集量

SQL语句：

```sql
SELECT b.InstID AS InstID, b.OrgName AS InstName, SUM(a.BCPVolume) AS Volume
FROM Stat_Collection a
JOIN Pub_OrgAddress b ON a.BTSID = b.InstID
WHERE a.BCDate >= '2025-01-01' AND a.BCDate < '2026-01-01' AND a.BCType = '1'
GROUP BY b.InstID, b.OrgName
```

### 模板5：按城市统计全血采集人次

问题模式：

- 统计每个城市某年的全血采集人次
- 统计各地市某年的全血采集人次

SQL语句：

```sql
SELECT b.City AS City, COUNT(*) AS Times
FROM Stat_Collection a
JOIN Pub_OrgAddress b ON a.BTSID = b.InstID
WHERE a.BCDate >= '2024-01-01' AND a.BCDate < '2025-01-01' AND a.BCType = '0'
GROUP BY b.City
```

### 模板6：按城市统计全血采集量

问题模式：

- 统计每个城市某年的全血采集量
- 统计各地市某年的全血采集量

SQL语句：

```sql
SELECT b.City AS City, SUM(a.BCPVolume) AS Volume
FROM Stat_Collection a
JOIN Pub_OrgAddress b ON a.BTSID = b.InstID
WHERE a.BCDate >= '2025-01-01' AND a.BCDate < '2026-01-01' AND a.BCType = '0'
GROUP BY b.City
```

### 模板7：按城市统计成分血采集人次

问题模式：

- 统计每个城市某年的成分血采集人次
- 统计各地市某年的成分血采集人次

SQL语句：

```sql
SELECT b.City AS City, COUNT(*) AS Times
FROM Stat_Collection a
JOIN Pub_OrgAddress b ON a.BTSID = b.InstID
WHERE a.BCDate >= '2024-01-01' AND a.BCDate < '2025-01-01' AND a.BCType = '1'
GROUP BY b.City
```

### 模板8：按城市统计成分血采集量

问题模式：

- 统计每个城市某年的成分血采集量
- 统计各地市某年的成分血采集量

SQL语句：

```sql
SELECT b.City AS City, SUM(a.BCPVolume) AS Volume
FROM Stat_Collection a
JOIN Pub_OrgAddress b ON a.BTSID = b.InstID
WHERE a.BCDate >= '2025-01-01' AND a.BCDate < '2026-01-01' AND a.BCType = '1'
GROUP BY b.City
```

### 模板9：带城市过滤的机构维度统计全血采集量

问题模式：

- 统计杭州市各机构2025年的全血采集量
- 查询杭州各血站2025年全血采集量

SQL语句：

```sql
SELECT b.InstID AS InstID, b.OrgName AS InstName, SUM(a.BCPVolume) AS Volume
FROM Stat_Collection a
JOIN Pub_OrgAddress b ON a.BTSID = b.InstID
WHERE a.BCDate >= '2025-01-01' AND a.BCDate < '2026-01-01' AND a.BCType = '0' AND b.City = '杭州市'
GROUP BY b.InstID, b.OrgName
```

### 模板10：带城市过滤的机构维度成分血采集量

问题模式：

- 统计宁波市各机构2024年的成分血采集量
- 查询宁波各血站2024年成分血采集量

SQL语句：

```sql
SELECT b.InstID AS InstID, b.OrgName AS InstName, SUM(a.BCPVolume) AS Volume
FROM Stat_Collection a
JOIN Pub_OrgAddress b ON a.BTSID = b.InstID
WHERE a.BCDate >= '2024-01-01' AND a.BCDate < '2025-01-01' AND a.BCType = '1' AND b.City = '宁波市'
GROUP BY b.InstID, b.OrgName
```

### 模板11：带城市过滤的机构维度全血采集人次

问题模式：

- 统计杭州市各机构2025年的全血采集人次
- 查询杭州各单位2025年全血人次

SQL语句：

```sql
SELECT b.InstID AS InstID, b.OrgName AS InstName, COUNT(*) AS Times
FROM Stat_Collection a
JOIN Pub_OrgAddress b ON a.BTSID = b.InstID
WHERE a.BCDate >= '2025-01-01' AND a.BCDate < '2026-01-01' AND a.BCType = '0' AND b.City = '杭州市'
GROUP BY b.InstID, b.OrgName
```

### 模板12：带城市过滤的机构维度成分血采集人次

问题模式：

- 统计杭州市各机构2025年的成分血采集人次
- 查询杭州各单位2025年成分血人次

SQL语句：

```sql
SELECT b.InstID AS InstID, b.OrgName AS InstName, COUNT(*) AS Times
FROM Stat_Collection a
JOIN Pub_OrgAddress b ON a.BTSID = b.InstID
WHERE a.BCDate >= '2025-01-01' AND a.BCDate < '2026-01-01' AND a.BCType = '1' AND b.City = '杭州市'
GROUP BY b.InstID, b.OrgName
```

## 5. 训练示例

### 问题1：统计每个机构2024年的全血采集人次

SQL语句:

```sql
SELECT b.InstID AS InstID, b.OrgName AS InstName, COUNT(*) AS Times
FROM Stat_Collection a JOIN Pub_OrgAddress b ON a.BTSID = b.InstID
WHERE a.BCDate >= '2024-01-01' AND a.BCDate < '2025-01-01' AND a.BCType = '0'
GROUP BY b.InstID, b.OrgName
```

### 问题2：统计每个机构2024年的全血采集量

SQL语句:

```sql
SELECT b.InstID AS InstID, b.OrgName AS InstName, SUM(a.BCPVolume) AS Volume
FROM Stat_Collection a JOIN Pub_OrgAddress b ON a.BTSID = b.InstID
WHERE a.BCDate >= '2024-01-01' AND a.BCDate < '2025-01-01' AND a.BCType = '0'
GROUP BY b.InstID, b.OrgName
```

### 问题3：统计每个机构2024年的成分血采集人次

SQL语句:

```sql
SELECT b.InstID AS InstID, b.OrgName AS InstName, COUNT(*) AS Times
FROM Stat_Collection a JOIN Pub_OrgAddress b ON a.BTSID = b.InstID
WHERE a.BCDate >= '2024-01-01' AND a.BCDate < '2025-01-01' AND a.BCType = '1'
GROUP BY b.InstID, b.OrgName
```

### 问题4：统计每个机构2024年的成分血采集量

SQL语句:

```sql
SELECT b.InstID AS InstID, b.OrgName AS InstName, SUM(a.BCPVolume) AS Volume
FROM Stat_Collection a JOIN Pub_OrgAddress b ON a.BTSID = b.InstID
WHERE a.BCDate >= '2024-01-01' AND a.BCDate < '2025-01-01' AND a.BCType = '1'
GROUP BY b.InstID, b.OrgName
```

### 问题5：统计每个城市2024年的全血采集人次

SQL语句:

```sql
SELECT b.City AS City, COUNT(*) AS Times
FROM Stat_Collection a JOIN Pub_OrgAddress b ON a.BTSID = b.InstID
WHERE a.BCDate >= '2024-01-01' AND a.BCDate < '2025-01-01' AND a.BCType = '0'
GROUP BY b.City
```

### 问题6：统计每个城市2025年的全血采集量

SQL语句:

```sql
SELECT b.City AS City, SUM(a.BCPVolume) AS Volume
FROM Stat_Collection a JOIN Pub_OrgAddress b ON a.BTSID = b.InstID
WHERE a.BCDate >= '2025-01-01' AND a.BCDate < '2026-01-01' AND a.BCType = '0'
GROUP BY b.City
```

### 问题7：统计每个城市2024年的成分血采集人次

SQL语句:

```sql
SELECT b.City AS City, COUNT(*) AS Times
FROM Stat_Collection a JOIN Pub_OrgAddress b ON a.BTSID = b.InstID
WHERE a.BCDate >= '2024-01-01' AND a.BCDate < '2025-01-01' AND a.BCType = '1'
GROUP BY b.City
```

### 问题8：统计每个城市2025年的成分血采集量

SQL语句:

```sql
SELECT b.City AS City, SUM(a.BCPVolume) AS Volume
FROM Stat_Collection a JOIN Pub_OrgAddress b ON a.BTSID = b.InstID
WHERE a.BCDate >= '2025-01-01' AND a.BCDate < '2026-01-01' AND a.BCType = '1'
GROUP BY b.City
```


### 问题9：统计杭州市各机构2025年的全血采集人次

SQL语句:

```sql
SELECT b.InstID AS InstID, b.OrgName AS InstName, COUNT(*) AS Times
FROM Stat_Collection a JOIN Pub_OrgAddress b ON a.BTSID = b.InstID
WHERE a.BCDate >= '2025-01-01' AND a.BCDate < '2026-01-01' AND a.BCType = '0' AND b.City = '杭州市'
GROUP BY b.InstID, b.OrgName
```

### 问题8：统计杭州市各机构2025年的全血采集量

SQL语句:

```sql
SELECT b.InstID AS InstID, b.OrgName AS InstName, SUM(a.BCPVolume) AS Volume
FROM Stat_Collection a JOIN Pub_OrgAddress b ON a.BTSID = b.InstID
WHERE a.BCDate >= '2025-01-01' AND a.BCDate < '2026-01-01' AND a.BCType = '0' AND b.City = '杭州市'
GROUP BY b.InstID, b.OrgName
```

### 问题9：统计宁波市各机构2025年的成分血采集人次

SQL语句:

```sql
SELECT b.InstID AS InstID, b.OrgName AS InstName, COUNT(*) AS Times
FROM Stat_Collection a JOIN Pub_OrgAddress b ON a.BTSID = b.InstID
WHERE a.BCDate >= '2025-01-01' AND a.BCDate < '2026-01-01' AND a.BCType = '1' AND b.City = '宁波市'
GROUP BY b.InstID, b.OrgName
```

### 问题10：统计宁波市各机构2025年的成分血采集量

SQL语句:

```sql
SELECT b.InstID AS InstID, b.OrgName AS InstName, SUM(a.BCPVolume) AS Volume
FROM Stat_Collection a JOIN Pub_OrgAddress b ON a.BTSID = b.InstID
WHERE a.BCDate >= '2025-01-01' AND a.BCDate < '2026-01-01' AND a.BCType = '1' AND b.City = '宁波市'
GROUP BY b.InstID, b.OrgName
```

## 6. 明确拒答示例

### 问题：统计火星基地各血站的回收量

处理原则：

- 当前知识中没有“回收量”指标
- 当前结构中没有“回收表”或 `回收量` 字段
- 应返回拒答，而不是猜测 SQL
