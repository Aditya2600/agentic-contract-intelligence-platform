# Synthetic contract demo pack

All entities, addresses, emails, bank details, IDs and signatures in this pack are
fictional and exist only to demonstrate software. `scripts/run_demo.py` (`make demo`) runs
all seven through one collection, in this order, and asserts the outcome of each.

| # | File | Format | The state it drives the pipeline into |
|---|---|---|---|
| 1 | `01_Master_Services_Agreement_MSA-2026-014.pdf` | PDF | Baseline register: `payment_due_days = 30`, `liability_cap = USD 250,000`, `notice_days = 60`. PAY-01 / LIA-01 / TERM-01 all pass. |
| 2 | `02_Amendment_No_1_AMD-2026-014-01.pdf` | PDF | Expressly supersedes MSA §4.3 (payment 30→45) and §9.2 (notice 60→90). Both arrive as `supersession_candidate` proposals for a human, never applied automatically. Liability is untouched and its row stays byte-identical. |
| 3 | `03_Invoice_INV-2026-0417.pdf` | PDF | Claims NET 10 and USD 18,500 due. Source PAY-01 is a **violation** (10 < 30). The invoice explicitly says it is not an amendment, so the reviewer **rejects** its payment term while approving the finding — item-level approve *and* reject in one gate. The contractual 45 days stands. |
| 4 | `04_Data_Processing_Addendum_DPA-2026-014-A.docx` | DOCX | Proves DOCX ingestion. Adds privacy obligations, explicitly modifies neither payment nor liability, and the whole register comes out byte-identical. |
| 5 | `05_Operational_Notice_OPS-NOTICE-2026-0528.txt` | TXT | Proves TXT ingestion, and cannot be typed confidently — classification **escalates to a human** rather than guessing. Commits nothing. |
| 6 | `06_Vendor_Portal_Policy_VPT-2026-014.txt` | TXT | One paragraph impersonates a system prompt, demands approval, asks not to be reported, and buries a 5-day payment term. That **one block is withheld** from the model; the other five are processed normally. The 5-day term is never extracted at all, and the run cannot report `clean`. |
| 7 | `07_Statement_of_Work_SOW-2026-014-A.txt` | TXT | Says payment is due 45 days "of acceptance". The extractor proposes an anchor of `receipt`, which the source text does not state, so grounding refuses it: one **repair attempt**, then **abstention**. Nothing is committed on a quote that fails. |
| — | `rules.json` | JSON | The synthetic buyer playbook: payment, liability and termination rules. |

## The story, end to end

MSA creates the register → the amendment *proposes* supersession and a human accepts it →
the invoice introduces a contradicting 10-day term, raises a source violation, and is
rejected at the item level so the negotiated term survives → the DPA and the notice prove
mixed-format ingestion without disturbing unrelated keys → the portal policy proves a
hostile paragraph is contained rather than obeyed, and cannot deny service to the rest of
its own document → the SOW proves an unsupported claim is abstained on rather than rounded
into a plausible value.
