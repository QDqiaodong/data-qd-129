# 线下促销优惠券预占(SQLite 独立账本 · 精确整数最优链路)

面向线下促销运营的第五条交付链 `basket-rules-vouchers`。它与滚动库、挖掘报告、公开账本和
别名图完全隔离:只消费一份**完整静态购物篮快照**(字段、商品标识与参数校验直接复用
`basket-rules` 的 `model.parse_snapshot`),在独立账本中固定**不可变活动目录**,并记录
请求、输入语义身份、分配与活动占用。旧命令 `basket-rules`、`basket-rules-roll`、
`basket-rules-publish`、`basket-rules-normalize` 行为不变,本链路不读不写它们的任何文件。

## 命令

```sh
basket-rules-vouchers init --ledger run/vouch.db --activities run/activities.json
basket-rules-vouchers allocate --ledger run/vouch.db --request-id req-001 \
    --source run/snapshot.json --output run/alloc-001.json
basket-rules-vouchers cancel --ledger run/vouch.db --request-id req-001 \
    --output run/cancel-001.json
```

- `init`:从 JSON 数组固定活动目录,账本已存在拒绝,账本与活动文件不得同路径。
- `allocate`:对本次 1..200 篮求**全局最优**可执行分配并预占;同 id 同身份重试重发原分配。
- `cancel`:只释放该请求已占用的篮与券额度;重复取消幂等,未知 id 失败(退出 2)。

## 活动目录(初始化后不变)

JSON 数组,至多 30 个活动,`id` 唯一;每个活动恰含四个字段:

| 字段 | 约束 |
| --- | --- |
| `id` | 非空标识,≤128 字符,无控制字符(复用 `common.ident`) |
| `required_items` | 1..12 个**不重复**商品标识 |
| `quota` | 1..200 整数 |
| `discount_cents` | 1..10^9 整数 |

相同所需商品可以对应不同活动(同集合、不同 id/额度/折扣均合法)。目录在 `init` 后只读,
账本不提供修改接口;`required_items` 规范化为排序数组后持久化。

## 快照与适用规则

快照与 `basket-rules --source` 同结构(`min_support_count`、`max_itemset_size`、
`min_confidence{numerator,denominator}`、`baskets[{id,items}]`),篮 id 唯一、篮内商品
唯一、空篮合法、全体商品 ≤12 种;本链路另要求篮数为 1..200。篮包含某活动**全部**所需商品
才适用该活动;每篮至多一券;跨所有未取消请求,每个活动的未取消预占数不得超过其 `quota`。

空篮、无适用活动或额度耗尽都**成功提交零分空分配**(全部篮 `activity_id=null`、
`total_discount_cents=0`),绝不把请求改成失败;null 篮写入 `allocations` 但不进
`occupied_baskets`、不扣任何额度。

## 最优目标与确定性平局

1. 全局最大化折扣总分(各得券篮 `discount_cents` 之和,精确整数,无浮点)。
2. 总分并列时,先把全部篮按篮 ID 排序,再比较每篮所得活动 ID 或 `null` 组成的向量:
   活动 ID 按 Python 字符串序,`null` 视为大于全部活动 ID,取**字典序最小**的向量。

求解器是整数最小费用流(SPFA 势初始化 + 连续最短路 Dijkstra),不是逐篮贪心。每条"篮→活动"
边的费用把两个目标编码进一个精确整数:设活动数 k、篮数 n、基数 M=k+1(null 的秩为 k),

```
cost(篮 i 的选择) = -discount * M^n + rank(选择) * M^(n-1-i)
```

位置项是严格小于 M^n 的基数 M 数,因此折扣最大化永远主导,平分时自动落到字典序最小的秩向量。
篮节点必有一条直通汇点的 null 边,故流恒可行,每个篮恰好流出 1 单位。

## 语义身份与重试/冲突

身份 = 经校验的完整快照**结构**的规范化 JSON(键排序、篮按 id 排序、篮内商品排序)的
SHA-256;忽略对象键顺序、篮排列与篮内商品排列,其余参数和内容全部参与,**输出路径不参与身份**。

- 同 `request-id` + 同身份:返回账本存档的原分配与当前状态,**不再求解、不再占额**;
  报告逐字节一致,可换输出路径。
- 同 `request-id` + 不同身份:拒绝(退出 2),旧记录与旧输出不变。
- 跨未取消请求,同一篮 ID 只能预占一次:新请求包含任一已占用篮时,**整次冲突回滚**,
  即使请求中另有互不相交的篮也不部分提交。

