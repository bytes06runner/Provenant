# Narration script for Provenant-demo.mp4 (1:44)

Timed to the video's scenes at a relaxed pace of about 150 words a minute. Each line fits its scene;
small overruns into the next scene are fine.

| Time | On screen | Say |
|---|---|---|
| 0:00 to 0:09 | Title, then the problem card | This is Provenant. Shopping agents can be hijacked by one line on a web page, and when they buy wrong, nobody knows who pays. |
| 0:09 to 0:21 | The baseline agent's run | Here's a typical agent built on PayPal's Agent Toolkit. It reads shop pages and types the order itself. PayPal now holds 149 dollars, but the merchant's signed total is 160.88, over my budget. |
| 0:21 to 0:32 | Mandate card | Now the same request through Provenant. My words become a mandate that I review and sign. Ambiguities get asked, never guessed, and every field I confirm is labeled User. |
| 0:32 to 0:43 | Live run, hijacks blocked | As the agent shops, every result carries a label, and reviews stay untrusted. A page tries to swap the payee, and so does the attacker's own shop. Both are blocked. |
| 0:43 to 0:52 | Provenance graph | The provenance graph traces every field of the PayPal order to my signature or the merchant's seal. Web text can't bind any of them. |
| 0:52 to 1:00 | PayPal order and button | It's a real PayPal order. Its custom ID is the hash of the decision trace, and I approve it with PayPal's own button. |
| 1:00 to 1:14 | Ruling and counterfactuals | Things can still go wrong. These shoes leak, though the merchant signed "waterproof". Blackbox replays the purchase 64 times, correcting each party, then splits the fault: half on me, half on the merchant, none on the agent. |
| 1:14 to 1:18 | Refund | The merchant's share, 45.26, is refunded on PayPal. |
| 1:18 to 1:31 | Ops console | In the operations console, every remedy, whether a void, a refund, or a payout from the agent's liability pool, waits for one human click, and is then reconciled with PayPal until it's final. |
| 1:31 to 1:37 | Results card | On the sandbox: fourteen of fourteen hijacks blocked, and three of three planted faults assigned correctly. |
| 1:37 to 1:44 | Closing card | Provenant. Trust the purchase, and know who pays when it goes wrong. |

## Recording tips

- Record the whole thing in one take while the video plays muted, so your timing follows the
  screen. QuickTime Player (File, New Audio Recording) is enough.
- Pause briefly at each scene change; the captions already carry the details, so you don't need to
  read numbers word for word if a line runs long.
- Say "one sixty point eight eight" and "forty-five twenty-six" for the amounts.
