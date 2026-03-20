#!/usr/bin/env python3
"""
Verify: do pending tx notifications arrive after the tx is already in a block?

Keeps a set of recently confirmed tx hashes. When a pending tx notification
arrives, checks if that tx was already confirmed. If so, it's a "late pending".

Usage:
  python3 verify_late_pending.py [--ws URL] [--rpc URL] [--duration SEC]
"""

import argparse
import asyncio
import json
import signal
import sys
import time
import urllib.request

running = True
confirmed_txs = {}   # tx_hash -> (block_number, block_receive_time)
late_pendings = []    # (tx_hash, block_number, block_time, pending_time, lag)
seen_pending = set()  # deduplication of pending tx hashes
pending_total = 0
block_total = 0

MAX_CONFIRMED_AGE = 60.0  # keep confirmed txs for 60s


def signal_handler(sig, frame):
    global running
    running = False


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def rpc_get_block_txhashes(rpc_url, block_number):
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


def cleanup_confirmed():
    cutoff = time.time() - MAX_CONFIRMED_AGE
    stale = [h for h, (_, t) in confirmed_txs.items() if t < cutoff]
    for h in stale:
        del confirmed_txs[h]
    return len(stale)


async def collect(ws_url, rpc_url, duration):
    import websockets

    global pending_total, block_total

    print(f"Late Pending TX Verification")
    print(f"WS: {ws_url}   RPC: {rpc_url}")
    if duration:
        print(f"Duration: {duration:.0f}s")
    print(f"Checking if pending tx notifications arrive after block confirmation...")
    print("Ctrl+C to stop\n")

    start = time.time()
    blocks_since_cleanup = 0

    async with websockets.connect(ws_url, ping_interval=20, ping_timeout=30) as ws:
        # Subscribe pending
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "method": "eth_subscribe",
            "params": ["newPendingTransactions"], "id": 1,
        }))
        r1 = json.loads(await ws.recv())
        if "error" in r1:
            print(f"ERROR: newPendingTransactions not supported: {r1['error']}")
            return
        pending_sub = r1["result"]

        # Subscribe heads
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "method": "eth_subscribe",
            "params": ["newHeads"], "id": 2,
        }))
        r2 = json.loads(await ws.recv())
        if "error" in r2:
            print(f"ERROR: newHeads failed: {r2['error']}")
            return
        heads_sub = r2["result"]

        print(f"  Subscribed. Monitoring...\n")

        while running:
            if duration and time.time() - start > duration:
                break

            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            except asyncio.TimeoutError:
                continue
            except Exception:
                print("  Connection lost.")
                break

            if "params" not in msg:
                continue

            sub = msg["params"].get("subscription")

            if sub == heads_sub:
                h = msg["params"]["result"]
                num = int(h["number"], 16)
                bt = time.time()
                block_total += 1

                tx_hashes = rpc_get_block_txhashes(rpc_url, num)
                for tx in tx_hashes:
                    confirmed_txs[tx] = (num, bt)
                    seen_pending.discard(tx)

                blocks_since_cleanup += 1
                if blocks_since_cleanup >= 30:
                    cleanup_confirmed()
                    blocks_since_cleanup = 0

                elapsed = bt - start
                print(f"\r  blocks={block_total}  pending={pending_total}  "
                      f"late={len(late_pendings)}  confirmed_pool={len(confirmed_txs)}  "
                      f"elapsed={elapsed:.0f}s", end="", flush=True)

            elif sub == pending_sub:
                tx_hash = msg["params"]["result"]
                if isinstance(tx_hash, str):
                    tx_hash = tx_hash.lower()
                    if tx_hash in seen_pending:
                        continue
                    seen_pending.add(tx_hash)
                    pending_total += 1

                    if tx_hash in confirmed_txs:
                        blk_num, blk_time = confirmed_txs[tx_hash]
                        pt = time.time()
                        lag = pt - blk_time
                        late_pendings.append((tx_hash, blk_num, blk_time, pt, lag))
                        print(f"\n  *** LATE: {tx_hash[:16]}.. "
                              f"block={blk_num} lag={lag*1000:.1f}ms")

    # ── Report ──
    dur = time.time() - start
    print(f"\n\n{'=' * 60}")
    print(f"  Late Pending TX Verification Report")
    print(f"  {dur:.0f}s, {block_total} blocks, {pending_total} pending txs")
    print(f"{'=' * 60}")

    print(f"\n  Late pending txs: {len(late_pendings)}")
    print(f"  Total pending txs: {pending_total}")
    if pending_total > 0:
        print(f"  Late ratio: {len(late_pendings) / pending_total * 100:.3f}%")

    if late_pendings:
        lags = [lp[4] for lp in late_pendings]
        lags.sort()
        avg_lag = sum(lags) / len(lags)
        print(f"\n  Lag statistics (pending notification - block arrival):")
        print(f"    Min:  {lags[0]*1000:.1f}ms")
        print(f"    Max:  {lags[-1]*1000:.1f}ms")
        print(f"    Mean: {avg_lag*1000:.1f}ms")
        if len(lags) >= 2:
            mid = len(lags) // 2
            med = lags[mid] if len(lags) % 2 else (lags[mid-1] + lags[mid]) / 2
            print(f"    P50:  {med*1000:.1f}ms")

        print(f"\n  First 20 late pending txs:")
        for tx, blk, bt, pt, lag in late_pendings[:20]:
            print(f"    {tx[:16]}..  block={blk}  lag={lag*1000:.1f}ms")

    print(f"\n{'=' * 60}\n")


async def main():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("pip install websockets")
        sys.exit(1)

    p = argparse.ArgumentParser(description="Verify late pending tx notifications")
    p.add_argument("--ws", default="ws://localhost:8546")
    p.add_argument("--rpc", default="http://localhost:8545")
    p.add_argument("--duration", type=float, default=None,
                   help="Auto-stop after N seconds (default: run until Ctrl+C)")
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
