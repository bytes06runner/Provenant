# Devpost submission assets

| File | What it is | Devpost field |
|---|---|---|
| `gallery/01-provenant-title.png` to `gallery/15-provenant-closing.png` | 15 images, 1800x1200 (3:2), PNG, each under 300 KB | Image gallery (in this order) |
| `Provenant-demo.mp4` | Demo video, 1:44, 1920x1080, H.264, no music | Upload to YouTube, then paste the link in Video demo link |
| `Provenant-project-brief.pdf` | 4-page project brief: problem, solution, PayPal and AI use, results, limitations | Optional file upload |
| `devpost-story.md` | Text for the "About the project" sections | Project story |

## Gallery order and captions

1. **Provenant**: a tribunal for every purchase your agent makes.
2. **The problem**: agents get hijacked by page text, and nobody knows who pays when they buy wrong.
3. **How it works**: Lineage prevents, Blackbox rules, Remedy pays, on one Flight Recorder.
4. **Signed mandate**: the buyer's words become fields they review and sign.
5. **Live hijacks blocked**: a payee injected by a page and the attacker's own signed payee.
6. **Provenance graph**: every field of the PayPal order traces to a signature.
7. **PayPal approval**: a real sandbox order, approved with the JS SDK v6 button.
8. **Before**: a conventional agent on PayPal's Agent Toolkit typed its own totals.
9. **Ruling**: shared fault between the buyer and the merchant, with intervals.
10. **Counterfactual replay**: what happens when each party is corrected.
11. **Remedy**: the merchant's share refunded on PayPal.
12. **Wrong item shipped**: the photo check finds a fulfillment fault.
13. **Ops console**: one operator click per money movement.
14. **Results**: 14 of 14 hijacks blocked, 3 of 3 planted faults correct.
15. **Closing**: trust the purchase, and know who pays when it goes wrong.

## Regenerating

With the API on :8700, the simulator on :8710 and the web app on :3000, run each command from `web/`:

```bash
node ../Submission/source/make_gallery.mjs
```

```bash
node ../Submission/source/make_video.mjs
```

```bash
node ../Submission/source/make_brief.mjs
```

`source/cards.html` holds the designed cards and `source/brief.html` the brief. App screens come
from the running app (`docs/screenshots/`, captured by `web/scripts/screenshots.mjs`).
