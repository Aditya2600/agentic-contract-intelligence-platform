## ----setup, include = FALSE---------------------------------------------------
knitr::opts_chunk$set(collapse = TRUE, comment = "#>")
library(superdocsr)

## ----fake-api-----------------------------------------------------------------
# Recorded payloads, shaped exactly like the documented API responses.
payloads <- list(
  upload = list(
    html = "<div data-chunk-id=\"c1\"><h1>Effects of X on Y</h1></div>",
    session_id = "paper-2026", filename = "manuscript.docx",
    chunks_count = 96, version_id = "v_1",
    page_setup = list(width_in = 8.27, height_in = 11.69, orientation = "portrait")
  ),
  started = list(job_id = "job_7f3a", session_id = "paper-2026", status = "pending"),
  review = list(
    job_id = "job_7f3a", session_id = "paper-2026", job_type = "chat",
    status = "awaiting_approval", progress = 60,
    created_at = "2026-08-10T10:00:00Z", updated_at = "2026-08-10T10:01:12Z",
    metadata = list(intermediate_responses = list(
      list(type = "intermediate", content = "Reading the discussion...", sequence = 1),
      # Note the shape: `content` is a JSON string inside JSON.
      list(type = "proposed_change_batch", sequence = 2, content = jsonlite::toJSON(list(
        type = "batch_approval", batch_id = "ch_1", batch_total = 2,
        changes = list(
          list(
            change_id = "ch_1", operation = "edit", chunk_id = "c41",
            old_html = "<p>The effect was very significant.</p>",
            new_html = "<p>The effect was significant (p = 0.03).</p>",
            ai_explanation = "Replaced an intensifier with the reported statistic"
          ),
          list(
            change_id = "ch_2", operation = "delete", chunk_id = "c58",
            old_html = "<p>This clearly proves the mechanism.</p>",
            new_html = NULL,
            ai_explanation = "Removed a causal claim the data does not support"
          )
        )
      ), auto_unbox = TRUE, null = "null"))
    ))
  ),
  done = list(
    job_id = "job_7f3a", session_id = "paper-2026", job_type = "chat",
    status = "completed", progress = 100,
    created_at = "2026-08-10T10:00:00Z", updated_at = "2026-08-10T10:02:40Z",
    result = list(response = "Applied 1 change.", session_id = "paper-2026")
  )
)

json <- function(x) {
  httr2::response(
    status_code = 200, headers = list(`Content-Type` = "application/json"),
    body = charToRaw(jsonlite::toJSON(x, auto_unbox = TRUE, null = "null"))
  )
}

approved_yet <- FALSE
transport <- function(req) {
  path <- httr2::url_parse(req$url)$path
  if (grepl("documents/upload", path)) return(json(payloads$upload))
  if (grepl("chat/async", path)) return(json(payloads$started))
  if (grepl("approve", path)) {
    approved_yet <<- TRUE
    return(json(list(status = "ok")))
  }
  if (grepl("^/v1/jobs/", path)) {
    return(json(if (approved_yet) payloads$done else payloads$review))
  }
  if (grepl("documents/export", path)) {
    return(httr2::response(200L, body = charToRaw("PK\003\004 ...docx bytes...")))
  }
  stop("unrecorded call: ", path)
}

## ----real-client, eval = FALSE------------------------------------------------
# client <- sd_client() # reads SUPERDOCS_API_KEY
# sd_verify_key(client) # cheap check that the key works

## ----client-------------------------------------------------------------------
client <- sd_client(
  api_key = "sk_not_a_real_key",
  budget = sd_budget(max_operations = 3, max_pages = 40),
  transport = transport
)
client

## ----upload-------------------------------------------------------------------
manuscript <- tempfile(fileext = ".docx")
writeBin(charToRaw("PK... a rendered manuscript ..."), manuscript)

doc <- sd_upload(manuscript, client, session_id = "paper-2026")
doc

## ----edit---------------------------------------------------------------------
job <- sd_edit(
  doc,
  paste(
    "In the discussion, replace vague intensifiers with the reported statistics,",
    "and remove any causal claim the results section does not support.",
    "Do not touch the methods, the citations, or the numbers."
  )
)
job

## ----wait---------------------------------------------------------------------
job <- sd_wait(job, timeout = 300, verbose = FALSE)
job$status
sd_awaiting_kind(job)

## ----changes------------------------------------------------------------------
changes <- sd_changes(job, refresh = FALSE)
changes

## ----changes-df---------------------------------------------------------------
changes[, c("change_id", "operation", "ai_explanation")]

## ----double-parse-------------------------------------------------------------
raw_event <- payloads$review$metadata$intermediate_responses[[2]]
substr(raw_event$content, 1, 60)

# One parse gets you a string. Two gets you the changes.
class(raw_event$content)
length(jsonlite::fromJSON(raw_event$content, simplifyVector = FALSE)$changes)

## ----approve------------------------------------------------------------------
keep <- changes$change_id[changes$operation == "edit"]
drop <- changes$change_id[changes$operation == "delete"]

job <- sd_approve(job, keep)
job <- sd_deny(job, drop, feedback = "Keep the sentence; soften it instead of cutting it.")
job <- sd_wait(job, verbose = FALSE)
job$status

## ----export-------------------------------------------------------------------
out <- file.path(tempdir(), "manuscript-revised.docx")
sd_export(doc, format = "docx", path = out)
file.size(out) > 0

## ----knit, eval = FALSE-------------------------------------------------------
# result <- sd_knit(
#   "paper.Rmd",
#   "Tighten the discussion; keep every citation and number exactly as written.",
#   output_format = "word_document",
#   export_path = "paper-revised.docx"
# )
# result$changes

## ----review-fns, eval = FALSE-------------------------------------------------
# # Prepare the work, decide later.
# sd_knit("paper.Rmd", "...", review = sd_review_none)
# 
# # A rule you can defend: accept wording edits, never accept deletions.
# sd_knit("paper.Rmd", "...", review = function(changes) {
#   changes$change_id[changes$operation == "edit"]
# })

## ----budget-------------------------------------------------------------------
sd_ops_used(client)
sd_ops_remaining(client)

tiny <- sd_client(api_key = "sk_x", budget = sd_budget(max_operations = 0), transport = transport)
try(sd_edit(sd_document(tiny, "paper-2026"), "Rewrite everything."))

## ----pages--------------------------------------------------------------------
strict <- sd_client(
  api_key = "sk_x",
  budget = sd_budget(max_pages = 5, chunks_per_page = 12),
  transport = transport
)
try(sd_upload(manuscript, strict, session_id = "paper-2026"))

## ----resume-------------------------------------------------------------------
doc <- sd_document(client, session_id = "paper-2026")
job <- sd_job(client, job_id = "job_7f3a", session_id = "paper-2026")

# Poll it again to find out where it got to. Here it has already finished.
sd_wait(job, verbose = FALSE)$status

## ----errors, error = TRUE-----------------------------------------------------
try({
broken <- sd_client(
  api_key = "sk_x",
  transport = function(req) {
    httr2::response(
      status_code = 415,
      headers = list(`Content-Type` = "application/json"),
      body = charToRaw('{"detail": "Unsupported file type: .doc"}')
    )
  }
)
tex <- tempfile(fileext = ".doc")
writeBin(charToRaw("x"), tex)
sd_upload(tex, broken)
})

