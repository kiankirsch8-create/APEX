# VALENTINI fidelity check · masterclass live day vs Databento trades (2026-11-15)

Data: Databento GLBX.MDP3 trades, NQ front month (NQU5), 23 Jun 2025, built by `scalping/orderflow/build_nq_trades.py`.
Big orders = trades with the **same exchange timestamp and same aggressor side** summed into one order (rule E2, INTERPRETED).
Chart: `valentini_0623_check.png` (1m, ET, circles = orders >= 30 lots).

## Clock time from video time
- "it's 3:24" (15:24 Italy = 09:24 ET), cash open at video ~1:39:30.
- Check: video 3:17 -> 11:08 ET = 19:08 Dubai; he says "it's already 7". Video runs without cuts.
- ET ≈ 09:30 + (video time − 1:39:30). Precision about ±1 min.

## His live trades vs the data ($20 per NQ point, prices reconstructed from his $ risk)
| Trade | Video | ET | What he says | What the data shows | Match |
|---|---|---|---|---|---|
| T1 long | 2:11–2:30 | 10:02–10:21 | buy stop on break of the high, risk $600–650, "laser green line", half off ~+$800, out at the 22,000 round number, ~$1,000–1,100 total | 10:00 high 21,956.25 broken at 10:02, rally to 22,012 at 10:20 (+55 pts); 114/112-lot sells at 10:10 | yes |
| T2 long | 2:54–2:57 | 10:45–10:48 | full 1m body close above box, risk $260, failed auction, stop −$255 | 10:44 close 22,005.00; 10:48 low 21,982.25 (−12.75 pts), 114-lot sell at 21,983.75 | yes |
| T3 long | 3:09–3:21 | 11:00–11:12 | "ultra-aggressive sellers absorbed near LVN", "green egg", entry on close, risk $250, stop to +$250 after high breaks, out +$250 | 141/120-lot sells at 21,950–21,970 (10:56–11:00) without follow-through; 112-lot buy 21,972.75 at 11:01:23; close 21,971; 11:12 low 21,989 (+18 pts) | yes |
| none | 3:22–3:30 | 11:13–11:21 | waits for a break, "it broke the session, setup not valid" | 11:25–11:30 break down to 21,932 | yes |

Pre-open: he names 21,822 at video 1:21; the open shakeout runs to 21,774 (09:35) with big sells absorbed, then reclaims.

## Verdict
The 30+ lot aggregated orders appear where and when he describes big trades, absorption and the "green egg".
Grouping by identical timestamp + side reproduces his bubbles well enough for the trigger (E1/E2). How Deepcharts
groups orders exactly is still unknown (INTERPRETED). Entry prices are reconstructed from his stated dollar risk,
not stated by him.

Next: §0b audit of noise filter, management, day type/sizing and entry timing BEFORE the Python port.
