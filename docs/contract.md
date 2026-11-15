# Bounded basket association v1

面向商品组合分析人员，从完整购物篮快照挖掘精确频繁项集和关联规则。baskets每项唯一id/items，
篮内item唯一（重复拒绝，不把数量当频次），空篮合法且计入分母；1..1000篮、全体<=12种商品。
max_itemset_size=1..3，min_support_count=1..1000，min_confidence为0..1整数分数。

枚举每篮所有不超过max_size的组合，频次按包含该组合的篮数计，不是出现次数。
频繁项集count>=门槛；每项集枚举全部非空真子集A→剩余B，confidence=count(A∪B)/count(A)，
support=count(A∪B)/N，lift=confidence/(count(B)/N)，leverage=support-support(A)*support(B)。
全部精确Fraction文本，不做浮点阈值比较；非频繁项集不产规则，单商品不产规则。

样例4篮，bread/milk各3篮、共同2篮，support1/2，双向confidence2/3、lift8/9、leverage-1/16。
规则排序确定；输入篮/商品排列不影响结果。输出全部规则，不将lift大于1解释为因果或推荐收益。
当前无SKU别名、购物数量、序列规则、FP-growth大规模优化、统计显著性或线上推荐服务。

持续修订输入的SQLite滚动窗口链（`basket-rules-roll`，版本化批、水位、恢复边界与索引策略）
见 [rolling.md](rolling.md)；其评分、门槛与排序复用本契约，`summarize` 为两链共享实现。

对外公开的加噪直方图发布链（`basket-rules-publish`，独立账本、发布额度、安全随机几何噪声、
存档重发与恢复边界）见 [publishing.md](publishing.md)；其推导组合与规则同样复用 `summarize`，
但只作用于加噪计数，真实计数与篮身份永不输出。
