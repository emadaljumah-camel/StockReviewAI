local LrApplication = import "LrApplication"
local LrDate = import "LrDate"
local LrDialogs = import "LrDialogs"
local LrExportSession = import "LrExportSession"
local LrPathUtils = import "LrPathUtils"
local LrProgressScope = import "LrProgressScope"
local LrTasks = import "LrTasks"

local JobClient = require "JobClient"
local JobState = require "JobState"
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

local function exportKey(photo, fallback)
    local identifier = photo.localIdentifier
    if identifier ~= nil then
        return "local-" .. tostring(identifier)
    end
    local uuid = trimmedString(photo:getRawMetadata("uuid"))
    return uuid or ("position-" .. tostring(fallback))
end

local function generatePreviews(photos, settings, previewDir, progress)
    local exportSession = LrExportSession {
        photosToExport = photos,
        exportSettings = {
            LR_collisionHandling = "rename",
            LR_exportServiceProvider = "com.adobe.ag.export.file",
            LR_export_destinationType = "specificFolder",
            LR_export_destinationPathPrefix = previewDir,
            LR_export_useSubfolder = false,
            LR_format = "JPEG",
            LR_jpeg_quality = settings.jpegQuality / 100,
            LR_minimizeEmbeddedMetadata = false,
            LR_removeLocationMetadata = false,
            LR_embeddedMetadataOption = "all",
            LR_outputSharpeningOn = false,
            LR_renamingTokensOn = true,
            LR_initialSequenceNumber = 1,
            LR_tokens = "stock-review-ai-{{naming_sequenceNumber_5Digits}}",
            LR_size_doConstrain = true,
            LR_size_maxHeight = settings.previewMaxLongEdge,
            LR_size_maxWidth = settings.previewMaxLongEdge,
            LR_size_resizeType = "wh",
            LR_size_units = "pixels",
        },
    }

    local previews = {}
    local rendered = 0
    for _, rendition in exportSession:renditions { stopIfCanceled = true } do
        if progress:isCanceled() then
            return nil, "Export canceled. No job was submitted."
        end
        local success, pathOrMessage = rendition:waitForRender()
        if not success then
            return nil, pathOrMessage or "Lightroom could not render a preview for this photo"
        end
        rendered = rendered + 1
        local key = exportKey(rendition.photo, rendered)
        if previews[key] then
            return nil, "Lightroom returned duplicate previews for a selected photo"
        end
        previews[key] = pathOrMessage
        progress:setPortionComplete(rendered * 45 / #photos, 100)
    end

    if rendered ~= #photos then
        return nil, "Lightroom did not render every selected photo"
    end
    return previews
end

local function photoPath(photo)
    return photo:getRawMetadata("path") or photo:getFormattedMetadata("fileName") or "Unknown photo"
end

function Plugin.reviewSelectedPhotos()
    LrTasks.startAsyncTask(function()
        math.randomseed(os.time())
        local catalog = LrApplication.activeCatalog()
        local photos = selectedPhotos(catalog)

        if #photos == 0 then
            LrDialogs.message("Stock Review AI", "Select one or more photos in Library before running the review.", "warning")
            return
        end

        local settings = Settings.get()
        local active, activeError = JobState.loadActive(settings)
        if activeError then
            LrDialogs.message("Stock Review AI", activeError, "critical")
            return
        end
        if active and not active.applied and not active.closed then
            LrDialogs.message(
                "Stock Review AI job pending",
                "Job " .. Utils.safeString(active.job_id)
                    .. " has not been applied. Use 'Check Job Status / Apply Results' first.",
                "warning"
            )
            return
        end

        local clientJobId = os.date("%Y%m%d-%H%M%S") .. "-"
            .. tostring(math.random(100000, 999999))
        local jobFolder = JobState.createFolder(settings, clientJobId)
        local previewDir = LrPathUtils.child(jobFolder, "previews")

        local progress = LrProgressScope {
            title = "Stock Review AI",
            caption = "Preparing review...",
        }

        local manifest = {
            schema_version = 1,
            client_job_id = clientJobId,
            plugin_version = "1.1.0",
            catalog_path = Utils.safeString(catalog:getPath()),
            endpoint_url = settings.endpointUrl,
            dry_run = settings.dryRun,
            overwrite_flags = settings.overwriteFlags,
            write_log = settings.writeLog,
            photos = {},
            server_photos = {},
        }

        local exportStartedAt = LrDate.currentTime()
        progress:setCaption("Exporting " .. tostring(#photos) .. " previews...")
        local previews, previewError = generatePreviews(photos, settings, previewDir, progress)
        if not previews then
            progress:done()
            LrDialogs.message("Stock Review AI export error", previewError, "critical")
            return
        end
        if settings.writeLog then
            Utils.appendLog(
                os.date("%Y-%m-%d %H:%M:%S")
                    .. "\texport"
                    .. "\tphotos=" .. tostring(#photos)
                    .. "\tseconds=" .. string.format("%.3f", LrDate.currentTime() - exportStartedAt)
                    .. "\tmax_long_edge=" .. tostring(settings.previewMaxLongEdge)
            )
        end

        for index, photo in ipairs(photos) do
            if progress:isCanceled() then
                progress:done()
                return
            end

            local path = photoPath(photo)
            local generatedPath = previews[exportKey(photo, index)]
            if not generatedPath then
                progress:done()
                LrDialogs.message("Stock Review AI export error", "A preview could not be matched to " .. LrPathUtils.leafName(path), "critical")
                return
            end
            local locationContext, locationError = readLocationContext(photo)
            if locationError then
                Utils.logger:warn(path .. ": optional location metadata unavailable: " .. locationError)
            end
            local rawUuid = photo:getRawMetadata("uuid")
            local photoId = trimmedString(rawUuid)
                or ("photo-" .. tostring(photo.localIdentifier or index))
            local filename = LrPathUtils.leafName(path)
            manifest.photos[#manifest.photos + 1] = {
                photo_id = photoId,
                catalog_uuid = trimmedString(rawUuid),
                local_identifier = tostring(photo.localIdentifier or ""),
                filename = filename,
                original_path = path,
                preview_path = generatedPath,
            }
            manifest.server_photos[#manifest.server_photos + 1] = {
                photo_id = photoId,
                catalog_uuid = trimmedString(rawUuid),
                filename = filename,
                threshold = math.floor(settings.minimumYesThreshold),
                location_context = locationContext,
            }
        end

        local manifestPath = LrPathUtils.child(jobFolder, "job.json")
        local saved, saveError = JobState.writeJson(manifestPath, manifest)
        if not saved then
            progress:done()
            LrDialogs.message("Stock Review AI job error", saveError, "critical")
            return
        end
        local activeState = {
            schema_version = 1,
            job_id = clientJobId,
            client_job_id = clientJobId,
            job_folder = jobFolder,
            catalog_path = manifest.catalog_path,
            endpoint_url = settings.endpointUrl,
            status = "submitting",
            applied = false,
            closed = false,
            submitted_at = os.time(),
        }
        local activeSaved, activeSaveError = JobState.saveActive(settings, activeState)
        if not activeSaved then
            progress:done()
            LrDialogs.message("Stock Review AI job error", activeSaveError, "critical")
            return
        end

        progress:setCaption("Creating desktop review job...")
        progress:setPortionComplete(48, 100)
        local response, submitError = JobClient.submit(settings, manifest, progress)
        if not response then
            progress:done()
            LrDialogs.message(
                "Stock Review AI submission interrupted",
                submitError .. "\n\nThe local job is saved. Use 'Check Job Status / Apply Results' to resume submission.",
                "warning"
            )
            return
        end

        activeState.job_id = response.job_id or clientJobId
        activeState.status = response.status or "running"
        JobState.saveActive(settings, activeState)
        progress:setPortionComplete(100, 100)
        progress:done()
        LrDialogs.message(
            "Stock Review AI job submitted",
            "Job " .. activeState.job_id .. " is stored and processing on the desktop.\n\n"
                .. "It is now safe to disconnect the laptop. Reconnect later and use "
                .. "'Check Job Status / Apply Results'.",
            "info"
        )
    end)
end

return Plugin
