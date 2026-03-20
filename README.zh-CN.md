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

## 许可证

MIT
