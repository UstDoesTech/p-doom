"""Train rl_policy with REINFORCE in the Python mock arena.

    python -m pdoom.train.rl --iterations 300 --out src/pdoom/weights/rl_policy.json

Reward per decision: progress toward the exit, kills, damage taken; +10 for clearing, -5 for dying.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import time
from pathlib import Path

from ..features import N_FEATURES, featurize
from ..players.rl_policy import ACTIONS, DEFAULT_WEIGHTS, RlPolicy, logits, softmax
from ..players.rule_bot import RuleBot
from ..sim import INTENT_KEYS, MockWorld, evaluate

GAP_TICS = 35
MAX_DECISIONS = 120
GAMMA = 0.99


def rollout(weights: list[list[float]], seed: int, rng: random.Random):
    world = MockWorld(seed)
    traj: list[tuple[list[float], int, float]] = []
    for _ in range(MAX_DECISIONS):
        if world.status() != "running":
            break
        feats = featurize(world.state())
        probs = softmax(logits(weights, feats))
        a = rng.choices(range(len(ACTIONS)), weights=probs)[0]
        d0, k0, h0 = world.exit_distance(), world.kills, world.health
        key = INTENT_KEYS[ACTIONS[a]]
        for _ in range(GAP_TICS):
            world.step(key)
            if world.status() != "running":
                break
        r = (d0 - world.exit_distance()) / 200.0 + 0.5 * (world.kills - k0)
        r -= (h0 - world.health) / 20.0
        if world.status() == "cleared":
            r += 10.0
        elif world.status() == "died":
            r -= 5.0
        traj.append((feats, a, r - 0.02))
    return traj, world.status()


def train(iterations: int, batch: int, lr: float, seed: int, log=print):
    rng = random.Random(seed)
    weights = [[0.0] * N_FEATURES for _ in ACTIONS]
    for it in range(iterations):
        grads = [[0.0] * N_FEATURES for _ in ACTIONS]
        episodes = []
        for _ in range(batch):
            traj, status = rollout(weights, rng.randrange(10_000, 1_000_000), rng)
            rets, g = [], 0.0
            for _, _, r in reversed(traj):
                g = r + GAMMA * g
                rets.append(g)
            rets.reverse()
            episodes.append((traj, rets, status))
        flat = [g for _, rets, _ in episodes for g in rets]
        if not flat:
            continue
        mean = sum(flat) / len(flat)
        std = math.sqrt(sum((g - mean) ** 2 for g in flat) / len(flat)) + 1e-6
        n = len(flat)
        for traj, rets, _ in episodes:
            for (feats, a, _), g in zip(traj, rets, strict=True):
                adv = (g - mean) / std
                probs = softmax(logits(weights, feats))
                for k in range(len(ACTIONS)):
                    coeff = ((1.0 if k == a else 0.0) - probs[k]) * adv / n
                    row = grads[k]
                    for j, f in enumerate(feats):
                        row[j] += coeff * f
        for k in range(len(ACTIONS)):
            for j in range(N_FEATURES):
                weights[k][j] += lr * grads[k][j] * 10.0
        if it % 25 == 0 or it == iterations - 1:
            cleared = sum(s == "cleared" for _, _, s in episodes) / batch
            died = sum(s == "died" for _, _, s in episodes) / batch
            log(f"iter {it:4d} cleared={cleared:.2f} died={died:.2f} mean_return={mean:.2f}")
    return weights


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iterations", type=int, default=300)
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--lr", type=float, default=0.5)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", type=Path, default=DEFAULT_WEIGHTS)
    args = ap.parse_args()
    t0 = time.time()
    weights = train(args.iterations, args.batch, args.lr, args.seed)
    held_out = list(range(100, 300))
    learned = evaluate(RlPolicy(weights), held_out)
    baseline = evaluate(RuleBot(), held_out)
    meta = {
        "trained_in": "pdoom.sim mock arena (not real Doom)",
        "iterations": args.iterations,
        "batch": args.batch,
        "seed": args.seed,
        "held_out_seeds": "100-299",
        "rl_policy": learned,
        "rule_bot": baseline,
        "train_seconds": round(time.time() - t0, 1),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"actions": [a.value for a in ACTIONS], "weights": weights, "meta": meta}
    args.out.write_text(json.dumps(payload, indent=1))
    print(json.dumps(meta, indent=1))


if __name__ == "__main__":
    main()
