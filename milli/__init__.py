"""milli-agent — a minimal, transparent, local-first Milli.

Four pillars, one module each:
  harness  → milli/runtime + milli/gateway  (scaffolding around the raw LLM)
  loop     → milli/loop                      (observe → reason → act → repeat)
             milli/graph                     (opt-in structure around the loop — extends this pillar)
  memory   → milli/memory                    (procedural / semantic / episodic)
  ops      → milli/ops + evals/              (trace → eval → gate → release)
"""

__version__ = "0.1.0"
