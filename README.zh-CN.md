# bsc-block-delay

[English](README.md)

BSC (BNB Smart Chain) 区块传播延迟精确测量工具，消除整数秒时间戳量化噪声的影响。

## 功能

- **绝对延迟测量** — 测量从区块创建到你的节点的延迟，而非与参考节点的相对延迟
- **量化感知** — 分离真实延迟与整数秒时间戳噪声
- **单验证者分析** — 每个验证者的独立延迟统计
- **链上名称解析** — 通过 StakeHub 合约将验证者地址解析为可读名称
- **零配置** — 仅需 `websockets` 依赖，默认连接 localhost

## 问题背景

BSC 区块携带**整数秒时间戳**，但实际出块间隔约 **450ms**（Fermi 硬分叉后）。简单的 `收到时间 - 时间戳` 得到约 600ms，其中约 500ms 是量化噪声而非真实延迟。

```
朴素"延迟" ≈ 600ms = ~100ms 真实延迟 + ~500ms 量化伪影
```

## 方法

### 矩估计法（整体）

观测到的原始延迟分解为 `raw = D + frac(t)`，其中 `D` 是真实传播延迟，`frac(t)` 是区块时间戳的亚秒部分。在不同验证者的大量区块上，`frac(t)` 近似均匀分布，因此：

```
E[D] = E[raw] - 0.5
Var[D] = Var[raw] - 1/12
```

简单、无偏、鲁棒，通过中心极限定理给出 95% 置信区间。

### 基于轮次的单验证者分析

BSC 采用 **BEP-341**（turnLength=8）：每个验证者以 τ≈0.45s 间隔连续出 8 个块。同一轮次内小数秒部分是**确定性的**，矩估计的随机假设不成立。因此使用轮次内 `min(raw)` 作为延迟 D 的紧上界，再减去解析校正项（约 62ms）。

单验证者指标：
- **MoM**：跨轮次无偏点估计
- **BestMin**：单轮最紧观测上界
- **AvgMin-corr**：平均轮次最小值减校正项

## BSC 共识参数（2026）

| 参数 | 值 |
|------|-----|
| 共识 | Parlia PoSA |
| 活跃验证者 | 21 |
| 轮次长度 | 8 (BEP-341) |
| 出块间隔 | ~450ms (Fermi 硬分叉) |
| 时间戳 | 整数秒（向下取整） |
| 注册验证者 | ~50 |

## 环境要求

- **Python** >= 3.7（推荐 3.9+）
- **websockets** 库

```bash
pip install websockets
```

无需其他依赖。验证者名称解析使用硬编码 ABI 选择器，无需 web3.py。

## 设计假设

本工具设计为**直接在节点上运行**，通过 `localhost` 访问 RPC/WebSocket。测量的延迟 (E[D]) 反映端到端的**区块可用延迟**：

```
E[D] = P2P 传播 + 区块导入（验证 + 交易执行 + 状态提交）
```

即从区块创建到该区块可通过 RPC 查询的时间——这是大多数应用关心的指标。其中区块导入部分在现代硬件上通常为 20-70ms，其余为网络传播延迟。

RPC 网络延迟**不包含**在测量结果中——工具假设本地访问。如需测量远程 RPC 延迟，那是另一个独立的问题。

## 使用

```bash
# 基本用法（连接本机）
python3 bsc_block_delay.py

# 自定义节点
python3 bsc_block_delay.py --ws ws://your-node:8546 --rpc http://your-node:8545

# 5 分钟后自动停止
python3 bsc_block_delay.py --duration 300

# 采集 1000 个块后自动停止
python3 bsc_block_delay.py --blocks 1000

# 跳过名称解析（启动更快）
python3 bsc_block_delay.py --no-names
```

按 **Ctrl+C** 停止并触发分析。

### 建议采集时长

| 时长 | 覆盖 |
|------|------|
| 90s（最低） | 1 完整轮次（21×8 块） |
| 5min | 约 4 轮，每验证者约 32 块 |
| 15min+ | 稳定的单验证者统计 |

## 输出

1. **实时区块流** — 显示原始延迟、接收时间、块间间隔、验证者名称
2. **整体延迟** — 矩估计延迟及 95% 置信区间
3. **出块间隔** — 实测轮次内出块间隔中位数
4. **验证者表** — 按延迟排序：块数、轮次数、MoM、最佳最小值、校正平均最小值
5. **轮次最小值分布** — P5/P50/P95 及全局最佳值

### 示例输出

实时区块流：

```
       Block       Raw          Time       Gap  Validator           E[D]
  ──────────────────────────────────────────────────────────────────────
    87652304   112.7ms  08:35:05.112   389.2ms  Avengers        ~264.2ms
    87652305   572.3ms  08:35:05.572   459.6ms  Avengers        ~259.9ms
    87652306    1.035s  08:35:06.034   462.4ms  Avengers        ~266.0ms
    87652307   480.2ms  08:35:06.480   445.5ms  Avengers        ~259.8ms
    ...
```

分析报告：

