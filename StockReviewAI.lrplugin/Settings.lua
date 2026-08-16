local LrBinding = import "LrBinding"
local LrDialogs = import "LrDialogs"
local LrFunctionContext = import "LrFunctionContext"
local LrPrefs = import "LrPrefs"
local LrPathUtils = import "LrPathUtils"
local LrView = import "LrView"

local Settings = {}

local PREFS = LrPrefs.prefsForPlugin()

Settings.defaults = {
    endpointUrl = "http://localhost:8080/stockreview/review",
    previewMaxLongEdge = 1280,
    jpegQuality = 75,
    timeoutSeconds = 120,
    dryRun = false,
    minimumYesThreshold = 70,
    writeLog = true,
    overwriteFlags = false,
    jobRoot = LrPathUtils.child(
        LrPathUtils.getStandardFilePath("documents"),
        "StockReviewAI Jobs"
    ),
}

function Settings.ensureDefaults()
    for key, value in pairs(Settings.defaults) do
        if PREFS[key] == nil then
            PREFS[key] = value
        end
    end
end

function Settings.get()
    Settings.ensureDefaults()
    return {
        endpointUrl = PREFS.endpointUrl,
        previewMaxLongEdge = tonumber(PREFS.previewMaxLongEdge) or Settings.defaults.previewMaxLongEdge,
        jpegQuality = tonumber(PREFS.jpegQuality) or Settings.defaults.jpegQuality,
        timeoutSeconds = tonumber(PREFS.timeoutSeconds) or Settings.defaults.timeoutSeconds,
        dryRun = not not PREFS.dryRun,
        minimumYesThreshold = tonumber(PREFS.minimumYesThreshold) or Settings.defaults.minimumYesThreshold,
        writeLog = not not PREFS.writeLog,
        overwriteFlags = not not PREFS.overwriteFlags,
        jobRoot = PREFS.jobRoot or Settings.defaults.jobRoot,
    }
end

local function numberField(viewFactory, props, key, width)
    return viewFactory:edit_field {
        value = LrView.bind(key),
        width_in_chars = width or 8,
        immediate = true,
    }
end

function Settings.showDialog()
    Settings.ensureDefaults()

    LrFunctionContext.callWithContext("StockReviewAISettings", function(context)
        local f = LrView.osFactory()
        local props = LrBinding.makePropertyTable(context)

        props.endpointUrl = PREFS.endpointUrl
        props.previewMaxLongEdge = tostring(PREFS.previewMaxLongEdge)
        props.jpegQuality = tostring(PREFS.jpegQuality)
        props.timeoutSeconds = tostring(PREFS.timeoutSeconds)
        props.dryRun = PREFS.dryRun
        props.minimumYesThreshold = tostring(PREFS.minimumYesThreshold)
        props.writeLog = PREFS.writeLog
        props.overwriteFlags = PREFS.overwriteFlags
        props.jobRoot = PREFS.jobRoot or Settings.defaults.jobRoot

        local contents = f:column {
            bind_to_object = props,
            spacing = f:control_spacing(),

            f:row {
                f:static_text { title = "Server review URL", width = 160 },
                f:edit_field { value = LrView.bind("endpointUrl"), width_in_chars = 52, immediate = true },
            },
            f:row {
                f:static_text { title = "Local job storage", width = 160 },
                f:edit_field { value = LrView.bind("jobRoot"), width_in_chars = 52, immediate = true },
            },
            f:row {
                f:static_text { title = "Preview max long edge", width = 160 },
                numberField(f, props, "previewMaxLongEdge"),
                f:static_text { title = "px" },
            },
            f:row {
                f:static_text { title = "JPEG quality", width = 160 },
                numberField(f, props, "jpegQuality"),
            },
            f:row {
                f:static_text { title = "Timeout", width = 160 },
                numberField(f, props, "timeoutSeconds"),
                f:static_text { title = "seconds" },
            },
            f:row {
                f:static_text { title = "Minimum YES threshold", width = 160 },
                numberField(f, props, "minimumYesThreshold"),
            },
            f:checkbox { title = "Dry run mode (do not change Lightroom flags)", value = LrView.bind("dryRun") },
            f:checkbox { title = "Write AI result log file", value = LrView.bind("writeLog") },
            f:checkbox { title = "Overwrite existing Lightroom Pick/Rejected flags", value = LrView.bind("overwriteFlags") },
        }

        local result = LrDialogs.presentModalDialog {
            title = "Stock Review AI Settings",
            contents = contents,
            actionVerb = "Save",
        }

        if result == "ok" then
            PREFS.endpointUrl = props.endpointUrl
            PREFS.previewMaxLongEdge = math.max(256, tonumber(props.previewMaxLongEdge) or Settings.defaults.previewMaxLongEdge)
            PREFS.jpegQuality = math.max(1, math.min(100, tonumber(props.jpegQuality) or Settings.defaults.jpegQuality))
            PREFS.timeoutSeconds = math.max(5, tonumber(props.timeoutSeconds) or Settings.defaults.timeoutSeconds)
            PREFS.dryRun = not not props.dryRun
            PREFS.minimumYesThreshold = math.max(0, math.min(100, tonumber(props.minimumYesThreshold) or Settings.defaults.minimumYesThreshold))
            PREFS.writeLog = not not props.writeLog
            PREFS.overwriteFlags = not not props.overwriteFlags
            PREFS.jobRoot = props.jobRoot ~= "" and props.jobRoot or Settings.defaults.jobRoot
        end
    end)
end

Settings.ensureDefaults()

return Settings
