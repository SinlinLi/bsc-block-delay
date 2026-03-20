#!/usr/bin/env python3
"""
BSC Block Propagation Delay Measurement

Measures true propagation delay from BSC validators to your node,
separating real delay from integer-second timestamp quantization noise.

  raw = D + frac(t)                 # observable = delay + quantization
  E[D] = E[raw] - 0.5              # because frac(t) ~ Uniform(0,1)
  min(raw in turn) ≈ D + ~0.06     # 8 blocks/turn covers most of [0,1)

Usage:
  python3 bsc_block_delay.py [--ws URL] [--rpc URL] [--duration SEC]
"""

import argparse
import asyncio
import json
import math
import signal
import sys
import time
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

# BSC consensus parameters (post-Fermi, 2026)
TURN_LENGTH = 8
BLOCK_INTERVAL = 0.45
# E[min(frac(f + k*0.45) for k in 0..7)] ≈ 0.062, varies <0.01 across τ
MIN_FRAC_CORRECTION = 0.062


@dataclass
class Block:
    number: int
    timestamp: int
    receive_time: float
    miner: str
    raw_delay: float


@dataclass
class Turn:
    validator: str
    blocks: list

    @property
    def min_raw(self):
        return min(b.raw_delay for b in self.blocks)

    @property
    def intra_intervals(self):
        return [
            self.blocks[i + 1].receive_time - self.blocks[i].receive_time
            for i in range(len(self.blocks) - 1)
            if self.blocks[i + 1].number == self.blocks[i].number + 1
        ]


# ── Formatting ──

def fmt(d):
    if abs(d) < 0.001:
        return f"{d*1e6:.0f}us"
    if abs(d) < 1:
        return f"{d*1000:.1f}ms"
    return f"{d:.3f}s"


