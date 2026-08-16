local LrFileUtils = import "LrFileUtils"
local LrPathUtils = import "LrPathUtils"

local Json = require "Json"
local Utils = require "Utils"

local JobState = {}

local function ensureDirectory(path)
    if not LrFileUtils.exists(path) then
        LrFileUtils.createAllDirectories(path)
    end
end

function JobState.activePath(settings)
    ensureDirectory(settings.jobRoot)
    return LrPathUtils.child(settings.jobRoot, "active_job.json")
end

function JobState.createFolder(settings, clientJobId)
    ensureDirectory(settings.jobRoot)
    local folder = LrPathUtils.child(settings.jobRoot, "job_" .. clientJobId)
    ensureDirectory(folder)
    ensureDirectory(LrPathUtils.child(folder, "previews"))
    return folder
end

function JobState.writeJson(path, value)
    local temporary = path .. ".tmp"
    local backup = path .. ".bak"
    local file, openError = io.open(temporary, "w")
    if not file then
        return false, openError
    end
    file:write(Json.encode(value))
    file:write("\n")
    file:close()

    if LrFileUtils.exists(backup) then
        LrFileUtils.delete(backup)
    end
    if LrFileUtils.exists(path) then
        local backedUp = LrFileUtils.move(path, backup)
        if not backedUp then
            return false, "Could not back up local job state: " .. path
        end
    end
    local moved = LrFileUtils.move(temporary, path)
    if not moved then
        if LrFileUtils.exists(backup) then
            LrFileUtils.move(backup, path)
        end
        return false, "Could not replace local job state: " .. path
    end
    if LrFileUtils.exists(backup) then
        LrFileUtils.delete(backup)
    end
    return true
end

function JobState.readJson(path)
    if not LrFileUtils.exists(path) and LrFileUtils.exists(path .. ".bak") then
        LrFileUtils.move(path .. ".bak", path)
    end
    local text, readError = Utils.fileToString(path)
    if not text then
        return nil, readError
    end
    local ok, value = pcall(function()
        return Json.decode(text)
    end)
    if not ok then
        return nil, tostring(value)
    end
    return value
end

function JobState.loadActive(settings)
    local path = JobState.activePath(settings)
    if not LrFileUtils.exists(path) then
        return nil
    end
    return JobState.readJson(path)
end

function JobState.saveActive(settings, state)
    return JobState.writeJson(JobState.activePath(settings), state)
end

return JobState
