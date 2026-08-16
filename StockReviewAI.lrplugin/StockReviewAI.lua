local LrApplication = import "LrApplication"
local LrDate = import "LrDate"
local LrDialogs = import "LrDialogs"
local LrExportSession = import "LrExportSession"
local LrFileUtils = import "LrFileUtils"
local LrPathUtils = import "LrPathUtils"
local LrProgressScope = import "LrProgressScope"
local LrTasks = import "LrTasks"

local AiClient = require "AiClient"
local Settings = require "Settings"
local Utils = require "Utils"

local Plugin = {}

local LOCATION_METADATA_FIELDS = {
    { requestKey = "country", metadataKey = "country" },
    {
        requestKey = "state_or_province",
        metadataKey = "stateProvince",
    },
    { requestKey = "city", metadataKey = "city" },
    { requestKey = "sublocation", metadataKey = "location" },
}

local function trimmedString(value)
    if type(value) ~= "string" then
        return nil
    end

    local trimmed = value:match("^%s*(.-)%s*$")
    if trimmed == "" then
        return nil
    end
    return trimmed
end

local function readLocationContext(photo)
    local ok, resultOrError = pcall(function()
        local context = {}
        local hasValue = false

        for _, field in ipairs(LOCATION_METADATA_FIELDS) do
            local value = trimmedString(
                photo:getFormattedMetadata(field.metadataKey)
            )
            if value then
                context[field.requestKey] = value
                hasValue = true
            end
        end

        return hasValue and context or nil
    end)

    if not ok then
        return nil, tostring(resultOrError)
    end
    return resultOrError
end

local function selectedPhotos(catalog)
    local target = catalog:getTargetPhotos()
    if target and #target > 0 then
        return target
    end

    local one = catalog:getTargetPhoto()
    if one then
        return { one }
    end

    return {}
end

local function makeTempDir()
    local dir = LrPathUtils.child(LrPathUtils.getStandardFilePath("temp"), "StockReviewAI")
    if not LrFileUtils.exists(dir) then
        LrFileUtils.createAllDirectories(dir)
    end
    return dir
end

local function cleanupOldPreviews()
    local dir = makeTempDir()
    for filePath in LrFileUtils.files(dir) do
        if LrPathUtils.leafName(filePath):match("^stock%-review%-ai%-") then
            local path = LrPathUtils.child(dir, LrPathUtils.leafName(filePath))
            if LrFileUtils.exists(path) then
                LrFileUtils.delete(path)
            end
        end
    end
end

local function generatePreview(photo, settings, index)
    local tempDir = makeTempDir()
    local uniqueName = "stock-review-ai-" .. tostring(math.floor(LrDate.currentTime())) .. "-" .. tostring(index)
    local exportSession = LrExportSession {
        photosToExport = { photo },
        exportSettings = {
            LR_collisionHandling = "overwrite",
            LR_exportServiceProvider = "com.adobe.ag.export.file",
            LR_export_destinationType = "specificFolder",
            LR_export_destinationPathPrefix = tempDir,
            LR_export_useSubfolder = false,
            LR_format = "JPEG",
            LR_jpeg_quality = settings.jpegQuality / 100,
            LR_minimizeEmbeddedMetadata = false,
            LR_removeLocationMetadata = false,
            LR_embeddedMetadataOption = "all",
            LR_outputSharpeningOn = false,
            LR_renamingTokensOn = true,
            LR_tokens = uniqueName,
            LR_size_doConstrain = true,
            LR_size_maxHeight = settings.previewMaxLongEdge,
            LR_size_maxWidth = settings.previewMaxLongEdge,
            LR_size_resizeType = "wh",
            LR_size_units = "pixels",
        },
    }

    local renditionPath
    for _, rendition in exportSession:renditions { stopIfCanceled = true } do
        local success, pathOrMessage = rendition:waitForRender()
        if success then
            renditionPath = pathOrMessage
        else
            return nil, pathOrMessage or "Lightroom could not render a preview for this photo"
        end
    end

    return renditionPath
end

local function shouldApplyFlag(photo, settings)
    if settings.overwriteFlags then
        return true
    end

    local current = photo:getRawMetadata("pickStatus")
    return current == nil or current == 0
