# data-qd-129 · 购物篮关联规则挖掘

Bounded exact frequent itemsets and association rule metrics。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。

## 安装与演示

Python >=3.11，运行和测试仅用标准库；先确认 `python3 --version`。
每仓建立独立环境，构建工具锁版本。首次安装无公共缓存时可能联网；业务运行不访问网络。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/python -m pip check
.venv/bin/basket-rules --source fixtures/input.json --output run/report.json
.venv/bin/python -m unittest discover -s tests -v
```

本地已有wheel时，安装构建工具可加 `--no-index --find-links <公共wheel缓存目录>`。
修改源码后重新安装。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的run/。

## 滚动窗口统计链（新命令，旧命令保留）

持续修订的购物篮走 `basket-rules-roll`：SQLite 库存固定窗口秒数与原挖掘参数，
JSONL 批 `{id,rev,time,items}` 按非递减水位单事务提交（版本/水位/篮状态/组合计数），
低版本忽略、同版本同内容幂等、同版本不同内容整批失败；`items=null` 删除，高版本可改
商品、时间或恢复。仅 `w-window<time<=w` 的最新非删除篮参与，空篮计分母，活跃上限10万篮。
报告只读持久组合计数，沿用精确分数、门槛与排序；无活跃篮输出空列表。
契约、索引策略与恢复边界见 [docs/rolling.md](docs/rolling.md)。

```sh
.venv/bin/basket-rules-roll init --db run/roll.db --window-seconds 100 \
    --min-support-count 2 --max-itemset-size 3 --min-confidence 1 2
.venv/bin/basket-rules-roll load --db run/roll.db --watermark 1000 --source run/batch.jsonl
.venv/bin/basket-rules-roll report --db run/roll.db --output run/rolling-report.json
```

src/basket_rules/model.py 为业务模型，相关算法模块独立组织；common.py 提供受限JSON及报告发布，
cli.py 衔接数据链路。fixtures 为明确自制样例，代码与样例采用MIT许可。

## 公开统计发布链（第三命令，旧命令保留）

对外发布走 `basket-rules-publish`：独立 SQLite 账本在 `init` 时固定滚动源库、公开商品全集
（≤12）和正整数发布额度。`publish` 在源库同一只读事务取参数与活跃篮，生成含空篮的全部
篮型直方图，逐格独立加 `Z=G1-G2`（`P(G=k)=2^(-k-1)`，系统安全随机源，截负为0），
仅输出加噪直方图及由其推导的篮数、至多3项组合与原精确规则，不泄露真实计数或身份。
新 id 在账本事务内存完整报告字节并扣一次额度后才发布；旧 id 逐字节重发存档、可换输出路径、
不读源不重抽不扣额。契约、并发与崩溃恢复边界见 [docs/publishing.md](docs/publishing.md)。

```sh
.venv/bin/basket-rules-publish init --ledger run/pub.db --source run/roll.db \
    --universe run/universe.json --quota 5
.venv/bin/basket-rules-publish publish --ledger run/pub.db --request-id req-1 --output run/pub-1.json
```

## 商品别名归一链（新命令，旧命令保留）

输入商品标识不一致时先走 `basket-rules-normalize`：按 `--as-of YYYY-MM-DD` 只取当日生效的
别名映射，把既有快照的篮内 ID 沿链解析到规范 SKU，输出与 `basket-rules --source` **同结构**的
快照。映射为 JSONL `{alias,target,valid_from,valid_to}`，有效期左闭右开、空止日期无期限；
当日每个别名须恰有一条出边、不得与规范 SKU 重名、链须终止于规范 SKU，重叠/悬空/环使整次失败。
多 ID 归一到同一 SKU 只保留一项，篮 ID、空篮与挖掘参数原样保留。别名文件 ≤64MB/50 万行，
链解析迭代记忆化，长链不受递归深度限制。输出原子替换；stdout 回执记录日期、三份输入原字节
SHA256 与解析/折叠计数。契约、复杂度与失败边界见 [docs/normalize.md](docs/normalize.md)。

```sh
.venv/bin/basket-rules-normalize --source fixtures/normalize/input.json \
    --canonical fixtures/normalize/canonical.json --aliases fixtures/normalize/aliases.jsonl \
    --as-of 2026-07-01 --output run/normalize-jul.json
.venv/bin/basket-rules --source run/normalize-jul.json --output run/normalize-jul-report.json
```

## 业务与样例结果

4篮中bread/milk共同2篮；双向confidence2/3、lift8/9，支持度1/2，精确分数输出。
