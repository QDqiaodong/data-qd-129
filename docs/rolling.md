# 滚动窗口购物篮统计(SQLite 修订批链路)

面向持续修订的购物篮流:`basket-rules-roll` 把 JSONL 批次的版本化修订提交进 SQLite,
按水位维护固定秒数滚动窗口内的活跃篮集合,并增量持久化至多 3 项商品组合计数。
报告只读持久计数,沿用快照链路的精确分数、门槛与排序。旧命令 `basket-rules` 行为不变。

## 命令

```sh
basket-rules-roll init --db roll.db --window-seconds 100 \
    --min-support-count 2 --max-itemset-size 3 --min-confidence 1 2
basket-rules-roll load --db roll.db --watermark 1000 --source batch.jsonl
basket-rules-roll report --db roll.db --output report.json
```

- `init` 建库并**固定**窗口秒数与原挖掘参数(min_support_count、max_itemset_size 1..3、
  min_confidence 整数分数 0..1);库已存在则拒绝。参数随每份报告回显。
- `load` 以非递减水位 `w` 提交一个 JSONL 批,stdout 输出
  `{applied,idempotent,ignored,expired,active_count,watermark}`。
- `report` 由持久计数生成与快照链路同构的 `result`(basket_count、item_universe、
  frequent_itemsets、rules、scope),外加 watermark 与固定参数;原子替换输出文件。
- 任何失败退出 2:库不变、旧报告保留。输入与输出不得相同,输出符号链接拒绝。

## JSONL 行契约

每行一个对象,字段恰好为 `{id,rev,time,items}`:

- `id`:非空标识串(≤128 字符,无控制字符,首尾无空白),同快照链路。
- `rev`:正整数版本(≤2^62)。同一 id 的版本全序,高版本覆盖低版本。
- `time`:非负整数秒(≤2^62)。`time > w` 的未来记录拒绝,整批失败。
- `items`:`null` 表示删除(墓碑);否则沿用商品校验——≤12 个去重标识,
  篮内重复拒绝;`[]` 是合法空篮并计入分母。商品顺序无关,存储前排序规范化。

批文件 ≤64MB、≤100 万行、单行 ≤1MB;空行、坏 JSON、重复键、非有限数值、缺/多字段均为坏行。
任何坏行使整批失败,不留半批。

## 批处理语义(按行)

设库中当前版本为 `(rev0,time0,items0)`:

- 行 `rev < rev0`:低版本,忽略(`ignored`)。
- 行 `rev == rev0` 且 `(time,items)` 规范化后相同:幂等空操作(`idempotent`),重放安全。
- 行 `rev == rev0` 但内容不同:冲突,**整批失败**,库不变。
- 行 `rev > rev0`(或新 id):应用(`applied`)。高版本可改商品、改时间或从删除/过期恢复。

过期与删除的篮**保留最高版本**行,绝不物理删除,使迟到的低版本仍被正确忽略。
活跃判定:`items` 非 null 且 `w-window < time <= w`;空篮计入分母。
活跃上限 10 万篮,批内瞬时超限即整批失败(同批先删后加可通过)。

## 事务与恢复边界

每个批是一个 `BEGIN IMMEDIATE … COMMIT` 事务,**版本、水位、篮状态、组合计数四者同
事务提交**;任何校验失败(坏行、未来记录、版本冲突、水位回退、活跃超限)触发
`ROLLBACK`。库启用 WAL 与 `synchronous=FULL`:

- 提交前进程中止(含 kill -9、断电):未提交帧在下次打开时被恢复逻辑丢弃,**不留半批**。
- 提交后中止:该批完整持久;重放同批时同版本同内容行幂等,**不重计**。
- 冲突批与失败批不触碰任何已提交状态,旧报告文件原样保留。
- 报告文件经临时文件 + `os.replace` 原子替换;报告只读 `meta` 与 `counts`,不扫篮表。

恢复边界即 SQLite 事务边界:单写者(单操作者批处理),不承诺多进程并发写或跨机复制。

## 索引策略

推进水位与修订都**不得全量扫描篮表**,靠两个索引保证:

- `baskets(id)` 主键:修订按 id 点查当前版本,O(log n)。
- `idx_baskets_expiry ON baskets(time) WHERE active=1` 部分索引:水位从 `w0` 推进到
  `w1` 时,仅范围扫描 `active=1 AND time <= w1-window` 的到期行;行过期后 `active=0`
  自动脱离该索引。已过期/已删除行不参与任何扫描。
- `counts(combo)` 主键:组合计数按主键 upsert 增量加减,归零即删。

修订本身也是增量:旧贡献减、新贡献加,只触及该篮 ≤C(12,1)+C(12,2)+C(12,3)=298 个组合。
报告读取 `counts` 全表(持久统计,非篮表)与 `meta` 分母,经 `mining.summarize` 产出
精确 Fraction 分数、count≥门槛过滤与确定性排序,与快照链路逐字节同构。
无活跃篮时 `frequent_itemsets` 与 `rules` 输出空列表。

## 不变量(每批提交后成立,测试逐批断言)

- `counts` 无 ≤0 行;`meta.active_count` = `baskets` 中 `active=1` 行数。
- `active=1` ⟺ `items` 非 null 且 `w-window < time <= w`(w 为已提交水位)。
- 每个 id 只保留最高 `(rev,time,items)`;水位非递减。