end

local function flagValueForResult(result, settings)
    if result.verdict == "YES" then
        return 1, "pick"
    end

    return -1, "reject"
end

local function applyFlag(catalog, photo, result, settings)
    if settings.dryRun then
        return "dry-run"
    end

    if not shouldApplyFlag(photo, settings) then
        return "skipped-existing-flag"
    end

    local flagValue, flagName = flagValueForResult(result, settings)
    catalog:withWriteAccessDo("Stock Review AI Flag", function()
        photo:setRawMetadata("pickStatus", flagValue)
    end)
    return "flagged-" .. flagName
end

local function removeTempFile(path)
    if path and LrFileUtils.exists(path) then
        LrFileUtils.delete(path)
    end
end

local function photoPath(photo)
    return photo:getRawMetadata("path") or photo:getFormattedMetadata("fileName") or "Unknown photo"
end

function Plugin.reviewSelectedPhotos()
    LrTasks.startAsyncTask(function()
        local catalog = LrApplication.activeCatalog()
        local photos = selectedPhotos(catalog)

        if #photos == 0 then
            LrDialogs.message("Stock Review AI", "Select one or more photos in Library before running the review.", "warning")
            return
        end

        local settings = Settings.get()
        cleanupOldPreviews()

        local progress = LrProgressScope {
            title = "Stock Review AI",
            caption = "Preparing review...",
        }

        local summary = {
            reviewed = 0,
            picked = 0,
            rejected = 0,
            skipped = 0,
            errors = 0,
            dryRun = 0,
        }

        for index, photo in ipairs(photos) do
            if progress:isCanceled() then
                break
            end

            local path = photoPath(photo)
            progress:setPortionComplete(index - 1, #photos)
            progress:setCaption("Reviewing " .. tostring(index) .. " of " .. tostring(#photos) .. ": " .. LrPathUtils.leafName(path))

            local previewPath
            local ok, resultOrError, status = LrTasks.pcall(function()
                local generatedPath, previewError = generatePreview(photo, settings, index)
                previewPath = generatedPath
                if not generatedPath then
                    return nil, previewError
                end

                local locationContext, locationError =
                    readLocationContext(photo)
                if locationError then
                    Utils.logger:warn(
                        path
                            .. ": optional location metadata unavailable: "
                            .. locationError
                    )
                end

                local result, aiError = AiClient.reviewPreview(
                    generatedPath,
                    settings,
                    LrPathUtils.leafName(path),
                    locationContext
                )
                if not result then
                    return nil, aiError
                end

                local applyStatus = applyFlag(catalog, photo, result, settings)
                return result, applyStatus
            end)

            removeTempFile(previewPath)

            if ok and resultOrError then
                summary.reviewed = summary.reviewed + 1
                status = status or "reviewed"
                if status == "dry-run" then
                    summary.dryRun = summary.dryRun + 1
                elseif status == "skipped-existing-flag" then
                    summary.skipped = summary.skipped + 1
                elseif status == "flagged-pick" then
                    summary.picked = summary.picked + 1
                elseif status == "flagged-reject" then
                    summary.rejected = summary.rejected + 1
                end

                if settings.writeLog then
                    Utils.appendLog(Utils.formatResultLine(path, resultOrError, status))
                end
            else
                local message = ok and (status or resultOrError) or tostring(resultOrError)
                summary.errors = summary.errors + 1
                Utils.logger:error(path .. ": " .. tostring(message))
                if settings.writeLog then
                    Utils.appendLog(Utils.formatResultLine(path, nil, "error", message))
                end
            end
        end

        progress:setPortionComplete(1)
        progress:done()

        local details = table.concat({
            "Reviewed: " .. tostring(summary.reviewed),
            "Picked: " .. tostring(summary.picked),
            "Rejected: " .. tostring(summary.rejected),
            "Skipped existing flags: " .. tostring(summary.skipped),
            "Dry-run reviewed: " .. tostring(summary.dryRun),
            "Errors: " .. tostring(summary.errors),
            "Log: " .. Utils.logPath(),
        }, "\n")

        LrDialogs.message("Stock Review AI complete", details, summary.errors > 0 and "warning" or "info")
    end)
end

return Plugin
