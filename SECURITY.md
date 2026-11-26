# Security and privacy

## Deployment

The backend has no client authentication, per-user authorization, or built-in
TLS. Anyone able to reach it can submit reviews/jobs, read known job results,
and request cancellation. Job IDs are not authentication secrets.

Use loopback for a single-computer setup. For another trusted computer, restrict
firewall access and configure appropriate encrypted transport/access control.
A private LAN is not itself authentication. Do not expose FastAPI or LM Studio
directly to the public internet. The documented proxy URL is a configuration
example, not a bundled secure gateway.

## Image and location handling

- The plug-in exports JPEG previews and sends them with filenames and optional
  catalog location. Local job state can include original paths and catalog IDs.
- The server can read GPS from JPEG EXIF. If catalog context is incomplete,
  Nominatim can receive coordinates to fill missing location fields. A local
  model does not imply that the pipeline makes no external network requests.
- Setting `geocoding.reverse_url` to an empty string in local config disables
  reverse lookup. Catalog context can still be supplied. Missing GPS or failed
  lookup does not itself prevent review.
- After location resolution, the model image passes through removal of certain
  JPEG EXIF/IPTC segments. This is best-effort metadata sanitation, not a
  universal anonymity guarantee. Original local/server preview files remain
  available and can contain GPS. Visible scene content may identify a location.
- The configured model endpoint receives image/prompt/context data. No cloud
  fallback adapter is implemented, but a remote endpoint still transmits data
  off-machine. Runtime/service terms apply.
- Dry-run prevents Lightroom flag writes, not model requests, geocoding,
  storage, or logging.

## Retention and diagnostics

Backend jobs default to `server/jobs/`; the plug-in defaults to
Documents/StockReviewAI Jobs. These contain previews, manifests, checkpoints,
results, and lifecycle state. There is no automatic retention period or
application-level encryption. Protect these folders and backups with OS access
controls and encryption where appropriate.

The default server log is `server/StockReviewAI.server.log`; the result log is
`StockReviewAI.log` inside the plug-in folder. Assessments, reasons, location
context, paths, provider usage/model IDs, and validation excerpts can be logged.
The optional benchmark produces additional artifacts. Hashes or excerpt limits
do not make all diagnostic content safe to publish.

Cancellation/closure retains files and does not restore flags. Clearing the
plug-in review log does not erase server jobs, backend logs, or backup copies.
Resolve jobs and stop the backend before manually maintaining job storage.

## Configuration and secrets

Keep machine-local values in ignored `server/config.yaml`. The loader does not
provide automatic `.env` loading or a general environment-variable override.
Never put credentials into committed examples, URL query strings, screenshots,
or public diagnostics. This implementation has no general provider API-key
setting; do not assume such a setting exists because another project uses one.

Ignore rules do not erase earlier commits. If a credential is exposed, rotate
it and assess its history separately. Do not attach catalogs, previews, raw job
folders, location-bearing logs, or model output to public issues without review.

## Verification limits and reporting

Documentation preparation preserves dependency requirements and installed
versions. It does not certify vulnerability clearance or perform a comprehensive
security assessment. Review installed libraries before processing untrusted
images or broadening deployment.

Use GitHub's private Report a vulnerability option if enabled. Otherwise request
a private contact channel in an issue without exploit details, credentials, or
personal data. Share affected version/commit, impact, and a sanitized reproduction
over that channel. There is no guaranteed response time or separate maintenance
commitment for old snapshots.
