# bsc-block-delay

[中文](README.zh-CN.md)

Precise BSC (BNB Smart Chain) block propagation delay measurement, accounting for integer-second timestamp quantization.

## Features

- **Absolute delay measurement** — measures block creation → your node, not relative to a reference node
- **Quantization-aware** — separates real delay from integer-second timestamp noise
- **Per-validator breakdown** — delay statistics for each of the 21+ active validators
- **On-chain name resolution** — resolves validator addresses to human-readable names via StakeHub contract
- **Zero config** — only requires `websockets`, connects to localhost by default

## The Problem

BSC blocks carry **integer-second timestamps** but arrive every **~450ms** (post-Fermi hard fork). A naive `receive_time - timestamp` gives ~600ms average — but ~500ms of that is quantization noise, not actual delay.

```
naive "delay" ≈ 600ms = ~100ms real delay + ~500ms quantization artifact
```

## Methods

### Method of Moments (Aggregate)

The observable raw delay decomposes as `raw = D + frac(t)`, where `D` is the true propagation delay and `frac(t)` is the sub-second part of the block timestamp. Across many blocks from different validators, `frac(t)` is approximately Uniform(0,1), giving:

```
E[D] = E[raw] - 0.5
Var[D] = Var[raw] - 1/12
```

Simple, unbiased, and robust. 95% CI via CLT.

### Turn-Based Per-Validator

BSC uses **BEP-341** (turnLength=8): each validator produces 8 consecutive blocks at τ≈0.45s intervals. Within a turn, the fractional-second parts are **deterministic** — so the MoM assumption (random frac) doesn't hold per-validator. Instead, we use `min(raw)` within each turn as a tight upper bound on D, then subtract a small analytical correction (≈62ms).

Per-validator metrics:
- **MoM**: unbiased point estimate (works across turns)
- **BestMin**: tightest observed upper bound from any single turn
- **AvgMin-corr**: mean turn minimum minus E[min(frac)] correction

## BSC Consensus Parameters (2026)

| Parameter | Value |
|-----------|-------|
| Consensus | Parlia PoSA |
| Cabinet size | 21 |
| turnLength | 8 (BEP-341) |
| Block interval | ~450ms (Fermi hard fork) |
| Timestamps | Integer seconds (floor) |
| Total validators | ~50 |

## Requirements

- **Python** >= 3.7 (recommended 3.9+)
- **websockets** library

```bash
pip install websockets
```

No other dependencies. Validator name resolution uses hardcoded ABI selectors — no web3.py needed.

## Design Assumptions

This tool is designed to run **on the node itself**, accessing RPC/WebSocket via `localhost`. The measured delay (E[D]) reflects the end-to-end **block availability delay**:

```
E[D] = P2P propagation + block import (validation + tx execution + state commit)
```

This is the time from block creation to when the block becomes queryable via RPC — the metric that matters for most applications. The block import component is typically 20-70ms on modern hardware; the remainder is network propagation.

RPC network latency is **not** included in the measurement by design — the tool assumes local access. If you need to measure remote RPC latency, that is a separate concern.

## Usage

```bash
# Basic (connects to localhost)
python3 bsc_block_delay.py

# Custom endpoints
python3 bsc_block_delay.py --ws ws://your-node:8546 --rpc http://your-node:8545

# Auto-stop after 5 minutes
python3 bsc_block_delay.py --duration 300

# Auto-stop after 1000 blocks
python3 bsc_block_delay.py --blocks 1000

# Skip validator name resolution (faster startup)
python3 bsc_block_delay.py --no-names
```

Press **Ctrl+C** to stop and trigger analysis.

### Recommended Duration

| Duration | Coverage |
|----------|----------|
| 90s (min) | 1 full round (21×8 blocks) |
| 5min | ~4 rounds, ~32 blocks per validator |
| 15min+ | Stable per-validator stats |

## Output

1. **Real-time block feed** — each block with raw delay, receive time, inter-block gap, and validator name
2. **Aggregate delay** — MoM estimate with analytical 95% CI
3. **Block interval** — measured intra-turn median τ
4. **Per-validator table** — sorted by delay: block count, turn count, MoM, best minimum, corrected average minimum
5. **Turn minimum distribution** — P5/P50/P95 and global best across all full turns

### Sample Output

Real-time block feed:

```
       Block       Raw          Time       Gap  Validator           E[D]
  ──────────────────────────────────────────────────────────────────────
    87652304   112.7ms  08:35:05.112   389.2ms  Avengers        ~264.2ms
    87652305   572.3ms  08:35:05.572   459.6ms  Avengers        ~259.9ms
    87652306    1.035s  08:35:06.034   462.4ms  Avengers        ~266.0ms
    87652307   480.2ms  08:35:06.480   445.5ms  Avengers        ~259.8ms
    ...
```

Analysis report:

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

## Validator Name Resolution

Queries BSC **StakeHub** contract (`0x2002`) on-chain:

1. `getValidators()` → operator addresses
2. `getValidatorConsensusAddress(op)` → consensus address mapping
3. `getValidatorDescription(op)` → moniker (e.g., "BscScan", "NodeReal")

Falls back to truncated hex addresses on failure.

---

## Mempool Visibility Tool

`bsc_mempool_visibility.py` — measures what fraction of block transactions your node saw as pending before they were included.

### Why

Block delay tells you how fast blocks arrive. Mempool visibility tells you how fast **transactions** arrive — critical for MEV/HFT where you need to see pending txs before they're confirmed.

### How It Works

1. Subscribes to `newPendingTransactions` + `newHeads` on a single WebSocket
2. Records first-seen time for each pending tx hash
3. When a new block arrives, fetches its tx list via `eth_getBlockByNumber`
4. Compares: which block txs were already in the pending pool?

### Metrics

| Metric | Meaning |
|--------|---------|
| **Visibility Rate** | % of block txs seen in mempool beforehand |
| **Lead Time** | How many ms before block arrival the tx was first seen |
| **Surprise Rate** | % of block txs never seen in mempool (private relay / dark pool) |
| **Per-Validator** | Visibility breakdown by block producer |

### Usage

```bash
python3 bsc_mempool_visibility.py [--ws URL] [--rpc URL] [--duration SEC]
```

### Note

Some BSC nodes disable `newPendingTransactions` for performance. If your node doesn't support it, the tool warns and continues (visibility shows 0%). Check your geth `--txpool.*` flags.

## License

MIT
