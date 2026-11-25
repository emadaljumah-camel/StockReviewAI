local LrDialogs = import "LrDialogs"
local LrTasks = import "LrTasks"

local JobClient = require "JobClient"
local JobState = require "JobState"
local Settings = require "Settings"
local Utils = require "Utils"

LrTasks.startAsyncTask(function()
    local settings = Settings.get()
    local active, activeError = JobState.loadActive(settings)
    if not active then
        LrDialogs.message(
            "Stock Review AI",
            activeError or "There is no active StockReviewAI job to cancel.",
            activeError and "critical" or "info"
        )
        return
    end
    if active.applied or active.closed then
        LrDialogs.message(
            "Stock Review AI",
            "Job " .. Utils.safeString(active.job_id) .. " is already closed.",
            "info"
        )
        return
    end

    local choice = LrDialogs.confirm(
        "Cancel Stock Review AI Job",
        "Cancel job " .. Utils.safeString(active.job_id)
            .. "? Any unreviewed photos will not be processed. "
            .. "Saved previews and completed results will be retained.",
        "Cancel Job",
        "Keep Job"
    )
    if choice ~= "ok" then
        return
    end

    settings.endpointUrl = active.endpoint_url or settings.endpointUrl
    local response, cancelError = JobClient.cancel(settings, active.job_id)
    if not response then
        LrDialogs.message(
            "Stock Review AI cancel error",
            cancelError .. "\n\nThe active job remains available for retry.",
            "critical"
        )
        return
    end

    active.status = "cancelled"
    active.closed = true
    active.cancelled_at = os.time()
    local saved, saveError = JobState.saveActive(settings, active)
    if not saved then
        LrDialogs.message("Stock Review AI cancel error", saveError, "critical")
        return
    end
    LrDialogs.message(
        "Stock Review AI job closed",
        "Job " .. Utils.safeString(active.job_id)
            .. (response.already_completed
                    and " was closed without applying its completed results."
                or " has been cancelled.")
            .. " You may start a new review.",
        "info"
    )
end)
