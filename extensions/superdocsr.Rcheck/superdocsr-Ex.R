pkgname <- "superdocsr"
source(file.path(R.home("share"), "R", "examples-header.R"))
options(warn = 1)
base::assign(".ExTimings", "superdocsr-Ex.timings", pos = 'CheckExEnv')
base::cat("name\tuser\tsystem\telapsed\n", file=base::get(".ExTimings", pos = 'CheckExEnv'))
base::assign(".format_ptime",
function(x) {
  if(!is.na(x[4L])) x[1L] <- x[1L] + x[4L]
  if(!is.na(x[5L])) x[2L] <- x[2L] + x[5L]
  options(OutDec = '.')
  format(x[1L:3L], digits = 7L)
},
pos = 'CheckExEnv')

### * </HEADER>
library('superdocsr')

base::assign(".oldSearch", base::search(), pos = 'CheckExEnv')
base::assign(".old_wd", base::getwd(), pos = 'CheckExEnv')
cleanEx()
nameEx("sd_approve")
### * sd_approve

flush(stderr()); flush(stdout())

base::assign(".ptime", proc.time(), pos = "CheckExEnv")
### Name: sd_approve
### Title: Approve or deny proposed changes
### Aliases: sd_approve

### ** Examples

## Not run: 
##D changes <- sd_changes(job)
##D keep <- changes$change_id[changes$operation == "edit"]
##D job <- sd_approve(job, keep)
##D job <- sd_wait(job)
## End(Not run)



base::assign(".dptime", (proc.time() - get(".ptime", pos = "CheckExEnv")), pos = "CheckExEnv")
base::cat("sd_approve", base::get(".format_ptime", pos = 'CheckExEnv')(get(".dptime", pos = "CheckExEnv")), "\n", file=base::get(".ExTimings", pos = 'CheckExEnv'), append=TRUE, sep="\t")
cleanEx()
nameEx("sd_awaiting_kind")
### * sd_awaiting_kind

flush(stderr()); flush(stdout())

base::assign(".ptime", proc.time(), pos = "CheckExEnv")
### Name: sd_awaiting_kind
### Title: What a paused job is waiting for
### Aliases: sd_awaiting_kind

### ** Examples

client <- sd_client(api_key = "sk_x", transport = function(req) req)
job <- sd_job(client, "j1", "paper-2026", status = "in_progress")
sd_awaiting_kind(job)



base::assign(".dptime", (proc.time() - get(".ptime", pos = "CheckExEnv")), pos = "CheckExEnv")
base::cat("sd_awaiting_kind", base::get(".format_ptime", pos = 'CheckExEnv')(get(".dptime", pos = "CheckExEnv")), "\n", file=base::get(".ExTimings", pos = 'CheckExEnv'), append=TRUE, sep="\t")
cleanEx()
nameEx("sd_budget")
### * sd_budget

flush(stderr()); flush(stdout())

base::assign(".ptime", proc.time(), pos = "CheckExEnv")
### Name: sd_budget
### Title: Cap what a client may spend
### Aliases: sd_budget

### ** Examples

sd_budget()
sd_budget(small_sample = TRUE)
sd_budget(max_operations = 3, max_pages = 40)



base::assign(".dptime", (proc.time() - get(".ptime", pos = "CheckExEnv")), pos = "CheckExEnv")
base::cat("sd_budget", base::get(".format_ptime", pos = 'CheckExEnv')(get(".dptime", pos = "CheckExEnv")), "\n", file=base::get(".ExTimings", pos = 'CheckExEnv'), append=TRUE, sep="\t")
cleanEx()
nameEx("sd_cancel")
### * sd_cancel

flush(stderr()); flush(stdout())

base::assign(".ptime", proc.time(), pos = "CheckExEnv")
### Name: sd_cancel
### Title: Cancel a running job
### Aliases: sd_cancel

### ** Examples

## Not run: 
##D sd_cancel(job)
## End(Not run)



base::assign(".dptime", (proc.time() - get(".ptime", pos = "CheckExEnv")), pos = "CheckExEnv")
base::cat("sd_cancel", base::get(".format_ptime", pos = 'CheckExEnv')(get(".dptime", pos = "CheckExEnv")), "\n", file=base::get(".ExTimings", pos = 'CheckExEnv'), append=TRUE, sep="\t")
cleanEx()
nameEx("sd_changes")
### * sd_changes

