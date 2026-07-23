# Text2SQL 结构与关联说明

## 1. 总体规则

### 1.1 核心主题

- `Stat_Collection` 是采集事实表，主要承载采集日期、采集类型、采集量、献血者属性等事实信息
- `Pub_OrgAddress` 是机构维度表，主要承载机构名称、城市等主数据

### 1.2 推荐关联

- 标准关联方式：`Stat_Collection.BTSID = Pub_OrgAddress.InstID`
- 涉及机构、城市、机构名称时，通常必须关联 `Pub_OrgAddress`
- 不要把 `Stat_Collection` 当成包含 `InstID`、`City`的完整维度表

### 1.3 常见查询模式

- 按机构统计：`GROUP BY b.InstID, b.OrgName`
- 按城市统计：`GROUP BY b.City`
- 统计采集人次：`COUNT(*)`
- 统计采集量：`SUM(a.BCPVolume)`
- “全血 / 成分血”只决定 `a.BCType` 的过滤条件，不决定聚合函数
- “全血采集量”应使用 `SUM(a.BCPVolume)`，不是 `COUNT(*)`
- “成分血采集人次”应使用 `COUNT(*)`，不是 `SUM(a.BCPVolume)`

## 2. Pub_OrgAddress

### 2.1 业务含义

记录机构主数据，用于补充机构名称、城市等维度信息。

### 2.2 DDL定义

```sql
CREATE TABLE Pub_OrgAddress (
    OrgCode varchar(50) NOT NULL,
    InstID bigint NOT NULL,
    OrgName varchar(254) NULL,
    OrgShortName varchar(254) NULL,
    RUsingFlag char(1) NOT NULL,
    RTimeStamp timestamp NOT NULL,
    IsCentral varchar(50) NULL,
    Description varchar(50) NULL,
    City varchar(50) NULL,
    District varchar(50) NULL,
    CONSTRAINT Pub_OrgAddress_PK PRIMARY KEY (InstID)
);
```

### 2.3 主键与角色

- 主键：`InstID`
- 表角色：机构维度表
- 主粒度：一条记录代表一个机构

### 2.4 关键字段

- `InstID`：机构编号，机构维度主键
- `OrgName`：机构名称，按机构汇总时常用
- `OrgShortName`：机构简称
- `City`：城市维度
- `IsCentral`：是否中心血站，枚举值通常为 `1/0`
- `RUsingFlag`：有效标识，通常 `1` 表示有效

### 2.5 查询建议

- 如果问题里出现“机构 / 单位 / 血站”，优先考虑 `OrgName`
- 如果问题里出现“城市 / 地市”，优先考虑 `City`
- 如果只问机构主数据，不涉及采集事实，可单表查询 `Pub_OrgAddress`

## 3. Stat_Collection

### 3.1 业务含义

记录血液采集事实信息，是采集类统计问题的核心事实表。

### 3.2 DDL定义

```sql
CREATE TABLE Stat_Collection (
    CollectionID bigint NOT NULL,
    BTSID bigint NULL,
    BTSName varchar(50) NULL,
    DonorID bigint NULL,
    DonationCode varchar(20) NOT NULL,
    ABO varchar(20) NULL,
    RhD varchar(20) NULL,
    BCDate datetime NULL,
    BCType varchar(20) NULL,
    TeamFlag char(1) NULL,
    BCPVolume decimal(10, 2) NULL,
    BirthDate datetime NULL,
    Age bigint NULL,
    Sex varchar(20) NULL,
    Occupation varchar(20) NULL,
    EducationLevel varchar(20) NULL,
    RCreationDate datetime NULL,
    RCreator bigint NULL,
    RCreationInst bigint NULL,
    RModifyDate datetime NULL,
    RModifier bigint NULL,
    RModifyInst bigint NULL,
    RUsingFlag char(1) NOT NULL,
    RTimeStamp timestamp NOT NULL
);

ALTER TABLE Stat_Collection
ADD CONSTRAINT Stat_Collection_Pub_OrgAddress_FK
FOREIGN KEY (BTSID) REFERENCES Pub_OrgAddress(InstID);
```

