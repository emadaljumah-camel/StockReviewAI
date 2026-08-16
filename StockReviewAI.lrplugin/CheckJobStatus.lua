local LrApplication = import "LrApplication"
local LrDialogs = import "LrDialogs"
local LrFileUtils = import "LrFileUtils"
local LrPathUtils = import "LrPathUtils"
local LrProgressScope = import "LrProgressScope"
local LrTasks = import "LrTasks"

local AiClient = require "AiClient"
local JobClient = require "JobClient"
local JobState = require "JobState"
local Settings = require "Settings"
local Utils = require "Utils"

local function resolvePhotos(catalog, manifest)
    local byId = {}
    local wanted = {}
    for _, entry in ipairs(manifest.photos or {}) do
        wanted[entry.photo_id] = entry
        if entry.catalog_uuid and entry.catalog_uuid ~= "" then
            local ok, photo = pcall(function()
                return catalog:findPhotoByUuid(entry.catalog_uuid)
            end)
            if ok and photo then
                byId[entry.photo_id] = photo
            end
        end
    end

    local wantedByLocalId = {}
    for photoId, entry in pairs(wanted) do
        if not byId[photoId]
            and entry.local_identifier
            and entry.local_identifier ~= ""
        then
            wantedByLocalId[entry.local_identifier] = photoId
        end
    end
    for _, photo in ipairs(catalog:getAllPhotos()) do
        local localId = tostring(photo.localIdentifier or "")
        local photoId = wantedByLocalId[localId]
        if photoId and not byId[photoId] then
            byId[photoId] = photo
        end
    end

    local missing = {}
    for photoId, _ in pairs(wanted) do
        if not byId[photoId] then
            missing[#missing + 1] = photoId
        end
    end
    table.sort(missing)
    return byId, missing
end

local function validateCompletedResult(result, manifest, photoById)
    if type(result) ~= "table"
        or result.status ~= "completed"
        or type(result.results) ~= "table"
    then
        return nil, "The server result is not a completed StockReviewAI job."
    end

    local expected = {}
    for _, entry in ipairs(manifest.photos or {}) do
        expected[entry.photo_id] = true
    end
    local byId = {}
    for _, item in ipairs(result.results) do
        if not expected[item.photo_id] then
            return nil, "The server returned an unknown photo ID: "
                .. Utils.safeString(item.photo_id)
        end
        if byId[item.photo_id] then
            return nil, "The server returned a duplicate photo ID: "
                .. Utils.safeString(item.photo_id)
        end
        local valid, validationError = AiClient.validateResult(item)
        if not valid then
            return nil, validationError
        end
        if not photoById[item.photo_id] then
            return nil, "The original Lightroom photo is unavailable: "
                .. Utils.safeString(item.photo_id)
        end
        byId[item.photo_id] = item
    end
    for photoId, _ in pairs(expected) do
        if not byId[photoId] then
            return nil, "The server result is missing photo ID: " .. photoId
        end
    end
    return byId
end

local function shouldApplyFlag(photo, manifest)
    if manifest.overwrite_flags then
        return true
    end
    local current = photo:getRawMetadata("pickStatus")
    return current == nil or current == 0
end

local function applyResults(catalog, manifest, resultById, photoById, progress)
    local summary = {
        reviewed = 0,
        picked = 0,
        rejected = 0,
        skipped = 0,
        dryRun = 0,
    }

    local function handle(entry)
        local result = resultById[entry.photo_id]
        local photo = photoById[entry.photo_id]
        local status
        summary.reviewed = summary.reviewed + 1
        if manifest.dry_run then
            status = "dry-run"
            summary.dryRun = summary.dryRun + 1
        elseif not shouldApplyFlag(photo, manifest) then
            status = "skipped-existing-flag"
            summary.skipped = summary.skipped + 1
        elseif result.verdict == "YES" then
            photo:setRawMetadata("pickStatus", 1)
            status = "flagged-pick"
            summary.picked = summary.picked + 1
        else
            photo:setRawMetadata("pickStatus", -1)
            status = "flagged-reject"
            summary.rejected = summary.rejected + 1
        end
        if manifest.write_log then
            Utils.appendLog(
                Utils.formatResultLine(entry.original_path, result, status)
            )
        end
    end

    if manifest.dry_run then
        for index, entry in ipairs(manifest.photos) do
            progress:setPortionComplete(index - 1, #manifest.photos)
            handle(entry)
        end
        return summary
    end

    local writeStatus = catalog:withWriteAccessDo(
        "Stock Review AI Apply Job",
        function()
            for index, entry in ipairs(manifest.photos) do
                progress:setCaption(
                    "Applying result " .. tostring(index) .. " of "
                        .. tostring(#manifest.photos) .. ": " .. entry.filename
                )
                progress:setPortionComplete(index - 1, #manifest.photos)
                handle(entry)
            end
        end,
        { timeout = 30 }
    )
    if writeStatus ~= nil and writeStatus ~= "executed" then
        return nil, "Lightroom catalog write access timed out."
    end
    return summary
end

local function summaryText(summary, jobId)
    return table.concat({
        "Job: " .. jobId,
        "Reviewed: " .. tostring(summary.reviewed),
        "Picked: " .. tostring(summary.picked),
        "Rejected: " .. tostring(summary.rejected),
        "Skipped existing flags: " .. tostring(summary.skipped),
        "Dry-run reviewed: " .. tostring(summary.dryRun),
        "Log: " .. Utils.logPath(),
    }, "\n")
end

LrTasks.startAsyncTask(function()
    local settings = Settings.get()
    local active, activeError = JobState.loadActive(settings)
    if not active then
        LrDialogs.message(
            "Stock Review AI",
            activeError or "There is no active StockReviewAI job.",
            activeError and "critical" or "info"
        )
        return
    end
    if active.applied then
        LrDialogs.message(
            "Stock Review AI",
            "Job " .. Utils.safeString(active.job_id) .. " has already been applied.",
            "info"
        )
        return
    end

    local manifestPath = LrPathUtils.child(active.job_folder, "job.json")
    local manifest, manifestError = JobState.readJson(manifestPath)
    if not manifest then
        LrDialogs.message("Stock Review AI job error", manifestError, "critical")
        return
    end
    settings.endpointUrl = active.endpoint_url or manifest.endpoint_url or settings.endpointUrl

    local progress = LrProgressScope {
        title = "Stock Review AI: Check Job Status",
        caption = "Contacting the desktop server...",
    }
    local resultPath = LrPathUtils.child(active.job_folder, "completed_result.json")
    local result

    if active.status == "downloaded" and LrFileUtils.exists(resultPath) then
        result = JobState.readJson(resultPath)
    else
        if active.status == "submitting" then
            progress:setCaption("Resuming preview upload...")
            local submitted, submitError = JobClient.submit(settings, manifest, progress)
            if not submitted then
                progress:done()
                LrDialogs.message(
                    "Stock Review AI submission unavailable",
                    submitError .. "\n\nThe job remains saved locally; try again when connected.",
                    "warning"
                )
                return
            end
            active.job_id = submitted.job_id or active.job_id
            active.status = submitted.status or "running"
            JobState.saveActive(settings, active)
        end

        local status, statusError = JobClient.status(settings, active.job_id)
        if not status then
            progress:done()
            LrDialogs.message(
                "Stock Review AI server unavailable",
                statusError .. "\n\nNo Lightroom flags were changed.",
                "warning"
            )
            return
        end
        active.status = status.status
        JobState.saveActive(settings, active)

        if status.status == "created" or status.status == "running" then
            progress:done()
            local jobProgress = status.progress or {}
            LrDialogs.message(
                "Stock Review AI job still running",
                "Stage: " .. Utils.safeString(jobProgress.stage) .. "\n"
                    .. "Progress: " .. tostring(jobProgress.processed_photos or 0)
                    .. "/" .. tostring(jobProgress.total_photos or #manifest.photos)
                    .. "\n\nThe laptop may be disconnected again.",
                "info"
            )
            return
        end

        if status.status == "failed" or status.status == "interrupted" then
            local errors = table.concat(status.errors or {}, "\n")
            local choice = LrDialogs.confirm(
                "Stock Review AI job interrupted",
                errors .. "\n\nResume from the last completed photo?",
                "Resume Job",
                "Not Now"
            )
            if choice == "ok" then
                local resumed, resumeError = JobClient.run(settings, active.job_id)
                if not resumed then
                    progress:done()
                    LrDialogs.message("Stock Review AI resume error", resumeError, "critical")
                    return
                end
                active.status = resumed.status or "running"
                JobState.saveActive(settings, active)
                progress:done()
                LrDialogs.message(
                    "Stock Review AI job resumed",
                    "The desktop resumed job " .. active.job_id
                        .. ". The laptop may be disconnected again.",
                    "info"
                )
                return
            end
            progress:done()
            return
        end

        if status.status ~= "completed" then
            progress:done()
            LrDialogs.message(
                "Stock Review AI",
                "Unknown server job status: " .. Utils.safeString(status.status),
                "warning"
            )
            return
        end

        progress:setCaption("Downloading completed results...")
        local downloadError
        result, downloadError = JobClient.result(settings, active.job_id)
        if not result then
            progress:done()
            LrDialogs.message("Stock Review AI download error", downloadError, "critical")
            return
        end
        local resultSaved, resultSaveError = JobState.writeJson(resultPath, result)
        if not resultSaved then
            progress:done()
            LrDialogs.message("Stock Review AI local result error", resultSaveError, "critical")
            return
        end
        active.status = "downloaded"
        JobState.saveActive(settings, active)
    end

    local catalog = LrApplication.activeCatalog()
    if active.catalog_path ~= ""
        and Utils.safeString(catalog:getPath()) ~= active.catalog_path
    then
        progress:done()
        LrDialogs.message(
            "Stock Review AI catalog mismatch",
            "This job belongs to a different Lightroom catalog. Open the original catalog before applying it.",
            "critical"
        )
        return
    end

    local photoById, missing = resolvePhotos(catalog, manifest)
    if #missing > 0 then
        progress:done()
        LrDialogs.message(
            "Stock Review AI missing photos",
            "Could not find " .. tostring(#missing)
                .. " original photo(s). No flags were changed.\n\n"
                .. table.concat(missing, "\n"),
            "critical"
        )
        return
    end
    local resultById, validationError = validateCompletedResult(
        result,
        manifest,
        photoById
    )
    if not resultById then
        progress:done()
        LrDialogs.message(
            "Stock Review AI invalid result",
            validationError .. "\nNo flags were changed.",
            "critical"
        )
        return
    end

    progress:setCaption("Applying completed results...")
    local ok, summaryOrError, applyError = LrTasks.pcall(function()
        return applyResults(catalog, manifest, resultById, photoById, progress)
    end)
    progress:done()
    if not ok or not summaryOrError then
        LrDialogs.message(
            "Stock Review AI apply error",
            Utils.safeString(applyError or summaryOrError)
                .. "\nThe downloaded result remains available; try again later.",
            "critical"
        )
        return
    end

    active.applied = true
    active.status = "applied"
    active.applied_at = os.time()
    JobState.saveActive(settings, active)
    LrDialogs.message(
        "Stock Review AI job applied",
        summaryText(summaryOrError, active.job_id),
        "info"
    )
end)