flush(stderr()); flush(stdout())

base::assign(".ptime", proc.time(), pos = "CheckExEnv")
### Name: sd_changes
### Title: Read the changes a job proposes
### Aliases: sd_changes

### ** Examples

## Not run: 
##D changes <- sd_changes(job)
##D changes[, c("change_id", "operation", "ai_explanation")]
##D sd_approve(job, changes$change_id[changes$operation != "delete"])
## End(Not run)



base::assign(".dptime", (proc.time() - get(".ptime", pos = "CheckExEnv")), pos = "CheckExEnv")
base::cat("sd_changes", base::get(".format_ptime", pos = 'CheckExEnv')(get(".dptime", pos = "CheckExEnv")), "\n", file=base::get(".ExTimings", pos = 'CheckExEnv'), append=TRUE, sep="\t")
cleanEx()
nameEx("sd_client")
### * sd_client

flush(stderr()); flush(stdout())

base::assign(".ptime", proc.time(), pos = "CheckExEnv")
### Name: sd_client
### Title: Create a SuperDocs client
### Aliases: sd_client

### ** Examples

# Offline client: a transport that answers every request the same way.
fake <- function(req) {
  httr2::response(
    status_code = 200,
    headers = list(`Content-Type` = "application/json"),
    body = charToRaw("[]")
  )
}
client <- sd_client(api_key = "sk_not_a_real_key", transport = fake)
client

## Not run: 
##D # Real client, key from the environment.
##D client <- sd_client()
##D sd_verify_key(client)
## End(Not run)



base::assign(".dptime", (proc.time() - get(".ptime", pos = "CheckExEnv")), pos = "CheckExEnv")
base::cat("sd_client", base::get(".format_ptime", pos = 'CheckExEnv')(get(".dptime", pos = "CheckExEnv")), "\n", file=base::get(".ExTimings", pos = 'CheckExEnv'), append=TRUE, sep="\t")
cleanEx()
nameEx("sd_continue")
### * sd_continue

flush(stderr()); flush(stdout())

base::assign(".ptime", proc.time(), pos = "CheckExEnv")
### Name: sd_continue
### Title: Answer a large-edit continue prompt
### Aliases: sd_continue

### ** Examples

## Not run: 
##D if (identical(sd_awaiting_kind(job), "continue_prompt")) {
##D   message(job$metadata$continue_prompt$message)
##D   job <- sd_continue(job, proceed = TRUE)
##D }
## End(Not run)



base::assign(".dptime", (proc.time() - get(".ptime", pos = "CheckExEnv")), pos = "CheckExEnv")
base::cat("sd_continue", base::get(".format_ptime", pos = 'CheckExEnv')(get(".dptime", pos = "CheckExEnv")), "\n", file=base::get(".ExTimings", pos = 'CheckExEnv'), append=TRUE, sep="\t")
cleanEx()
nameEx("sd_deny")
### * sd_deny

flush(stderr()); flush(stdout())

base::assign(".ptime", proc.time(), pos = "CheckExEnv")
### Name: sd_deny
### Title: Deny proposed changes
### Aliases: sd_deny

### ** Examples

## Not run: 
##D job <- sd_deny(job, "ch_3", feedback = "Keep the hedge; the effect is not significant.")
## End(Not run)



base::assign(".dptime", (proc.time() - get(".ptime", pos = "CheckExEnv")), pos = "CheckExEnv")
base::cat("sd_deny", base::get(".format_ptime", pos = 'CheckExEnv')(get(".dptime", pos = "CheckExEnv")), "\n", file=base::get(".ExTimings", pos = 'CheckExEnv'), append=TRUE, sep="\t")
cleanEx()
nameEx("sd_document")
### * sd_document

flush(stderr()); flush(stdout())

base::assign(".ptime", proc.time(), pos = "CheckExEnv")
### Name: sd_document
### Title: A document open in a SuperDocs session
### Aliases: sd_document

### ** Examples

client <- sd_client(api_key = "sk_x", transport = function(req) req)
sd_document(client, session_id = "paper-2026")



