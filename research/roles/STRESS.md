# Stress-Tester brief (sub-agent)

A strategy passed its build test. Before any hidden-year data is spent, test whether it is robust.

1. `python -m research.robustness --idea <id> --strategy <name>` (about 12 min; build period only).
   It reruns the strategy with each numeric parameter at x0.9 and x1.1 and with +0.2 pip slippage.
2. Gate: every variant keeps net > 0, the median variant keeps >= 50% of the base net, slippage run net > 0.
3. Return the variant table and verdict, and point out which parameter it is most sensitive to.
   A fail means the edge is probably fitted to exact values (failure mode "overfit"); the Analyst takes it from there.
