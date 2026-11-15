# 商品别名归一(SKU 快照预处理)

`basket-rules-normalize` 把既有购物篮快照中的商品别名按**某一日期**折叠到规范 SKU,
输出与输入结构完全相同的快照,可直接交给既有 `basket-rules --source` 挖掘。
旧命令与滚动/发布链行为不变;本链只做标识符重写,不读 SQLite、不联网。

## 命令

```sh
basket-rules-normalize \
    --source input.json \
    --canonical canonical.json \
    --aliases aliases.jsonl \
    --as-of 2026-07-01 \
    --output normalized.json
basket-rules --source normalized.json --output report.json
```

- `--source`:既有快照,契约同 [contract.md](contract.md)(字段、整数范围、篮 ID、篮内去重、
  1..1000 篮、每篮 ≤12 项)。只读,原字节 SHA256 写入回执。
- `--canonical`:JSON 数组,规范 SKU 标识符;去重且每项符合标识符规则。≤64MB / ≤50 万项。
- `--aliases`:JSONL,每行恰好 `{alias,target,valid_from,valid_to}`;≤64MB / ≤50 万行 / 单行 ≤1MB。
- `--as-of YYYY-MM-DD`:判定生效映射的日期,必须是真实存在的日历日(严格四位年-月-日)。
- `--output`:归一后的快照;临时文件 + `os.replace` 原子替换,失败保留旧文件。

成功时 stdout 输出单行 JSON 回执:

```json
{"as_of":"2026-07-01","aliases_sha256":"…","basket_count":4,"canonical_sha256":"…",
 "effective_aliases":3,"folded_items":1,"items_after":5,"items_before":6,
 "output":"normalized.json","resolved_items":4,"source_sha256":"…"}
```

`source_sha256` / `canonical_sha256` / `aliases_sha256` 均为**输入原字节**的 SHA256;
`resolved_items` 是经别名链改写的篮内项次数,`folded_items` 是因多 ID 归一到同一 SKU
而丢弃的重复项次数。

## 有效期

区间为**左闭右开** `[valid_from, valid_to)`;`valid_to = null` 表示无期限。
某记录在 `as_of` 生效当且仅当 `valid_from <= as_of` 且(`valid_to` 为 null 或 `as_of < valid_to`)。
因此同一别名可以在相邻区间切换目标:前一天归到 A,当天起归到 B。
`valid_to` 必须严格晚于 `valid_from`,不接受空区间。

## 生效图与失败边界(全部使整次运行失败,退出 2,输出不变)

只考察 `as_of` 当日生效的记录构成的图:

- **每个别名恰有一条出边**:同一别名两条生效记录即有效期重叠
  (`overlapping validity`),整次失败。
- **别名不得与规范 SKU 重名**:生效记录中 `alias` 出现在规范 SKU 集合里即失败。
- **所有链必须到达规范 SKU**:沿 `alias -> target` 前进,`target` 必须继续是别名或规范 SKU。
  目标两者都不是即**悬空**(`dangling`);沿链回到已访问节点即**环**(`cycle`),含自指 `a -> a`。
- **篮内项必须可解析**:每个篮内项要么是规范 SKU,要么是当日生效的别名;
  否则该篮项无定义(`neither a canonical SKU nor an effective alias`)。
- **归一后商品全集 ≤12**:折叠后仍超过快照契约的 12 种 SKU 即失败。

文件级结构错误同样整次失败:坏 JSON、重复键、非有限数值、缺/多字段、空行、
空白标识符、非严格日期、非真实日历日(如 `2026-02-30`)、单行/总大小/总行数超限。
结构校验作用于**每一行**,即使该行在 `as_of` 不生效;而重叠、重名、悬空、环等图约束
只作用于生效子图——不在当日的记录不影响当日结果。

非生效记录与当日的悬空/环无关,这使同一份别名日志可以跨日期复用,而日期切换只需改 `--as-of`。

## 归一语义

- 篮内 ID 沿链解析到根:别名 `a -> b -> sku` 与直接 `sku` 都记为 `sku`。
- **折叠**:同一篮内多个 ID 解析到同一 SKU 只保留一项;若某 ID 解析结果已存在,
  该重复项被丢弃并计入 `folded_items`。
- 输出仅含 `min_support_count`、`max_itemset_size`、`min_confidence`、`baskets` 四个字段,
  参数逐值原样保留;**篮 ID 原样保留**(不重排、不改写),空篮保留,篮顺序即输入顺序。
- 每篮 `items` 排序并去重,因此输出对篮内原始排列不敏感,可重复得到逐字节相同结果。
- 解析发生在校验之后:任何失败都不写输出,旧文件按原字节保留。

## 复杂度

设 `A` 为别名记录数(≤50 万),`K` 为规范 SKU 数,`N` 为篮数(≤1000),`I` 为每篮项数(≤12):

- 读入与逐行校验 `O(A)`;哈希表存生效边,空间 `O(A)`。
- 链解析**迭代 + 记忆化**,每条边最多访问常数次,`O(A)`;先按别名排序使失败信息确定,
  最坏 `O(A log A)`。**不递归**,链长不受 Python 递归深度限制(测试覆盖 6 万层链)。
- 篮归一 `O(N·I)` 解析 + 每篮 `O(I log I)` 排序。
- 合计时间 `O(A log A + N·I log I)`,空间 `O(A + N·I)`。
- 输入整体读入内存(别名 ≤64MB、快照 ≤8MB),不流式;单操作者批处理,不承诺并发。

## 失败与恢复边界

- 失败统一退出 2 并 stderr 给出首条错误;输出路径不存在/等于任一输入/是符号链接均拒绝,
  绝不覆盖输入。
- 输出经同目录临时文件 + `os.replace`;进程在替换前中止则临时文件可能残留于输出目录
  (下次成功运行覆盖同名输出,不影响正确性),替换后中止则该次结果完整。
- 无跨文件事务:本链只读输入、只写一个输出文件,不维护持久状态,重跑即幂等。
