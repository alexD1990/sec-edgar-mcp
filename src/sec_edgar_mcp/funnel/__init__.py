"""The research funnel: stage 1 (screening) and stage 2 (light analysis) as plain code.

    python -m sec_edgar_mcp.funnel stage1     # all filers -> a few hundred candidates
    python -m sec_edgar_mcp.funnel stage2     # candidates -> ~20 with a fact sheet each

Results go to <stockdocs>/runs/<date>/. Stage 3 (deep analysis) is done by Claude (Opus) from
those fact sheets, following stockdocs/method/stage3-deep.md. The rules and thresholds here
mirror stockdocs/method/*.md; change both together.
"""