def median(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def pct(xs, p):
    s = sorted(xs)
    k = (len(s) - 1) * p / 100
    f, c = math.floor(k), math.ceil(k)
    return s[f] if f == c else s[f] * (c - k) + s[c] * (k - f)


# ── Validator name resolution ──

def resolve_validator_names(rpc_url):
    """Query StakeHub (0x2002) for consensus_address → moniker mapping."""
    # ABI selectors (hardcoded to avoid pycryptodome dependency)
    SEL_GET_VALS = "0xbff02e20"
    SEL_GET_DESC = "0xa43569b3"
    SEL_GET_CONS = "0x059ddd22"
    STAKE_HUB = "0x0000000000000000000000000000000000002002"

    def eth_call(to, data):
        payload = json.dumps({
            "jsonrpc": "2.0", "method": "eth_call",
            "params": [{"to": to, "data": data}, "latest"], "id": 1,
        }).encode()
        req = urllib.request.Request(
            rpc_url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        return json.loads(urllib.request.urlopen(req, timeout=5).read())

    # Get operator addresses
    r = eth_call(STAKE_HUB, SEL_GET_VALS + "0" * 64 + "0" * 62 + "32")
    if "error" in r:
        return {}
    data = bytes.fromhex(r["result"][2:])
    off = int.from_bytes(data[0:32], "big")
    sd = data[off:]
    count = int.from_bytes(sd[0:32], "big")
    operators = ["0x" + sd[32 + i*32 + 12 : 32 + (i+1)*32].hex() for i in range(count)]

    # Map operator → (consensus_addr, moniker)
    result = {}
    for op in operators:
        pad = op[2:].zfill(64)

        r1 = eth_call(STAKE_HUB, SEL_GET_CONS + pad)
        if "error" in r1 or len(r1.get("result", "")) <= 10:
            continue
        consensus = "0x" + bytes.fromhex(r1["result"][2:])[12:32].hex()

        r2 = eth_call(STAKE_HUB, SEL_GET_DESC + pad)
        moniker = consensus[:10] + ".."
        if "error" not in r2 and len(r2.get("result", "")) > 10:
            db = bytes.fromhex(r2["result"][2:])
            s_off = int.from_bytes(db[0:32], "big")
            m_off = int.from_bytes(db[s_off:s_off+32], "big")
            a = s_off + m_off
            slen = int.from_bytes(db[a:a+32], "big")
            if 0 < slen < 200:
                moniker = db[a+32:a+32+slen].decode("utf-8", errors="replace")

        result[consensus.lower()] = moniker
    return result


# ── Turn detection ──

def detect_turns(blocks):
    if not blocks:
        return []
    turns, cur = [], [blocks[0]]
    for i in range(1, len(blocks)):
        b, prev = blocks[i], blocks[i-1]
        if b.miner == prev.miner and b.number == prev.number + 1:
            cur.append(b)
        else:
            turns.append(Turn(cur[0].miner, cur))
            cur = [b]
    turns.append(Turn(cur[0].miner, cur))
    return turns


# ── Analysis ──

def analyze(blocks, names):
    n = len(blocks)
    if n < 30:
        print(f"\n  Insufficient data ({n} blocks, need >= 30)")
        return

    raws = [b.raw_delay for b in blocks if 0 < b.raw_delay < 3.0]
    n_clean = len(raws)
    n_outliers = n - n_clean
    if n_clean < 30:
        print(f"\n  Insufficient clean data ({n_clean} blocks after filtering)")
        return

    # Turn detection
    turns = detect_turns(blocks)
    clean_turns = [t for t in turns if all(0 < b.raw_delay < 3.0 for b in t.blocks)]
    full_turns = [t for t in clean_turns if len(t.blocks) == TURN_LENGTH]

    # Block interval (intra-turn only, not affected by delay)
    intervals = []
    for t in clean_turns:
        intervals.extend(iv for iv in t.intra_intervals if 0.1 < iv < 2.0)
    tau = median(intervals) if intervals else BLOCK_INTERVAL

    # ── Aggregate: Method of Moments ──
    mean_raw = sum(raws) / n_clean
    mean_D = mean_raw - 0.5
    var_D = max(0, sum((r - mean_raw)**2 for r in raws) / (n_clean - 1) - 1/12)
    std_D = math.sqrt(var_D)
    # Analytical 95% CI (CLT): mean ± 1.96 * std / sqrt(n)
    se = math.sqrt(var_D / n_clean) if var_D > 0 else std_D / math.sqrt(n_clean)
    ci_lo, ci_hi = mean_D - 1.96 * se, mean_D + 1.96 * se

    # ── Per-validator ──
    val_turns = defaultdict(list)
    for t in clean_turns:
        val_turns[t.validator].append(t)

    val_stats = {}
    for v, vturns in val_turns.items():
        v_raws = [b.raw_delay for t in vturns for b in t.blocks]
        full = [t for t in vturns if len(t.blocks) == TURN_LENGTH]
        mins = [t.min_raw for t in full] if full else [t.min_raw for t in vturns]

        val_stats[v] = {
            "mom": sum(v_raws) / len(v_raws) - 0.5,
            "best_min": min(mins),
            "avg_min": sum(mins) / len(mins),
            "n_blocks": len(v_raws),
            "n_turns": len(vturns),
        }

    def name(addr):
        n = names.get(addr.lower(), addr[:8] + ".." + addr[-4:])
        return n[:14]

    # ── Report ──
    W = 72
    dur = blocks[-1].receive_time - blocks[0].receive_time

    print(f"\n{'=' * W}")
    print(f"  BSC Block Propagation Delay Report")
    print(f"  {n} blocks ({n_clean} clean), {fmt(dur)}, "
          f"{len(full_turns)} full turns")
    if n_outliers:
        print(f"  Filtered {n_outliers} outliers (raw > 3s)")
    print(f"{'=' * W}")

    print(f"\n  Aggregate Delay (Method of Moments)")
    print(f"    E[D] = E[raw] - 0.5 = {fmt(mean_raw)} - 500ms = {fmt(mean_D)}")
    print(f"    95% CI: [{fmt(ci_lo)}, {fmt(ci_hi)}]")
    print(f"    Std[D] = {fmt(std_D)}")

    print(f"\n  Block Interval: τ = {fmt(tau)} (intra-turn median)")

    print(f"\n  Per-Validator Delay ({len(val_stats)} validators)")
    print(f"    {'Validator':<14} {'Blk':>4} {'Trn':>3} "
          f"{'MoM':>8} {'BestMin':>8} {'AvgMin-corr':>11}")
    print(f"    {'─' * 56}")

    for v in sorted(val_stats, key=lambda x: val_stats[x]["mom"]):
        s = val_stats[v]
        corrected = max(0, s["avg_min"] - MIN_FRAC_CORRECTION)
        print(f"    {name(v):<14} {s['n_blocks']:>4} {s['n_turns']:>3} "
              f"{fmt(s['mom']):>8} {fmt(s['best_min']):>8} "
              f"{fmt(corrected):>11}")

    # Turn minimum distribution (direct, no deconvolution needed)
    if len(full_turns) >= 5:
        tmins = sorted(t.min_raw for t in full_turns)
        print(f"\n  Turn Minimums (across {len(full_turns)} full turns)")
        print(f"    P5={fmt(pct(tmins, 5))}  P50={fmt(pct(tmins, 50))}  "
              f"P95={fmt(pct(tmins, 95))}")
        print(f"    Global best: {fmt(tmins[0])} "
              f"→ fastest validator ≤ {fmt(tmins[0])} delay")

    print(f"\n{'=' * W}\n")


# ── Signal handling ──

running = True
all_blocks = []
all_names = {}


def handle_signal(sig, frame):
    global running
    running = False
    print("\n\n  Collection stopped. Analyzing...")
    analyze(all_blocks, all_names)
    sys.exit(0)


signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


# ── Data collection ──

async def collect(ws_url, rpc_url, duration=None):
    import websockets

    global all_blocks, all_names

    print(f"BSC Block Delay Measurement")
    print(f"WS: {ws_url}   RPC: {rpc_url}")

    print("Resolving validators...", end=" ", flush=True)
    try:
        all_names = resolve_validator_names(rpc_url)
        print(f"OK ({len(all_names)})")
    except Exception as e:
        print(f"failed ({e})")

    def name(addr):
        n = all_names.get(addr.lower(), addr[:8] + ".." + addr[-4:])
        return n[:14]

    if duration:
        print(f"Duration: {duration:.0f}s")
    print("Ctrl+C to stop\n")

    start = time.time()

    async with websockets.connect(ws_url, ping_interval=20, ping_timeout=30) as ws:
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "method": "eth_subscribe",
            "params": ["newHeads"], "id": 1,
        }))
        resp = json.loads(await ws.recv())
        if "error" in resp:
            print(f"Subscribe failed: {resp['error']}")
            return

        print(f"  {'Block':>10}  {'Raw':>8}  {'Time':>12}  "
              f"{'Gap':>8}  {'Validator':<14}  {'E[D]':>8}")
        print(f"  {'─' * 70}")

        while running:
            if duration and time.time() - start > duration:
                print(f"\n  Duration reached. Analyzing...")
                analyze(all_blocks, all_names)
                return

            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            except asyncio.TimeoutError:
                continue
            except Exception:
                print("  Connection lost")
                break

            if "params" not in msg:
                continue

            h = msg["params"]["result"]
            num = int(h["number"], 16)
            ts = int(h["timestamp"], 16)
            rt = time.time()
            miner = h.get("miner", "?").lower()
            raw = rt - ts

            all_blocks.append(Block(num, ts, rt, miner, raw))
            cnt = len(all_blocks)

            gap = ""
            if cnt > 1 and all_blocks[-2].number == num - 1:
                gap = f"{fmt(rt - all_blocks[-2].receive_time):>8}"

            rt_s = time.strftime("%H:%M:%S", time.localtime(rt)) + f".{int(rt*1000)%1000:03d}"

            est = ""
            if cnt >= 20:
                cl = [b.raw_delay for b in all_blocks if 0 < b.raw_delay < 3]
                if cl:
                    est = f"  ~{fmt(sum(cl)/len(cl) - 0.5)}"

            print(f"  {num:>10}  {fmt(raw):>8}  {rt_s}  "
                  f"{gap:>8}  {name(miner):<14}{est}")


async def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("pip install websockets")
        sys.exit(1)

    p = argparse.ArgumentParser(description="BSC block propagation delay")
    p.add_argument("--ws", default="ws://localhost:8546")
    p.add_argument("--rpc", default="http://localhost:8545")
    p.add_argument("--duration", type=float, default=None,
                   help="Auto-stop after N seconds")
    p.add_argument("ws_pos", nargs="?", default=None, help=argparse.SUPPRESS)
    p.add_argument("rpc_pos", nargs="?", default=None, help=argparse.SUPPRESS)
    args = p.parse_args()

    ws = args.ws_pos or args.ws
    rpc = args.rpc_pos or args.rpc

    while running:
        try:
            await collect(ws, rpc, args.duration)
            break
        except Exception as e:
            print(f"Error: {e}")
            if running:
                print("Reconnecting in 5s...")
                await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
