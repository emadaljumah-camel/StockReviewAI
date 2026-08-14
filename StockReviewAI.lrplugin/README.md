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

The threshold is sent to the server so the returned `verdict` remains the single source of truth for flagging.

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
