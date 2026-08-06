"""Measure what your machine actually does, and project a real run from it.

Guessing at model choice from someone else's benchmark is how you end up
waiting three minutes per business. This runs the real classify prompt at a
real evidence size against each model you name and reports prefill and
generation separately, because they fail for different reasons:

* slow prefill  -> the prompt is too long (lower --evidence-chars)
* slow generate -> the model is too big (try a smaller one)

On CPU, threads contend for the same cores, so more workers does not help --
the projection below assumes one.
"""

from __future__ import annotations

import time

from .classify import PROMPT, SCHEMA, SYSTEM
from .llm import Ollama, OllamaError

# Small, instruction-following models that hold up on CPU. Sizes are the Q4
# download; anything over ~5 GB will struggle on a 16 GB CPU-only box.
SUGGESTED = ["gemma4:e4b", "qwen3.5:4b", "phi4-mini", "gemma4:e2b"]

SAMPLE_ICP = (
    "An independently owned funeral home, mortuary, crematory or cemetery "
    "that serves families directly. Exclude casket retailers, headstone "
    "manufacturers, life-insurance agencies, hospices and pet cremation."
)

# Stands in for a real html2text dump: boilerplate, then the useful part.
FILLER = (
    "Home About Us Services Obituaries Pre-Planning Grief Support Contact "
    "Directions Hours Monday Tuesday Wednesday Thursday Friday Saturday "
    "Sunday Privacy Policy Terms Accessibility Site Map Careers "
)
BODY = (
    "Riverside Funeral Home and Cremation has served families in Agawam since "
    "1948. We provide full funeral services, burial, cremation and memorial "
    "planning. Our family owned firm is led by owner and funeral director "
    "Margaret A. Whitfield, who took over from her father in 2011. "
)


def sample_text(n_chars: int) -> str:
    text = BODY + (FILLER * 200)
    return text[:n_chars]


def run(
    host: str,
    models: list[str],
    evidence_chars: int = 2500,
    n_businesses: int = 1500,
    runs: int = 3,
) -> None:
    print(
        f"Benchmarking {len(models)} model(s) at {evidence_chars:,} chars of "
        f"evidence, {runs} runs each.\n"
        f"First call per model includes load time and is discarded.\n"
    )
    text = sample_text(evidence_chars)
    prompt = PROMPT.format(
        icp=SAMPLE_ICP,
        name="Riverside Funeral Home & Cremation",
        category="Funeral home",
        types='["Funeral home", "Cremation service"]',
        address="12 River Rd, Agawam, MA 01001",
        website="https://riversidefh.com",
        text=text,
    )

    header = (
        f"{'model':<16}{'prompt tok':>11}{'prefill/s':>11}"
        f"{'gen/s':>8}{'sec/call':>10}{'1,500 rows':>12}"
    )
    print(header)
    print("-" * len(header))

    results = []
    for model in models:
        llm = Ollama(host=host, model=model, num_ctx=4096, timeout=900)
        try:
            llm.check()
        except SystemExit as exc:
            print(f"{model:<16}  {exc}")
            continue

        samples = []
        for i in range(runs + 1):
            t0 = time.monotonic()
            try:
                llm.json_chat(SYSTEM, prompt, SCHEMA)
            except OllamaError as exc:
                print(f"{model:<16}  failed: {exc}")
                break
            wall = time.monotonic() - t0
            if i > 0:                      # discard the load-time run
                samples.append((wall, dict(llm.last)))
        if not samples:
            continue

        wall = sum(w for w, _ in samples) / len(samples)
        m = samples[-1][1]
        hours = wall * n_businesses / 3600
        results.append((model, wall, hours))
        print(
            f"{model:<16}{m['prompt_tokens']:>11,.0f}{m['prefill_tok_s']:>11,.1f}"
            f"{m['gen_tok_s']:>8,.1f}{wall:>10,.1f}{hours:>11,.1f}h"
        )

    if not results:
        print("\nNo model completed. Pull one first, e.g. `ollama pull gemma4:e4b`.")
        return

    best, wall, hours = min(results, key=lambda r: r[1])
    print(f"\nFastest: {best} at {wall:,.1f}s per business ({hours:,.1f}h for "
          f"{n_businesses:,} rows).")
    print(
        f"Set it in .env:\n"
        f"  OLLAMA_MODEL={best}\n"
        f"  LLM_MAX_EVIDENCE_CHARS={evidence_chars}\n"
        f"and run the LLM stages with --workers 1 on CPU."
    )
    if wall > 20:
        print(
            "\nStill slow. Try --evidence-chars 1500, or a smaller model. "
            "Classification only needs enough text to tell the business type "
            "-- it is not summarising the page."
        )
