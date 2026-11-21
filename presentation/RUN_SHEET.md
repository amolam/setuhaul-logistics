# SetuHaul readout: run sheet (15 minutes)

Keep this beside the laptop. The deck is `SetuHaul_readout.pptx`. The speaker notes on each slide have the script.

## Pre-flight (30 minutes before)

- [ ] Test PostgreSQL cluster on port 55432 is running. After a reboot, restart it with `pg_ctl` from the old scratchpad `pgdata`.
- [ ] Demo server started from a Terminal tab (`python demo/server.py`), console open at http://127.0.0.1:8765/demo
- [ ] "Reset for presentation" pressed: fresh database, policy rule OFF, AI toggle on Rules
- [ ] Pre-flight line in the header is all green; the OpenRouter key shows as present
- [ ] One beat A dry run in Real mode, then reset again (costs about $0.01)
- [ ] OpenRouter credit is above $0.50; the daily cap is $0.25
- [ ] Switch the AI toggle to **Real model** just before the talk
- [ ] Deck open in Presenter View, the demo console in a second window; Alt+Tab tested
- [ ] Phone notifications off, laptop on power, screen sleep off

> The pre-flight line, the reset button, the toggle and the race panel come from the demo changes in
> `demo_changes_prompt.md`. Tick these boxes only once that work is merged and you have run it yourself.

## Timing

| Clock | Slide or screen | Say / do | If behind |
|---|---|---|---|
| 0:00 | 1 Title | One sentence on what we built and what we'll prove | — |
| 0:20 | 2 Problem | Ask the driver vs look it up; 20–35 messages in 30 minutes | Skip the stat strip |
| 1:15 | 3 Where the AI stops | "The agent requests; the app and the database decide" | — |
| 2:30 | 4, then the console | Beat A: late driver | — |
| 4:30 | Console | Beat B: race with 2 drivers | — |
| 6:00 | Console | Beat C: dock breakdown | **Drop C first** |
| 7:15 | Console | Beat D: no slot, open the hand-off record | Never drop D (the brief requires it) |
| 8:30 | Console, then 5 | Race with N = 50: 1 won, 49 refused, 0 overlaps. Takes about 3.5 minutes on the laptop (N = 10 about 1 minute): start it at 6:00 during beat C, or run N = 10 live | Show slide 5 only |
| 10:00 | 6 Evidence | 95% held-out, 0 hard gates, NO-GO stated plainly (30 s) | One sentence |
| 10:30 | 7 Running cost | $440/month, $2,060 at 10× (4.7×); the model is the line to watch | Say the two totals only |
| 11:15 | 8 Questions | Answer from the appendix (slides 9–17) | — |

## If something breaks on stage

- **Model slow or failing:** switch the AI toggle to Rules. Say: "Capacity never depended on the model; this is the
  fallback the production design uses."
- **Demo server down:** go to slide 5 and tell the race result from the slide. Restart the server in its
  Terminal tab only during questions.
- **Faculty question mid-demo:** answer it, then drop beat C.

## Appendix map (for questions)

| Question | Slide |
|---|---|
| How did you audit the data? | 9 Data audit |
| What states does a booking go through? | 10 Showing is not reserving |
| Who wins when capacity is short? | 11 Allocation policy |
| When does a person step in? | 12 Human in the loop |
| Duplicates, retries, stale options? | 13 Failure handling |
| How did you break your own system? | 14 How we broke it |
| Why OpenRouter, and what does the model cost? | 15 Model and cost |
| How does this map to the brief's measures? | 16 Measures scorecard |
| What does the infrastructure cost, line by line? | 17 Cost model |
