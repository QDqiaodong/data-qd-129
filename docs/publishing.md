# 公开统计发布(SQLite 独立账本 · 加噪直方图链路)

面向对外发布的购物篮统计:`basket-rules-publish` 从滚动链路的 SQLite 源库读取一个
一致快照,在**独立的发布账本**中固定源库、公开商品全集(≤12)与正整数发布额度,
对每格直方图独立施加对称双边几何噪声后发布。旧命令 `basket-rules`、`basket-rules-roll`
行为不变,发布链路**绝不写源库**。

## 命令

```sh
basket-rules-publish init --ledger publish.db --source run/roll.db \
    --universe run/universe.json --quota 5
basket-rules-publish publish --ledger publish.db --request-id req-001 --output run/pub-001.json
basket-rules-publish publish --ledger publish.db --request-id req-001 --output run/pub-001-copy.json
```

- `init` 建独立账本并**固定**三项内容:源库路径与其挖掘参数(窗口秒数、min_support_count、
  max_itemset_size、min_confidence 分数)、公开商品全集、正整数发布额度。账本已存在拒绝,
  账本与源库不得同路径。全集文件是 JSON 标识数组,去重排序后须为 1..12 个商品。
- `publish` 新 `request-id`:在源库**同一只读事务**取参数与活跃篮快照,生成包含空篮模式的
  全部篮型直方图并逐格加噪;活跃篮出现全集外商品即拒绝。报告完整字节在账本事务内存档并
  扣一次额度后,才原子写输出;额度耗尽拒绝新 id。
- `publish` 旧 `request-id`:逐字节重发账本存档,可换输出路径;**不读源库、不重抽噪声、
  不扣额度**。退出码 0,收据 `status` 为 `replayed`。

## 噪声契约(逐格独立)

每格真实计数 `c` 的噪声为 `Z = G1 - G2`,其中 G1、G2 独立同分布几何:

- `P(G=k)=2^(-k-1)`,`k>=0`(即连续公平随机比特中首个 0 之前的 1 的个数)。
- 因此 Z 的期望为 0,分布为 `P(Z=0)=1/3`、`P(Z=k)=2^(-|k|)/3`(k≠0);每格独立抽取,包括空篮格。
- 随机源为系统安全随机源(`secrets`/`os.urandom`),不用可播种的伪随机数。
- 输出计数 `noisy_count = max(0, c + Z)`,截负为 0;报告不保留真实计数。

## 输出内容(只含可公开信息)

- `noisy_histogram`:全部出现过的篮型(含 `items: []` 空篮格)及其加噪计数;篮型按
  商品元组排序,格内商品规范化排序。未出现的篮型不发格,不据此推断补零。
- `derived.basket_count`:由加噪格求和推导,不是真实活跃篮数。
- `derived.frequent_itemsets` / `derived.rules`:仅由加噪计数推导的至多 3 项组合与
  **原精确规则**(同一 Fraction 精确分数、同一 count≥门槛、同一确定性排序;门槛参数随
  账本固定并在报告 `parameters` 回显)。推导篮数为 0 时两者均为空列表。
- `item_universe`、`source_watermark`、噪声机制说明与 scope。
- 报告与账本均不含真实计数、篮 id 或其他身份信息;源库不被修改。

## 事务、并发与恢复边界

账本启用 WAL 与 `synchronous=FULL`;每次 `publish` 以 `BEGIN IMMEDIATE` 开始,
存档写入与额度占用是同一事务:

1. 同 id 并发:写锁串行化,先到者在事务内插入存档并扣额,后到者只读到已存档行并重放,
   噪声不换、额度不双扣。
2. 不同 id 并发:事务串行提交,额度检查(`COUNT(*)` 已存档行数 < quota)与插入同事务,
   超额者整体回滚拒绝,任何 id 都不透支。
3. 提交前崩溃(含 kill -9、断电):未提交帧被 SQLite 回滚,无存档、无扣额;重试同一 id
   走新事务重新抽噪,不存在"半次发布"。
4. 提交后、输出落盘前崩溃或磁盘发布失败:存档已持久且额度已扣;重试同一 id 逐字节重发,
   不读源、不重抽、不另扣。输出经同目录临时文件 + `os.replace` 原子替换,符号链接拒绝,
   输出路径不得覆盖账本或源库。
5. 源库并发 `load`:发布在源库以 `mode=ro` 打开并在单一只读事务内完成全部读取,
   快照不会撕裂;发布不持源库写锁,不阻塞源库提交,也永远不写源库。

恢复边界即账本 SQLite 事务边界加一次原子文件替换;账本与源库是两个独立数据库文件,
单操作者多进程并发发布由账本写锁保证安全,不承诺跨机复制。