### 3.3 表角色

- 表角色：采集事实表
- 主粒度：一条记录代表一次采集事实
- 常见时间字段：`BCDate`
- 常见指标字段：`BCPVolume`
- 常见机构关联字段：`BTSID`

### 3.4 关键字段

- `CollectionID`：采集记录唯一号
- `BTSID`：机构编号，关联 `Pub_OrgAddress.InstID`
- `BTSName`：机构名称冗余字段，优先以维表 `OrgName` 为准
- `DonorID`：献血者唯一号
- `DonationCode`：献血编号
- `BCDate`：采集日期
- `BCType`：采集类型，`0=全血`，`1=成分血`
- `BCPVolume`：采集量，全血为毫升，成分血为治疗量
- `ABO`：血型，`1=A型`，`2=B型`，`3=O型`，`4=AB型`
- `RhD`：RhD，`1=阴性`，`2=阳性`
- `Sex`：性别，`1=男`，`2=女`
- `TeamFlag`：是否团队，`0=否`，`1=是`
- `Age`：年龄
- `Occupation`：职业
- `EducationLevel`：学历
- `RUsingFlag`：有效标识，通常 `1` 表示有效

### 3.5 指标与维度分类

#### 维度字段

- `BTSID`
- `BTSName`
- `BCDate`
- `BCType`
- `ABO`
- `RhD`
- `Sex`
- `TeamFlag`
- `Occupation`
- `EducationLevel`

#### 指标字段

- `COUNT(*)` 用于统计采集人次
- `SUM(BCPVolume)` 用于统计采集量
- `BCType='0'` 与 `BCType='1'` 都可以和 `COUNT(*)` 或 `SUM(BCPVolume)` 组合，具体取决于问题中的指标表达

#### 时间字段

- `BCDate`
- `RCreationDate`
- `RModifyDate`

## 4. 主外键关系

### 4.1 明确关系

```sql
Stat_Collection.BTSID -> Pub_OrgAddress.InstID
```

### 4.2 业务含义

- `Stat_Collection` 通过 `BTSID` 找到采集机构
- 机构名称、城市等维度信息应从 `Pub_OrgAddress` 获取
- 生成 SQL 时，不要凭空使用 `Stat_Collection.City`

## 5. 枚举与过滤规则

### 5.1 BCType

- `0`：全血
- `1`：成分血

### 5.2 RhD

- `1`：阴性
- `2`：阳性

### 5.3 Sex

- `1`：男
- `2`：女

### 5.4 TeamFlag

- `0`：否
- `1`：是

### 5.5 常见时间过滤

- 2024年：`BCDate >= '2024-01-01' AND BCDate < '2025-01-01'`
- 2025年：`BCDate >= '2025-01-01' AND BCDate < '2026-01-01'`

## 6. 推荐写法与禁用写法

### 6.1 推荐写法

```sql
SELECT b.OrgName, COUNT(*) AS Times
FROM Stat_Collection a
JOIN Pub_OrgAddress b ON a.BTSID = b.InstID
WHERE a.BCType = '0'
GROUP BY b.OrgName
```

### 6.2 禁用写法

- 不要写 `Stat_Collection.InstID = Pub_OrgAddress.InstID`
- 不要写不存在的表，如 `BloodScrapRecord`
- 不要写不存在的字段，如 `ScrapAmount`
- 不要在需要城市时省略与 `Pub_OrgAddress` 的关联
- 不要把“全血”直接等同于 `COUNT(*)`
- 不要把“成分血”直接等同于 `SUM(a.BCPVolume)`

## 7. Text2SQL 提示

- 当问题提到机构、城市时，优先识别为“事实表 + 维度表”的 join 查询
- 当问题提到“采集人次”，优先生成 `COUNT(*)`
- 当问题提到“采集量”，优先生成 `SUM(BCPVolume)`
- 当问题提到“全血”或“成分血”时，先映射到 `a.BCType`，再根据“人次 / 采集量”决定聚合函数
- 当问题超出当前结构能力范围时，应拒答，不应猜测新表或新字段
