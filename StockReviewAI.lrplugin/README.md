# StockReviewAI Lightroom plug-in

Submit photo previews for AI stock suitability review and later map
**YES → Pick** and **NO → Rejected**. Originals are not deleted, moved, or
edited. The plug-in changes Lightroom `pickStatus` only.

Start with the [root setup guide](../README.md#setup) and
[one-photo dry-run](../README.md#one-photo-walkthrough).

## Install and settings

Add this complete folder through **File > Plug-in Manager > Add**.
Select **02 Stock Review AI**, then **Open Settings...**.

For a direct server on this computer, set **Endpoint URL** to
`http://127.0.0.1:8001/review`. The initial
`http://localhost:8080/stockreview/review` assumes an external reverse proxy;
that proxy is not started by this project. The plug-in removes trailing
`/review` to derive job paths. Saved preferences override defaults.

| Preference | Initial value |
|---|---|
| Preview max long edge | 1536 pixels |
| JPEG quality | 75 |
| Timeout | 120 seconds |
| Minimum YES threshold | 70 |
| Dry run | Off |
| Write AI result log file | On |
| Overwrite existing Pick/Rejected flags | Off |
| Local job root | Documents/StockReviewAI Jobs |

Existing flags are protected at application time unless overwrite was enabled
in the submitted job. Dry-run still invokes export, upload, inference, and
optional logging; only flag writes are suppressed.

## Commands

All three are under **Library > Plug-in Extras**.

### Review Selected Photos for Stock AI

Select test photos and run the command. The plug-in exports previews, saves a
manifest/active pointer, uploads binary JPEGs, and starts a background job.
Wait for queued acknowledgment before disconnecting. No flags change during
submission. Only one unresolved active client job is tracked.

The saved manifest includes dry-run/overwrite choices, threshold, photo/catalog
identities, and endpoint. Changing preferences later does not change these
options in an existing job.

### Check Job Status / Apply Results

Recover an interrupted submission or check the saved server job. Running jobs
show progress; failed/interrupted jobs offer Resume Job. Completed results are
downloaded and cached, validated, and processed against original catalog photos.

**This is an application command, not a read-only preview.** A completed
non-dry-run job can immediately set flags without a per-photo approval dialog.
The summary counts reviewed, picked, rejected, protected/skipped, and dry-run
photos. Wrong catalogs, missing photos, and invalid results are refused.

Applied state is saved at the job level after processing. No per-photo
application checkpoint or automatic original-flag restoration is implemented.
On an error, cached results remain, but inspect possibly changed flags before
retry. A write-access scope is not a documented guarantee of batch rollback.

A completed dry-run is treated as applied/resolved for job lifecycle purposes.
To change flags afterward, disable dry-run and submit a new job.

### Cancel Active Job

Confirms cancellation and calls the server, then closes local active state after
acknowledgment. Completed jobs can be closed without applying their results.
Cancelled jobs cannot be resumed; start a new job if needed.

Previews and results remain. In-flight inference may take time to stop.
Cancellation does not undo applied flags. Cancelling export/upload progress
alone also does not establish that queued server work was cancelled.

## Logs, location, and recovery

The result log is `StockReviewAI.log` inside this plug-in folder. Plug-in
Manager offers **Clear AI Review Log**; this does not erase backend logs, jobs,
or change flags.

Catalog location can be supplied to the server. Missing context may be enriched
using preview GPS and a Nominatim lookup. Uploaded previews can retain GPS even
though the model-bound image passes through metadata stripping later.

Keep local job folders until work is resolved. See
[recovery](../README.md#recovery-and-rollback),
[updates](../README.md#updating),
[architecture](../docs/ARCHITECTURE.md), and [privacy](../SECURITY.md).
