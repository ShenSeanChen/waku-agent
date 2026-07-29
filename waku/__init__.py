"""waku-agent — a minimal, transparent, local-first Waku.

Four pillars, one module each:
  harness  → waku/runtime + waku/gateway  (scaffolding around the raw LLM)
  loop     → waku/loop                      (observe → reason → act → repeat)
             waku/graph                     (opt-in structure around the loop — extends this pillar)
  memory   → waku/memory                    (procedural / semantic / episodic)
  ops      → waku/ops + evals/              (trace → eval → gate → release)
"""

__version__ = "0.1.0"
