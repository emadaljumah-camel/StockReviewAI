# StockReviewAI Lightroom Plugin

StockReviewAI reviews the currently selected Lightroom Classic photos with a local FastAPI review server, then maps the server verdict to Lightroom flags:

- `YES` => Pick flag
- `NO` => Rejected flag

It creates temporary JPEG previews only. It does not modify original files and does not move folders.

## Project Layout

```text
StockReviewAI/
├── StockReviewAI.lrplugin/
└── server/
```

## Lightroom Plugin Files

- `Info.lua` - Lightroom plugin manifest and Library > Plug-in Extras menu items
- `StockReviewAI.lua` - selected-photo workflow, preview generation, progress, and flagging
- `AiClient.lua` - base64 JPEG upload to the StockReviewAI FastAPI server and response validation
- `Json.lua` - bundled Lua 5.1 JSON encoder/decoder used when Lightroom has no JSON namespace
- `Prompt.lua` - legacy stock-review prompt kept for backwards compatibility
- `Settings.lua` - plugin preferences dialog backed by `LrPrefs`
- `Utils.lua` - logging and shared helpers
- `ReviewSelected.lua`, `OpenSettings.lua`, `ClearLog.lua` - menu command entry points

## Lightroom Install

1. Open Lightroom Classic.
2. Go to `File > Plug-in Manager`.
3. Click `Add`.
4. Select this folder: `StockReviewAI\StockReviewAI.lrplugin`.
5. Confirm the plugin is enabled.

## StockReviewAI Server

The Lightroom plugin sends previews to:

```text
http://localhost:8080/stockreview/review
```

The FastAPI server listens locally on:

```text
127.0.0.1:8001
```

The server then calls LM Studio at the endpoint configured in `server/config.yaml`. The default is:

```text
http://127.0.0.1:1234/v1/chat/completions
```

Create and start the server:

```bat
cd /d D:\Projects\StockReviewAI\server
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8001
```

Or use:

```bat
run_server.bat
```

Configure LM Studio in `server/config.yaml`:

```yaml
lmstudio:
  endpoint: "http://127.0.0.1:1234/v1/chat/completions"
  model: "local-model"
  timeout_seconds: 120
  retries: 2
```

## Plugin Settings

Open:

```text
Library > Plug-in Extras > Open Stock Review AI Settings
```

Set:

- server review URL
- preview max long edge, default `1280`
- JPEG quality, default `75`
- timeout seconds
- dry-run mode
- minimum YES threshold, default `70`
- plugin log writing
- overwrite existing Lightroom flags

LM Studio endpoint, model name, prompt, JSON schema mode, JSON validation, retries, and inference logging are configured in the FastAPI server, not Lightroom.

The threshold is sent as a JSON integer to the server. The AI returns only a
structured assessment; deterministic server code calculates the score and
returns the authoritative `verdict`.

## Decision Policy

Every successful review produces exactly one result:

- `YES` => Pick
- `NO` => Reject

There is no manual-review state. Operational failures leave the existing
Lightroom flag unchanged.

The reviewer permanently assumes that model releases and property releases are
not available. Recognizable people, genuinely release-sensitive property, or
prominent protected IP restrict the photograph to editorial scoring. Distant,
obscured, silhouetted, or incidental people do not count as recognizable.
Ordinary public exteriors do not automatically count as release-sensitive
property, and tiny or incidental logos or artwork do not count as prominent IP.

For unrestricted photographs, the server uses the higher of commercial and
editorial value. A tie selects commercial. Release-sensitive photographs use
editorial value only.

The model also returns structured editorial evidence. The server applies these
deterministic caps:

- no editorial evidence: editorial value is capped at `49`
- distinctive architecture and/or strong visual sense of place only:
  editorial value is capped at `69`, unless verified city- or
  sublocation-level context is available
- at least one strong documentary-evidence category: no evidence-based cap

Specific verified location does not add points. It only removes the `69`
supporting-evidence ceiling when the photograph visibly contains distinctive
architecture or a strong visual sense of place. Country- or region-level
context does not remove the ceiling, and location cannot remove the `49` cap
when no effective editorial evidence exists.

Release-sensitive photographs must have an effective editorial value of at
least `70` and must still meet the configured final-score threshold.

Human-activity evidence is ignored unless the submitted preview shows clearly
readable activity. Ignored evidence is recorded in the internal server log.

The model reports general composition-defect severity and specific framing
defects. The server applies one deterministic composition policy:

- minor: preserve route value and cap only composition scores above `69`
- major: subtract `20` from composition and `10` from route value, then cap composition at `49` and route value at `64`
- severe: subtract `40` from composition and `20` from route value, then cap composition at `29` and route value at `49`

Scores are clamped at zero. Logged deductions are the actual differences
between raw and effective values, including any additional reduction caused by
a cap.

When the model reports three or more distinct composition defects, the server
deterministically raises an effective severity of `none` or `minor` to `major`.
Existing `major` and `severe` classifications are never lowered.

The model also classifies the main subject as `discrete`,
`environmental_scene`, or `pattern_or_abstract`. The server ignores
`subject_too_small` for environmental and abstract subjects. If sanitization
removes every reported defect, effective severity becomes `none` and no
composition or route penalty is applied. A discrete subject reported as both
`subject_too_small` and surrounded by `excessive_dead_space` is treated as at
least a major composition defect.

