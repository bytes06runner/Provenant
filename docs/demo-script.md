# Demo script

The submission video is [`Submission/Provenant-demo.mp4`](../Submission/Provenant-demo.mp4)
(1:44, 1920x1080). It was recorded from the real running app by
[`Submission/source/make_video.mjs`](../Submission/source/make_video.mjs): a visible cursor, click
ripples, zooms and captions are injected into the page, and capture pauses while a model call runs,
so there is no dead air but every screen and every PayPal id is real.

| Time | Scene | What the viewer sees | Caption |
|---|---|---|---|
| 0:00 | Title | Provenant: Lineage, Blackbox, Remedy | |
| 0:03 | The problem | Agents get hijacked by page text; nobody knows who pays when they buy wrong | |
| 0:09 | Before | The baseline agent on PayPal's Agent Toolkit, its tool calls and the order PayPal holds | It typed the shop, item, price and quantity into `create_order`. PayPal holds 149.00; the signed total is 160.88, over the 150 budget. |
| 0:21 | Mandate | A request typed by the buyer becomes a Mandate Card; an ambiguity is asked; the buyer signs | Ambiguities are asked, never guessed. Every confirmed field is labeled User. |
| 0:32 | Live run | Tool results stream in with label chips; reviews are Untrusted; two hijack attempts are blocked | A payee injected by a page and the attacker's own signed payee. Both blocked. |
| 0:43 | Provenance graph | Order fields on the right, sources on the left, edges colored by label; zoom on payee and total | Text from a web page can never bind who is paid, what is bought, how much, or where it ships. |
| 0:52 | PayPal approval | A real sandbox order with `custom_id` = trace hash, and the official JS SDK v6 button | You approve with the official PayPal button. |
| 1:00 | Ruling | The "waterproof" case: complaint, 64 replays, counterfactual table, Shapley shares with intervals | Fault: you 50%, merchant 50%, agent 0%. |
| 1:14 | Remedy | Merchant refund 45.26 on PayPal (`3AA89058PW941291F`), reconciled until COMPLETED | |
| 1:18 | Ops console | Recourse queue in AG Grid with approve buttons | Every remedy needs one operator click. |
| 1:31 | Results | 14 of 14 hijacks blocked, 3 of 3 planted faults correct, sandbox ids | |
| 1:37 | Close | Provenant, links | |

## Reproducing it

```bash
.venv/bin/uvicorn api.app:app --port 8700
.venv/bin/uvicorn merchants.app:app --port 8710
cd web && npm run dev
```

```bash
cd web && node ../Submission/source/make_video.mjs
```

The Kestrel ruling, the baseline run (`b-52cf96dfa972`) and the planted cases come from the local
Flight Recorder (backed up outside the repo; see `docs/progress.md`). A fresh checkout can record
its own with `scripts/recourse_case.py` and `scripts/planted_scenarios.py`.

## Voice-over (optional, for a narrated cut)

1. Shopping agents read web pages and then pay. A review can tell them whom to pay, and when they
   buy the wrong thing, nobody knows who owes the money.
2. Here is a typical agent on PayPal's Agent Toolkit. It typed the order itself: the total PayPal
   holds is not the merchant's signed total, and it is over the budget.
3. Provenant starts from what you meant. Your request becomes a mandate you check and sign.
4. While it shops, every value carries where it came from. Reviews are untrusted. Two hijack
   attempts, one from page text and one from the attacker's own signed shop, are blocked.
5. Every field of the PayPal order traces to your signature or the merchant's seal, and the
   order's custom_id is the hash of the whole decision trace.
6. When something still goes wrong, Blackbox replays the purchase with each party corrected, splits
   the fault with exact Shapley values, and moves the money on PayPal: here, a refund of the
   merchant's half.
7. Provenant: prevent, rule, pay.
