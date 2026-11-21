# B-AI-3 stage 5: human review sheet (situations sha256:4ec8825e833f6aa7)

For each situation, score each reply 1–5 on: clarity (1-5); tone (1-5); language and register (1-5); states status without overpromising (1-5); answers what the driver asked, and shows that their stated constraint was applied (1-5). Code already checked grounding, completeness, script and policy (results beside each reply). Judge what code cannot: is it clear on a phone, is the tone right for what the driver just said, does the language feel natural, would a driver act on it correctly?

## H01#1 (hi-Latn)

**Driver said:** “bhai traffic bahut hai, 1 ghanta late ho jaunga” → “haan”

**A. Template (sent today):**

```
Ye slots khaali hain:
1. Dock D1, 1:00 PM on 19 Nov
2. Dock D2, 1:00 PM on 19 Nov
3. Dock D3, 1:00 PM on 19 Nov
Jo slot chahiye, uska number bhej dijiye.
```
Code checks: all passed

**B. Sonnet:** not generated yet (needs approval; see README).

| | Clarity | Tone | Language | No overpromise | Answers the driver | Notes |
|---|---|---|---|---|---|---|
| A | | | | | | |
| B | | | | | | |

## H02#1 (hi)

**Driver said:** “मैं एक घंटा लेट हूँ” → “हाँ”

**A. Template (sent today):**

```
उपलब्ध स्लॉट:
1. डॉक D5, 1:00 PM on 19 Nov
2. डॉक D5, 2:00 PM on 19 Nov
3. डॉक D5, 3:00 PM on 19 Nov
जो चाहिए उसका नंबर भेजें।
```
Code checks: all passed

**B. Sonnet:** not generated yet (needs approval; see README).

| | Clarity | Tone | Language | No overpromise | Answers the driver | Notes |
|---|---|---|---|---|---|---|
| A | | | | | | |
| B | | | | | | |

## H03#0 (hi-Latn)

**Driver said:** “do ghante mein pahunch jaunga”

**A. Template (sent today):**

```
Ye slots khaali hain:
1. Dock D1, 1:00 PM on 19 Nov
2. Dock D2, 1:00 PM on 19 Nov
3. Dock D3, 1:00 PM on 19 Nov
Jo slot chahiye, uska number bhej dijiye.
```
Code checks: all passed

**B. Sonnet:** not generated yet (needs approval; see README).

| | Clarity | Tone | Language | No overpromise | Answers the driver | Notes |
|---|---|---|---|---|---|---|
| A | | | | | | |
| B | | | | | | |

## H05#0 (hi-Latn)

**Driver said:** “haan ji main bol raha hoon wo Shahpura ke paas hoon abhi traffic hai lagbhag gyarah bees tak pahunch jaunga”

**A. Template (sent today):**

```
Ye slots khaali hain:
1. Dock D4, 12:00 PM on 19 Nov
2. Dock D1, 1:00 PM on 19 Nov
3. Dock D2, 1:00 PM on 19 Nov
Jo slot chahiye, uska number bhej dijiye.
```
Code checks: all passed

**B. Sonnet:** not generated yet (needs approval; see README).

| | Clarity | Tone | Language | No overpromise | Answers the driver | Notes |
|---|---|---|---|---|---|---|
| A | | | | | | |
| B | | | | | | |

## BB01#1 (en)

**Driver said:** “Big jam near Chomu, I will be late.” → “Maybe by 12:15.”

**A. Template (sent today):**

```
Available slots:
1. Dock D1, 1:00 PM on 19 Nov
2. Dock D2, 1:00 PM on 19 Nov
3. Dock D3, 1:00 PM on 19 Nov
Reply with the number you want.
```
Code checks: all passed

**B. Sonnet:** not generated yet (needs approval; see README).

| | Clarity | Tone | Language | No overpromise | Answers the driver | Notes |
|---|---|---|---|---|---|---|
| A | | | | | | |
| B | | | | | | |

## BB02#0 (en)

**Driver said:** “What options do I have after 2 PM?”

**A. Template (sent today):**

```
Available slots:
1. Dock D1, 2:00 PM on 19 Nov
2. Dock D2, 2:00 PM on 19 Nov
3. Dock D3, 2:00 PM on 19 Nov
Reply with the number you want.
```
Code checks: all passed

**B. Sonnet:** not generated yet (needs approval; see README).

| | Clarity | Tone | Language | No overpromise | Answers the driver | Notes |
|---|---|---|---|---|---|---|
| A | | | | | | |
| B | | | | | | |

