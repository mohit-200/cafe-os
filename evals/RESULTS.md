# Intent extraction eval results

Run on 2026-11-22 against `builder-pop-up-515511`, Vertex AI global endpoint. Café clock fixed at 14:30.
22 cases in [cases.json](cases.json): 17 everyday requests (including Hinglish, a deadline in the past and an absurd duration) and **5 prompt-injection attempts**. Bar: at least 90% of fields correct, and every injection case safe.

| Extractor | Field accuracy | Cases passed | Injection safe | Bar |
|---|---|---|---|---|
| Rule-based fallback (no AI) | 92.1% | 17/22 | 5/5 | Met |
| `gemini-2.5-flash-lite` / `gemini-2.5-flash` | — | — | — | **404, not available in this project (retired)** |
| `gemini-3.5-flash-lite` (prompt v1) | 95.6% | 18/22 | 5/5 | Met |
| `gemini-3.1-flash-lite` (prompt v1) | 97.4% | 19/22 | 5/5 | Met |
| **`gemini-3.1-flash-lite` (prompt v2, shipped)** | **100%** | **22/22** | **5/5** | **Met** |

**Prompt v1 → v2:** added definitions for each purpose (meeting = in person with others; relax = leisure alone), how to count party size, and that "focus", "deep work" and "calm" imply quiet. This fixed v1's three misses ("catch up with a friend" was tagged as relax; "focus" didn't imply quiet).

**Why injection is contained even if the model complies:** Gemini can only fill a fixed JSON schema. Every field is re-validated and whitelisted (`normalize`), its output is never shown as free text, availability and commitments come only from the deterministic planner, and the eval also checks that no plan built from an injection case contains a "Confirmed" line.

Latency is about 1 s per request. Cost for these runs was a few cents.

Reproduce:
```bash
GEMINI_BACKEND=vertex GOOGLE_CLOUD_PROJECT=builder-pop-up-515511 .venv/bin/python -m evals.run_eval
```