base::assign(".dptime", (proc.time() - get(".ptime", pos = "CheckExEnv")), pos = "CheckExEnv")
base::cat("sd_document", base::get(".format_ptime", pos = 'CheckExEnv')(get(".dptime", pos = "CheckExEnv")), "\n", file=base::get(".ExTimings", pos = 'CheckExEnv'), append=TRUE, sep="\t")
cleanEx()
nameEx("sd_edit")
### * sd_edit

flush(stderr()); flush(stdout())

base::assign(".ptime", proc.time(), pos = "CheckExEnv")
### Name: sd_edit
### Title: Ask SuperDocs to edit a document
### Aliases: sd_edit

### ** Examples

## Not run: 
##D doc <- sd_upload("manuscript.docx")
##D job <- sd_edit(doc, "Tighten the abstract to 150 words without losing the result.")
##D job <- sd_wait(job)
##D sd_changes(job)
## End(Not run)



base::assign(".dptime", (proc.time() - get(".ptime", pos = "CheckExEnv")), pos = "CheckExEnv")
base::cat("sd_edit", base::get(".format_ptime", pos = 'CheckExEnv')(get(".dptime", pos = "CheckExEnv")), "\n", file=base::get(".ExTimings", pos = 'CheckExEnv'), append=TRUE, sep="\t")
cleanEx()
nameEx("sd_export")
### * sd_export

flush(stderr()); flush(stdout())

base::assign(".ptime", proc.time(), pos = "CheckExEnv")
### Name: sd_export
### Title: Export a document to a file
### Aliases: sd_export

### ** Examples

## Not run: 
##D sd_export(doc, "docx", "manuscript-revised.docx")
##D sd_export(doc, "pdf", "manuscript-revised.pdf",
##D   options = list(paper_size = "A4", margins = "narrow")
##D )
## End(Not run)



base::assign(".dptime", (proc.time() - get(".ptime", pos = "CheckExEnv")), pos = "CheckExEnv")
base::cat("sd_export", base::get(".format_ptime", pos = 'CheckExEnv')(get(".dptime", pos = "CheckExEnv")), "\n", file=base::get(".ExTimings", pos = 'CheckExEnv'), append=TRUE, sep="\t")
cleanEx()
nameEx("sd_job")
### * sd_job

flush(stderr()); flush(stdout())

base::assign(".ptime", proc.time(), pos = "CheckExEnv")
### Name: sd_job
### Title: A SuperDocs async job
### Aliases: sd_job

### ** Examples

client <- sd_client(api_key = "sk_x", transport = function(req) req)
sd_job(client, job_id = "550e8400", session_id = "paper-2026")



base::assign(".dptime", (proc.time() - get(".ptime", pos = "CheckExEnv")), pos = "CheckExEnv")
base::cat("sd_job", base::get(".format_ptime", pos = 'CheckExEnv')(get(".dptime", pos = "CheckExEnv")), "\n", file=base::get(".ExTimings", pos = 'CheckExEnv'), append=TRUE, sep="\t")
cleanEx()
nameEx("sd_knit")
### * sd_knit

flush(stderr()); flush(stdout())

base::assign(".ptime", proc.time(), pos = "CheckExEnv")
### Name: sd_knit
### Title: Render an R Markdown paper and edit it through SuperDocs
### Aliases: sd_knit

### ** Examples

## Not run: 
##D result <- sd_knit(
##D   "paper.Rmd",
##D   "Tighten the discussion section; keep every citation exactly as written.",
##D   export_path = "paper-revised.docx"
##D )
##D result$changes
## End(Not run)



base::assign(".dptime", (proc.time() - get(".ptime", pos = "CheckExEnv")), pos = "CheckExEnv")
base::cat("sd_knit", base::get(".format_ptime", pos = 'CheckExEnv')(get(".dptime", pos = "CheckExEnv")), "\n", file=base::get(".ExTimings", pos = 'CheckExEnv'), append=TRUE, sep="\t")
cleanEx()
nameEx("sd_ops_used")
### * sd_ops_used

flush(stderr()); flush(stdout())

base::assign(".ptime", proc.time(), pos = "CheckExEnv")
### Name: sd_ops_used
### Title: Operations spent and left on a client
### Aliases: sd_ops_used sd_ops_remaining

### ** Examples