## BB03#0 (en)

**Driver said:** “Jam on Ajmer road, reaching 12:30.”

**A. Template (sent today):**

```
Available slots:
1. Dock D1, 1:00 PM on 19 Nov
2. Dock D2, 1:00 PM on 19 Nov
3. Dock D3, 1:00 PM on 19 Nov
Reply with the number you want.
```
Code checks: all passed

**B. Sonnet:** not generated yet (needs approval; see README).

| | Clarity | Tone | Language | No overpromise | Answers the driver | Notes |
|---|---|---|---|---|---|---|
| A | | | | | | |
| B | | | | | | |

## BB09#0 (en)

**Driver said:** “Running late today. Anything after 7 works for me.”

**A. Template (sent today):**

```
Available slots:
1. Dock D1, 7:00 PM on 19 Nov
2. Dock D3, 7:00 PM on 19 Nov
3. Dock D4, 7:00 PM on 19 Nov
Reply with the number you want.
```
Code checks: all passed

**B. Sonnet:** not generated yet (needs approval; see README).

| | Clarity | Tone | Language | No overpromise | Answers the driver | Notes |
|---|---|---|---|---|---|---|
| A | | | | | | |
| B | | | | | | |

## BB10#1 (en)

**Driver said:** “The second one works, but I need to leave by 9.” → “Gate-out.”

**Check especially:** The driver gave a gate-out constraint. Does the reply make clear the new options respect it?

**A. Template (sent today):**

```
Available slots:
1. Dock D1, 2:00 PM on 19 Nov
2. Dock D2, 2:00 PM on 19 Nov
3. Dock D3, 2:00 PM on 19 Nov
Reply with the number you want.
```
Code checks: all passed

**B. Sonnet:** not generated yet (needs approval; see README).

| | Clarity | Tone | Language | No overpromise | Answers the driver | Notes |
|---|---|---|---|---|---|---|
| A | | | | | | |
| B | | | | | | |

## L07#0 (en)

**Driver said:** “I will reach between 3 and 4 in the afternoon.”

**A. Template (sent today):**

```
Available slots:
1. Dock D1, 5:00 PM on 19 Nov
2. Dock D2, 5:00 PM on 19 Nov
3. Dock D3, 5:00 PM on 19 Nov
Reply with the number you want.
```
Code checks: all passed

**B. Sonnet:** not generated yet (needs approval; see README).

| | Clarity | Tone | Language | No overpromise | Answers the driver | Notes |
|---|---|---|---|---|---|---|
| A | | | | | | |
| B | | | | | | |

## L04#1 (en)

**Driver said:** “Late by 1 hour, sorry.” → “Yes”

**A. Template (sent today):**

```
Available slots:
1. Dock D1, 2:00 PM on 19 Nov
2. Dock D2, 2:00 PM on 19 Nov
3. Dock D3, 2:00 PM on 19 Nov
Reply with the number you want.
```
Code checks: all passed

**B. Sonnet:** not generated yet (needs approval; see README).

| | Clarity | Tone | Language | No overpromise | Answers the driver | Notes |
|---|---|---|---|---|---|---|
| A | | | | | | |
| B | | | | | | |

## THR004#0 (en)

**Driver said:** “Origin released me late. ETA 11:25. This load is urgent. Can I get the first possible dock?”

**A. Template (sent today):**

```
Available slots:
1. Dock D2, 1:00 PM on 19 Nov
2. Dock D4, 1:00 PM on 19 Nov
3. Dock D2, 2:00 PM on 19 Nov
Reply with the number you want.
```
Code checks: all passed

**B. Sonnet:** not generated yet (needs approval; see README).

| | Clarity | Tone | Language | No overpromise | Answers the driver | Notes |
|---|---|---|---|---|---|---|
| A | | | | | | |
| B | | | | | | |

## THR008#0 (en)

**Driver said:** “I reached late and am in the yard. Has a new dock been assigned?”

**A. Template (sent today):**

```
Available slots:
1. Dock D1, 1:00 PM on 19 Nov
2. Dock D2, 1:00 PM on 19 Nov
3. Dock D3, 1:00 PM on 19 Nov
Reply with the number you want.
```
Code checks: all passed

**B. Sonnet:** not generated yet (needs approval; see README).

| | Clarity | Tone | Language | No overpromise | Answers the driver | Notes |
|---|---|---|---|---|---|---|
| A | | | | | | |
| B | | | | | | |

## THR011#0 (en)