```
========================================================================
  BSC Block Propagation Delay Report
  268 blocks (263 clean), 120.211s, 31 full turns
========================================================================

  Aggregate Delay (Method of Moments)
    E[D] = E[raw] - 0.5 = 628.2ms - 500ms = 128.2ms
    95% CI: [104.0ms, 152.5ms]
    Std[D] = 200.7ms

  Block Interval: τ = 450.0ms (intra-turn median)

  Per-Validator Delay (21 validators)
    Validator       Blk Trn      MoM  BestMin AvgMin-corr
    ────────────────────────────────────────────────────────
    Feynman           8   1   14.4ms   64.5ms       2.5ms
    BscScan           8   1   19.5ms  115.9ms      53.9ms
    The48Club         8   1   32.4ms  124.4ms      62.4ms
    ...

  Turn Minimums (across 31 full turns)
    P5=70.2ms  P50=148.1ms  P95=322.8ms
    Global best: 63.1ms → fastest validator ≤ 63.1ms delay
========================================================================
```

## 验证者名称解析

通过链上查询 BSC **StakeHub** 合约（`0x2002`）：

1. `getValidators()` → 获取运营者地址
2. `getValidatorConsensusAddress(op)` → 共识地址映射
3. `getValidatorDescription(op)` → 可读名称（如 "BscScan"、"NodeReal"）

查询失败时回退到截断的十六进制地址。

---

## Mempool 可见率工具

`bsc_mempool_visibility.py` — 测量你的节点在区块打包前能看到多少比例的 pending 交易。

### 为什么需要

区块延迟告诉你区块到达有多快。Mempool 可见率告诉你**交易**到达有多快——对 MEV/HFT 来说，能在出块前看到 pending 交易至关重要。

### 原理

1. 在同一个 WebSocket 上订阅 `newPendingTransactions`（交易哈希）和 `newHeads`（新区块）
2. 记录每个 pending 交易的首次出现时间
3. 新区块到达时，通过 `eth_getBlockByNumber` 获取该区块所有交易哈希
4. 对比：哪些区块交易之前已经在 pending 池中见过？

### 指标

| 指标 | 含义 |
|------|------|
| **可见率** | 区块交易中，出块前就在 mempool 中看到的比例 |
| **提前量** | 可见交易在区块到达前多少 ms 被首次看到 |
| **惊喜率** | 从未在 mempool 中出现、直接随区块到达的交易比例（私有通道/暗池） |
| **验证者分布** | 按出块者分组的可见率统计 |

### 示例输出

实时区块流：

```
      Block#   Txs  Seen   Vis%      Lead   Surp  Validator          cum
  ──────────────────────────────────────────────────────────────────────
    87661424   133   111  83.5%   243.9ms     22  Defibit          68.0%
    87661425   116    90  77.6%   355.5ms     26  Defibit          69.2%
    87661426   107   100  93.5%   320.0ms      7  Defibit          71.8%
    ...
```

分析报告：

```
========================================================================
  BSC Mempool Visibility Report
  266 blocks, 28977 transactions, 2min 59s
========================================================================

  Overall Visibility
    Seen before block: 74.2% (21499 / 28977)
    Surprise (never seen): 25.8% (7478)

  Lead Time Distribution (21499 visible txs)
    P5=19.8ms  P25=61.8ms  P50=300.4ms  P95=584.3ms

  Per-Validator Visibility (21 validators)
    Validator       Blk   Txs   Vis% Surprise% Lead(P50)
    ───────────────────────────────────────────────────────
    Figment          16  1791  82.2%     17.8%   320.0ms
    MathW            16  1910  79.8%     20.2%   323.2ms
    The48Club        16  1703  64.7%     35.3%   285.3ms
    ...
========================================================================
```

### 使用

```bash
python3 bsc_mempool_visibility.py [--ws URL] [--rpc URL] [--duration SEC]
```

### 注意

部分 BSC 节点为了性能会关闭 `newPendingTransactions`。如果你的节点不支持，工具会警告并继续运行（可见率显示 0%）。检查 geth 的 `--txpool.*` 配置。

---

## Late Pending 验证工具

`verify_late_pending.py` — 验证你的节点是否会对**已经打包进区块**的交易发出 `newPendingTransactions` 通知。

### 为什么需要

在测量 mempool 可见率时，一个关键假设是 pending 通知在区块**之前**到达。如果节点有时在区块**之后**才发出 pending 事件（"late pending"），会虚增可见率——交易虽然技术上被"看到"了，但实际上是在出块之后。

这个工具量化 late pending 的发生频率和延迟。

### 原理

1. 在同一个 WebSocket 上订阅 `newPendingTransactions` 和 `newHeads`
2. 新区块到达时，获取其交易哈希并存入已确认集合
3. 收到 pending 交易通知时，检查该交易是否已在已确认集合中
4. 如果是，记录为 "late pending"，并计算 lag（距区块到达的时间差）

### 使用

```bash
python3 verify_late_pending.py [--ws URL] [--rpc URL] [--duration SEC]
```

### 结果解读

- **Late 比率 ~0%**：pending 通知可靠——交易确实在区块之前被看到
- **Late 比率 >0%**：部分 pending 通知在区块之后才到达，说明 mempool 可见率测量会略微高估真实的出块前可见率
- **Lag 值**：pending 通知在区块之后多久才到达（如果触发了 geth TxPool 的竞态窗口，通常在亚 100ms 级别）

## 许可证

MIT