client <- sd_client(api_key = "sk_x", transport = function(req) req)
sd_ops_used(client)
sd_ops_remaining(client)



base::assign(".dptime", (proc.time() - get(".ptime", pos = "CheckExEnv")), pos = "CheckExEnv")
base::cat("sd_ops_used", base::get(".format_ptime", pos = 'CheckExEnv')(get(".dptime", pos = "CheckExEnv")), "\n", file=base::get(".ExTimings", pos = 'CheckExEnv'), append=TRUE, sep="\t")
cleanEx()
nameEx("sd_review_console")
### * sd_review_console

flush(stderr()); flush(stdout())

base::assign(".ptime", proc.time(), pos = "CheckExEnv")
### Name: sd_review_console
### Title: Review helpers for sd_knit()
### Aliases: sd_review_console sd_review_none

### ** Examples

client <- sd_client(api_key = "sk_x", transport = function(req) req)
job <- sd_job(client, "j1", "paper-2026")
sd_review_none(sd_changes(job, refresh = FALSE))



base::assign(".dptime", (proc.time() - get(".ptime", pos = "CheckExEnv")), pos = "CheckExEnv")
base::cat("sd_review_console", base::get(".format_ptime", pos = 'CheckExEnv')(get(".dptime", pos = "CheckExEnv")), "\n", file=base::get(".ExTimings", pos = 'CheckExEnv'), append=TRUE, sep="\t")
cleanEx()
nameEx("sd_upload")
### * sd_upload

flush(stderr()); flush(stdout())

base::assign(".ptime", proc.time(), pos = "CheckExEnv")
### Name: sd_upload
### Title: Upload a document into a SuperDocs session
### Aliases: sd_upload

### ** Examples

## Not run: 
##D client <- sd_client()
##D doc <- sd_upload("manuscript.docx", client)
##D doc
## End(Not run)



base::assign(".dptime", (proc.time() - get(".ptime", pos = "CheckExEnv")), pos = "CheckExEnv")
base::cat("sd_upload", base::get(".format_ptime", pos = 'CheckExEnv')(get(".dptime", pos = "CheckExEnv")), "\n", file=base::get(".ExTimings", pos = 'CheckExEnv'), append=TRUE, sep="\t")
cleanEx()
nameEx("sd_verify_key")
### * sd_verify_key

flush(stderr()); flush(stdout())

base::assign(".ptime", proc.time(), pos = "CheckExEnv")
### Name: sd_verify_key
### Title: Confirm an API key works
### Aliases: sd_verify_key

### ** Examples

## Not run: 
##D sd_verify_key(sd_client())
## End(Not run)



base::assign(".dptime", (proc.time() - get(".ptime", pos = "CheckExEnv")), pos = "CheckExEnv")
base::cat("sd_verify_key", base::get(".format_ptime", pos = 'CheckExEnv')(get(".dptime", pos = "CheckExEnv")), "\n", file=base::get(".ExTimings", pos = 'CheckExEnv'), append=TRUE, sep="\t")
cleanEx()
nameEx("sd_wait")
### * sd_wait

flush(stderr()); flush(stdout())

base::assign(".ptime", proc.time(), pos = "CheckExEnv")
### Name: sd_wait
### Title: Wait for a job to reach a stopping point
### Aliases: sd_wait

### ** Examples

## Not run: 
##D job <- sd_wait(sd_edit(doc, "Rewrite the conclusion."), timeout = 600)
##D sd_awaiting_kind(job)
## End(Not run)



base::assign(".dptime", (proc.time() - get(".ptime", pos = "CheckExEnv")), pos = "CheckExEnv")
base::cat("sd_wait", base::get(".format_ptime", pos = 'CheckExEnv')(get(".dptime", pos = "CheckExEnv")), "\n", file=base::get(".ExTimings", pos = 'CheckExEnv'), append=TRUE, sep="\t")
### * <FOOTER>
###
cleanEx()
options(digits = 7L)
base::cat("Time elapsed: ", proc.time() - base::get("ptime", pos = 'CheckExEnv'),"\n")
grDevices::dev.off()
###
### Local variables: ***
### mode: outline-minor ***
### outline-regexp: "\\(> \\)?### [*]+" ***
### End: ***
quit('no')
