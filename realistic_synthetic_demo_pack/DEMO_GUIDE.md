# Synthetic Contract Demo Pack

All entities, addresses, emails, bank details, IDs, and signatures in this pack are fictional and are provided only for software demonstration.

## Recommended demo order

1. `01_Master_Services_Agreement_MSA-2026-014.pdf`
   - Creates initial register:
     - payment_due_days = 30
     - liability_cap = USD 250,000
     - termination_notice_days = 60
   - PAY-01 / LIA-01 / TERM-01 should pass.

2. `02_Amendment_No_1_AMD-2026-014-01.pdf`
   - Explicitly supersedes MSA Section 4.3: payment 30 -> 45 days.
   - Explicitly supersedes MSA Section 9.2: notice 60 -> 90 days.
   - Liability remains USD 250,000.
   - Human can approve payment and reject/approve notice to demonstrate partial review.

3. `03_Invoice_INV-2026-0417.pdf`
   - Invoice claims NET 10 and USD 18,500 due.
   - Source PAY-01 should be a violation (10 < 30).
   - Invoice explicitly says it is not an amendment; MSA order-of-precedence also places invoice below amendment/MSA.
   - Deliverable register should retain the approved contractual payment term (typically 45 days), not become 10.

4. `04_Data_Processing_Addendum_DPA-2026-014-A.docx`
   - Proves DOCX ingestion.
   - Adds security/privacy obligations (72-hour incident notification).
   - Explicitly says it does not modify payment or liability.

5. `05_Operational_Notice_OPS-NOTICE-2026-0528.txt`
   - Proves TXT ingestion.
   - Operational notice only; should not modify contract register values.

6. `rules.json`
   - Synthetic buyer playbook for payment, liability, and termination rules.

## Strong live-demo story

MSA -> register v1 -> Amendment proposes changes -> human review -> Invoice introduces conflicting 10-day payment term -> source rule violation -> final register remains governed by approved amendment -> DPA/TXT prove mixed-format ingestion without unrelated keys being overwritten.