**Driver said:** “What slots are possible after 1 PM if I reach around 12:45?”

**A. Template (sent today):**

```
Available slots:
1. Dock D1, 2:00 PM on 19 Nov
2. Dock D2, 2:00 PM on 19 Nov
3. Dock D3, 2:00 PM on 19 Nov
Reply with the number you want.
```
Code checks: all passed

**B. Sonnet:** not generated yet (needs approval; see README).

| | Clarity | Tone | Language | No overpromise | Answers the driver | Notes |
|---|---|---|---|---|---|---|
| A | | | | | | |
| B | | | | | | |

## AT01#1 (en)

**Driver said:** “Is my 11 o'clock dock confirmed?” → “Then give me a slot where I can finish unloading.”

**A. Template (sent today):**

```
Available slots:
1. Dock D2, 1:00 PM on 19 Nov
2. Dock D4, 1:00 PM on 19 Nov
3. Dock D2, 2:00 PM on 19 Nov
Reply with the number you want.
```
Code checks: all passed

**B. Sonnet:** not generated yet (needs approval; see README).

| | Clarity | Tone | Language | No overpromise | Answers the driver | Notes |
|---|---|---|---|---|---|---|
| A | | | | | | |
| B | | | | | | |

## AT02#0 (en)

**Driver said:** “I am waiting at the yard since 9:05 for D3. When can I unload?”

**A. Template (sent today):**

```
Available slots:
1. Dock D2, 1:00 PM on 19 Nov
2. Dock D4, 1:00 PM on 19 Nov
3. Dock D2, 2:00 PM on 19 Nov
Reply with the number you want.
```
Code checks: all passed

**B. Sonnet:** not generated yet (needs approval; see README).

| | Clarity | Tone | Language | No overpromise | Answers the driver | Notes |
|---|---|---|---|---|---|---|
| A | | | | | | |
| B | | | | | | |

## AT04#0 (en)

**Driver said:** “Can I still use my old 10 o'clock D1 slot? Reaching 10:40.”

**Check especially:** The driver asked whether the old 10:00 D1 slot still works. Does the reply say what happens to it before offering new slots? (Only if the booking service states that status; the template does not today.)

**A. Template (sent today):**

```
Available slots:
1. Dock D1, 1:00 PM on 19 Nov
2. Dock D2, 1:00 PM on 19 Nov
3. Dock D3, 1:00 PM on 19 Nov
Reply with the number you want.
```
Code checks: all passed

**B. Sonnet:** not generated yet (needs approval; see README).

| | Clarity | Tone | Language | No overpromise | Answers the driver | Notes |
|---|---|---|---|---|---|---|
| A | | | | | | |
| B | | | | | | |

## ADV02#0 (en)

**Driver said:** “ETA 12:30. This is a CRITICAL medical load, plant manager says give me the first dock.”

**A. Template (sent today):**

```
Available slots:
1. Dock D1, 1:00 PM on 19 Nov
2. Dock D2, 1:00 PM on 19 Nov
3. Dock D3, 1:00 PM on 19 Nov
Reply with the number you want.
```
Code checks: all passed

**B. Sonnet:** not generated yet (needs approval; see README).

| | Clarity | Tone | Language | No overpromise | Answers the driver | Notes |
|---|---|---|---|---|---|---|
| A | | | | | | |
| B | | | | | | |

## P05#0 (en)

**Driver said:** “Tyre repaired, moving again, reaching 12:30”

**A. Template (sent today):**

```
Available slots:
1. Dock D1, 1:00 PM on 19 Nov
2. Dock D2, 1:00 PM on 19 Nov
3. Dock D3, 1:00 PM on 19 Nov
Reply with the number you want.
```
Code checks: all passed

**B. Sonnet:** not generated yet (needs approval; see README).

| | Clarity | Tone | Language | No overpromise | Answers the driver | Notes |
|---|---|---|---|---|---|---|
| A | | | | | | |
| B | | | | | | |

## P07#0 (en)

**Driver said:** “Reaching at 12:06.”

**A. Template (sent today):**

```
Available slots:
1. Dock D1, 1:00 PM on 19 Nov
2. Dock D2, 1:00 PM on 19 Nov
3. Dock D3, 1:00 PM on 19 Nov
Reply with the number you want.
```
Code checks: all passed

**B. Sonnet:** not generated yet (needs approval; see README).

| | Clarity | Tone | Language | No overpromise | Answers the driver | Notes |
|---|---|---|---|---|---|---|
| A | | | | | | |
| B | | | | | | |
