#!/usr/bin/env python3
"""
BSC Mempool Visibility Measurement

Measures what fraction of block transactions your node saw as pending
before they were included in a block.

  visibility = (txs seen in mempool before block) / (total txs in block)
  lead_time  = block_receive_time - first_seen_time

A high visibility rate with long lead times means your node receives
pending transactions early — critical for HFT/MEV.

Usage:
  python3 bsc_mempool_visibility.py [--ws URL] [--rpc URL] [--duration SEC]
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
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Pending pool management
CLEANUP_INTERVAL_BLOCKS = 30  # ~13.5s at 0.45s/block
PENDING_TTL = 120.0           # drop unconfirmed entries after 120s


@dataclass
class BlockVisibility:
    number: int
    timestamp: int
    receive_time: float
    miner: str
    total_txs: int
    seen_txs: int
    surprise_txs: int
    lead_times: list = field(default_factory=list)

    @property
    def visibility(self):
        return self.seen_txs / self.total_txs if self.total_txs > 0 else 1.0

    @property
    def surprise_rate(self):
        return self.surprise_txs / self.total_txs if self.total_txs > 0 else 0.0

    @property
    def median_lead(self):
        if not self.lead_times:
            return None
        s = sorted(self.lead_times)
        n = len(s)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


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

    r = eth_call(STAKE_HUB, SEL_GET_VALS + "0" * 64 + "0" * 62 + "32")
    if "error" in r:
        return {}
    data = bytes.fromhex(r["result"][2:])
    off = int.from_bytes(data[0:32], "big")
    sd = data[off:]
    count = int.from_bytes(sd[0:32], "big")
    operators = ["0x" + sd[32 + i*32 + 12 : 32 + (i+1)*32].hex() for i in range(count)]

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


# ── RPC helpers ──

def rpc_get_block_txhashes(rpc_url, block_number):
    """Fetch tx hashes for a block. Returns list of lowercase hex hashes."""
    payload = json.dumps({
        "jsonrpc": "2.0", "method": "eth_getBlockByNumber",
        "params": [hex(block_number), False], "id": 1,
    }).encode()
    req = urllib.request.Request(
        rpc_url, data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=5).read())
        if "error" in resp or resp.get("result") is None:
            return []
        return [tx.lower() for tx in resp["result"].get("transactions", [])]
    except Exception:
        return []


# ── Pending pool management ──

def cleanup_pending_pool(pool, max_age=PENDING_TTL):
    cutoff = time.time() - max_age
    stale = [h for h, t in pool.items() if t < cutoff]
    for h in stale:
        del pool[h]
    return len(stale)


# ── Analysis ──

def analyze(blocks, names, pending_count_total, pending_pool_peak, pending_pool_size):
    n = len(blocks)
    if n < 5:
        print(f"\n  Insufficient data ({n} blocks, need >= 5)")
        return

    total_txs = sum(b.total_txs for b in blocks)
    total_seen = sum(b.seen_txs for b in blocks)
    total_surprise = sum(b.surprise_txs for b in blocks)
    dur = blocks[-1].receive_time - blocks[0].receive_time

    all_leads = []
    for b in blocks:
        all_leads.extend(b.lead_times)

    def name(addr):
        n = names.get(addr.lower(), addr[:8] + ".." + addr[-4:])
        return n[:14]

    W = 72
    if dur >= 60:
        dur_str = f"{dur/60:.0f}min {dur%60:.0f}s"
    else:
        dur_str = f"{dur:.0f}s"

    print(f"\n{'=' * W}")
    print(f"  BSC Mempool Visibility Report")
    print(f"  {n} blocks, {total_txs} transactions, {dur_str}")
    print(f"{'=' * W}")

    # Overall
    vis_pct = total_seen / total_txs * 100 if total_txs else 0
    sur_pct = total_surprise / total_txs * 100 if total_txs else 0
    print(f"\n  Overall Visibility")
    print(f"    Seen before block: {vis_pct:.1f}% ({total_seen} / {total_txs})")
    print(f"    Surprise (never seen): {sur_pct:.1f}% ({total_surprise})")

    # Lead time distribution
    if all_leads:
        print(f"\n  Lead Time Distribution ({len(all_leads)} visible txs)")
        print(f"    P5={fmt(pct(all_leads, 5))}  P25={fmt(pct(all_leads, 25))}  "
              f"P50={fmt(pct(all_leads, 50))}  P95={fmt(pct(all_leads, 95))}")

    # Per-validator
    val_blocks = defaultdict(list)
    for b in blocks:
        val_blocks[b.miner].append(b)

    print(f"\n  Per-Validator Visibility ({len(val_blocks)} validators)")
    print(f"    {'Validator':<14} {'Blk':>4} {'Txs':>5} "
          f"{'Vis%':>6} {'Surprise%':>9} {'Lead(P50)':>9}")
    print(f"    {'─' * 55}")

    for v in sorted(val_blocks, key=lambda x: (
        sum(b.seen_txs for b in val_blocks[x]) /
        max(1, sum(b.total_txs for b in val_blocks[x]))
    ), reverse=True):
        vblocks = val_blocks[v]
        v_txs = sum(b.total_txs for b in vblocks)
        v_seen = sum(b.seen_txs for b in vblocks)
        v_vis = v_seen / v_txs * 100 if v_txs else 0
        v_sur = 100 - v_vis
        v_leads = []
        for b in vblocks:
            v_leads.extend(b.lead_times)
        v_lead_str = fmt(median(v_leads)) if v_leads else "  -"
        print(f"    {name(v):<14} {len(vblocks):>4} {v_txs:>5} "
              f"{v_vis:>5.1f}% {v_sur:>8.1f}% {v_lead_str:>9}")

    # Mempool stats
    elapsed = dur if dur > 0 else 1
    avg_rate = pending_count_total / elapsed

    print(f"\n  Mempool Stats")
    print(f"    Peak pending pool size: {pending_pool_peak}")
    print(f"    Current pending pool size: {pending_pool_size}")
    print(f"    Total pending txs received: {pending_count_total}")
    print(f"    Avg new pending txs/sec: {avg_rate:.1f}")

    print(f"\n{'=' * W}\n")


# ── Signal handling ──

running = True
all_blocks = []
all_names = {}
pending_pool = {}
pending_count_total = 0
pending_pool_peak = 0


def handle_signal(sig, frame):
    global running
    running = False
    print("\n\n  Collection stopped. Analyzing...")
    analyze(all_blocks, all_names, pending_count_total,
            pending_pool_peak, len(pending_pool))
    sys.exit(0)


signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


# ── Data collection ──

async def collect(ws_url, rpc_url, duration=None):
    import websockets

    global all_blocks, all_names, pending_pool
    global pending_count_total, pending_pool_peak

    print(f"BSC Mempool Visibility Measurement")
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
    blocks_since_cleanup = 0

    async with websockets.connect(ws_url, ping_interval=20, ping_timeout=30) as ws:
        # Subscribe to newPendingTransactions
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "method": "eth_subscribe",
            "params": ["newPendingTransactions"], "id": 1,
        }))
        resp1 = json.loads(await ws.recv())
        if "error" in resp1:
            print(f"  WARNING: newPendingTransactions failed: {resp1['error']}")
            print(f"  Your node may not support pending tx subscriptions.")
            print(f"  Continuing with newHeads only (visibility will show 0%).\n")
            pending_sub_id = None
        else:
            pending_sub_id = resp1.get("result")
            print(f"  Subscribed to pending transactions")

        # Subscribe to newHeads
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "method": "eth_subscribe",
            "params": ["newHeads"], "id": 2,
        }))
        resp2 = json.loads(await ws.recv())
        if "error" in resp2:
            print(f"  newHeads subscribe failed: {resp2['error']}")
            return
        heads_sub_id = resp2.get("result")
        print(f"  Subscribed to new blocks\n")

        print(f"  {'Block#':>10}  {'Txs':>4}  {'Seen':>4}  {'Vis%':>5}  "
              f"{'Lead':>8}  {'Surp':>5}  {'Validator':<14}  {'cum':>6}")
        print(f"  {'─' * 72}")

        while running:
            if duration and time.time() - start > duration:
                print(f"\n  Duration reached. Analyzing...")
                analyze(all_blocks, all_names, pending_count_total,
                        pending_pool_peak, len(pending_pool))
                return

            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            except asyncio.TimeoutError:
                continue
            except Exception:
                print("  Connection lost.")
                break

            if "params" not in msg:
                continue

            sub_id = msg["params"].get("subscription")

            # ── Pending transaction ──
            if sub_id == pending_sub_id:
                tx_hash = msg["params"]["result"]
                if isinstance(tx_hash, str):
                    tx_hash = tx_hash.lower()
                    if tx_hash not in pending_pool:
                        pending_pool[tx_hash] = time.time()
                        pending_count_total += 1
                        pool_size = len(pending_pool)
                        if pool_size > pending_pool_peak:
                            pending_pool_peak = pool_size
                continue

            # ── New block ──
            if sub_id == heads_sub_id:
                h = msg["params"]["result"]
                num = int(h["number"], 16)
                miner = h.get("miner", "?").lower()
                block_time = time.time()

                # Fetch block's tx hashes
                tx_hashes = rpc_get_block_txhashes(rpc_url, num)
                total = len(tx_hashes)

                # Compare against pending pool
                seen = 0
                lead_times = []
                for tx in tx_hashes:
                    if tx in pending_pool:
                        seen += 1
                        lead_times.append(block_time - pending_pool[tx])
                        del pending_pool[tx]

                surprise = total - seen
                bv = BlockVisibility(
                    number=num, timestamp=int(h["timestamp"], 16),
                    receive_time=block_time, miner=miner,
                    total_txs=total, seen_txs=seen,
                    surprise_txs=surprise, lead_times=lead_times,
                )
                all_blocks.append(bv)

                # Print real-time line
                vis_str = f"{bv.visibility * 100:.1f}%" if total > 0 else "  -"
                lead_str = fmt(bv.median_lead) if bv.median_lead is not None else "  -"
                cum_seen = sum(b.seen_txs for b in all_blocks)
                cum_total = sum(b.total_txs for b in all_blocks)
                cum_str = f"{cum_seen / cum_total * 100:.1f}%" if cum_total > 0 else "  -"

                print(f"  {num:>10}  {total:>4}  {seen:>4}  "
                      f"{vis_str:>5}  {lead_str:>8}  {surprise:>5}  "
                      f"{name(miner):<14}  {cum_str:>6}")

                # Periodic cleanup
                blocks_since_cleanup += 1
                if blocks_since_cleanup >= CLEANUP_INTERVAL_BLOCKS:
                    cleanup_pending_pool(pending_pool)
                    blocks_since_cleanup = 0


async def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("pip install websockets")
        sys.exit(1)

    p = argparse.ArgumentParser(description="BSC mempool visibility measurement")
    p.add_argument("--ws", default="ws://localhost:8546")
    p.add_argument("--rpc", default="http://localhost:8545")
    p.add_argument("--duration", type=float, default=None,
                   help="Auto-stop after N seconds")
    p.add_argument("--no-names", action="store_true",
                   help="Skip validator name resolution")
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