## 取消语义

`cancel` 在一个写事务内删除该请求的 `occupied_baskets` 行、按活动回减 `activity_usage`,
并把请求置为 `cancelled`;`allocations` 与存档报告保留。取消只释放该请求自己的占用,
其他请求的篮与额度不受影响。重复取消幂等(回执 `released=false`)。取消后用**同一 id 同一
身份**调 `allocate`,回执 `status=cancelled`、`allocation_in_effect=false` 并重发原分配,
**绝不重新占额**;要再次申请必须使用新 `request-id`。取消释放会改变下一新请求面对的可行
图与最优解。

## 账本结构

| 表 | 内容 |
| --- | --- |
| `config` | schema 版本、活动文件路径与字节 SHA-256 |
| `activities` | 固定活动目录(所需商品、额度、折扣、序号) |
| `requests` | 每请求一行:状态 reserved/cancelled、身份哈希、源字节哈希、报告字节存档、时间戳 |
| `allocations` | 每请求的全部篮(含 null 篮)及其活动 ID、折扣、篮序号 |
| `occupied_baskets` | 仅未取消请求的得券篮:篮 ID 全局唯一预占 |
| `activity_usage` | 每活动当前未取消占用数,始终 ≤ quota |

报告含全部篮及其券(或 null)、`total_discount_cents`、`coupon_baskets` 与各活动本次
占用数 `activity_occupancy`;回执额外区分 `status`(reserved/cancelled)、
`allocation_in_effect`(原分配当前是否仍生效)、`replayed`、全局占用总数与各活动当前用量。

## 并发线性化边界

账本启用 WAL、`synchronous=FULL`、`busy_timeout=30s`;每条 `allocate`/`cancel` 以
`BEGIN IMMEDIATE` 开启写事务,SQLite 写锁使命令在单账本上严格串行。剩余额度与篮占用在
同一事务、同一已提交状态下读取,最优解、请求记录、篮占用与扣额在同一事务提交:

- 两个并发 `allocate` 等价于某个串行顺序中各自求当时最优:后到者要么看到先到者已占用的篮
  而整次冲突,要么在剩余额度上求自己的最优,不可能用过时图提交次优方案或超额。
- `cancel` 与 `allocate` 并发:若分配先线性化且引用了将被释放的篮,它按冲突失败回滚;
  若取消先线性化,分配在释放后的新可行图上求新最优。两种串行序都可能发生,均合法。
- 同 id 并发重试:写锁串行化,后到者只重发存档,不二次预占。

线性化点即写事务的 `COMMIT`;每条命令的回执对应它自己的线性化时刻,后续取消**不追溯改写**
已经输出的文件(取消命令写出的是该请求存档的原分配字节)。不持有数据库事务等待用户交互:
文件发布在事务提交之后。

## 崩溃、发布失败与恢复边界

1. 提交前异常(含 `SIGKILL`、断电、测试缝 `BASKET_RULES_VOUCHERS_ABORT=1`):未提交帧由
   SQLite 回滚,无请求行、无篮占用、无扣额;同 id 重试是一次全新求解,不存在半次预占。
2. 提交后、输出落盘前崩溃或磁盘写失败(测试缝
   `BASKET_RULES_VOUCHERS_DELAY_SECONDS` 可放大该窗口):请求与占用已持久;同 id 重试
   逐字节重发存档,不重新求解、不二次扣额。
3. 输出经同目录临时文件 + `os.replace` 原子替换,失败保留旧文件;符号链接拒绝,输出不得
   覆盖源快照、活动文件或账本,任何校验/冲突错误退出码 2 且不产生部分输出。
4. 重启后状态完全由已提交的表决定:零用量行(`used=0`)可能因先占后释放而保留,
   `occupied_baskets` 与各活动用量始终互相一致且不超额度。

## 求解复杂度

设 n=篮数(≤200)、k=活动数(≤30)。建图 O(nk),最小费用流恰好推 n 个单位流,每单位一次
Dijkstra:O(n·E·log V),E=O(nk)、V=O(n+k),即 O(n²k·log(n+k));费用整数可达
10^9·M^n 量级,Python 任意精度整数精确承载,无浮点近似。空间 O(nk+n)。

## 不做的事

不消费滚动库、挖掘报告、公开账本或别名图;不修改任何输入文件;不做逐篮贪心、浮点近似或
概率/随机选择;活动目录初始化后不可变;不提供跨机复制或多写者文件系统承诺。