Foreground dominance alone does not activate a penalty. When
`foreground_dominates_frame=true`, `dominant_distraction` remains an effective
defect, and at least one additional distinct effective defect exists, the
server raises an effective severity of `none` or `minor` to `major`.

The existing subject-visibility and foreground-dominance fields remain
diagnostic observables. They do not apply separate caps, preventing duplicate
composition penalties.

Reasonably fixable cropping, straightening, perspective correction, edge
cleanup, empty space, and minor imbalance do not qualify for major or severe
penalties. Intentional negative space, asymmetry, foreground framing,
documentary complexity, and unconventional perspective remain valid when they
improve the photograph.

An unidentified exterior building is not release-sensitive merely because it
is prominent or distinctive. Private interiors, clearly visible protected
artwork or designs, trusted metadata restrictions, and clearly identifiable
private businesses central to the frame may be treated as release-sensitive.

The plugin sends trusted textual location context already stored in the
Lightroom catalog when the SDK exposes it. It also preserves embedded GPS in
the temporary preview JPEG. The server can use that GPS with its configured
Nominatim provider to obtain country, state or province, city, and sublocation
when catalog text is absent or incomplete. Committed catalog text takes
priority over reverse-geocoder values.

GPS coordinates are sent only to the configured reverse-geocoding provider.
They are not sent to LM Studio or written to the review log. Reverse geocoding
is cached and rate-limited; failures do not prevent image review. The server
does not infer location from the image, filename, or folder.

Reverse-geocoded context can improve documentary specificity only when the
visible photograph corresponds with it. It does not add points or bypass the
existing editorial-evidence, composition, release, routing, or threshold
rules. Nominatim results contain OpenStreetMap data: © OpenStreetMap
contributors.

The server log records these as separate concepts:

- `release_restriction=unrestricted` or `editorial_only`
- `selected_market_basis=commercial` or `editorial`
- `raw_editorial_score`, `effective_editorial_score`, and
  `editorial_evidence`
- `effective_editorial_evidence` and `ignored_editorial_evidence`
- `raw_composition_score`, `effective_composition_score`,
  `raw_composition_defect_severity`,
  `effective_composition_defect_severity`, `severity_escalation_reason`,
  `main_subject_type`, `raw_composition_defects`,
  `effective_composition_defects`, `ignored_composition_defects`,
  `raw_route_value`, `effective_route_value`, `composition_score_deduction`,
  `route_value_deduction`, and `composition_policy_applied`
- `main_subject_visibility`, `foreground_dominates_frame`, and
  `human_activity_readability`
- `verified_location_context`, `location_context_level`,
  `location_context_source`, `gps_metadata_present`,
  `reverse_geocoder_status`,
  `location_country`, `location_state_or_province`, `location_city`, and
  `location_sublocation`

An unrestricted photograph can use editorial as its selected market basis when
its editorial-value score is higher. This does not mean that the photograph is
restricted to editorial licensing.

The final score uses exact integer arithmetic:

```text
weighted_total =
    technical_score × 35
  + composition_score × 25
  + route_value × 40

final_score = (weighted_total + 50) // 100
```

This produces deterministic half-up rounding. A photograph is picked when its
score is greater than or equal to the configured threshold.

A validated hard rejection overrides the score and is limited to:

- clearly missed focus on the main subject
- destructive and unintended motion blur
- irrecoverable exposure clipping
- severe artifacts that make the photograph unusable
- no sufficiently identifiable or meaningful subject

Weak commercial value, weak editorial value, generic subject matter, and
ordinary snapshot quality affect the component scores; they are not hard
rejections. Fatal defects must be clearly visible in the supplied JPEG preview
at its configured size and quality.

## Independent Image Evaluation

Stock Review AI evaluates every photograph independently. It does not group
similar images or select the strongest frame from a sequence. Two similar
photographs may both be picked when each independently meets the configured
threshold.

PhotoPick AI remains responsible for comparing related photographs and
selecting the strongest image from a group.

## Caddy

Use this route:

```caddy
handle_path /stockreview/* {
    reverse_proxy 127.0.0.1:8001
}
```

With this route, Lightroom should use:

```text
http://localhost:8080/stockreview/review
```

## NSSM Service

Install as:

```text
Name: StockReviewAI
Path: D:\Projects\StockReviewAI\server\.venv\Scripts\python.exe
Arguments: -m uvicorn main:app --host 127.0.0.1 --port 8001
Startup directory: D:\Projects\StockReviewAI\server
```

## Use

1. Select one or more photos in Lightroom Library.
2. Run `Library > Plug-in Extras > Review Selected Photos for Stock AI`.
3. Watch the progress dialog.
4. Review the final summary and log.

The log is written beside the plugin as:

```text
StockReviewAI.log
```

Use `Library > Plug-in Extras > Clear AI Review Log` to delete it.

## Testing Checklist

1. Enable dry-run mode and test one selected photo.
2. Confirm no Lightroom flag changes in dry-run.
3. Disable dry-run and test one selected photo.
4. Confirm `YES` photos become Pick and `NO` photos become Rejected.
5. Test 10 mixed photos.
6. Disable overwrite and confirm existing Pick/Rejected flags are preserved.
7. Stop the FastAPI server or LM Studio and confirm errors are logged without flag changes.
8. Confirm original files are not modified.

## Notes

- Flag changes are performed inside `catalog:withWriteAccessDo`.
- Work runs through `LrTasks.startAsyncTask`.
- Temporary previews are exported JPEG renditions with max long edge constraints.
- Invalid server or model JSON results are logged and leave the photo unchanged.
