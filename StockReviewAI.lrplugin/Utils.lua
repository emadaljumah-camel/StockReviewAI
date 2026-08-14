local LrDialogs = import "LrDialogs"
local LrFileUtils = import "LrFileUtils"
local LrLogger = import "LrLogger"
local LrPathUtils = import "LrPathUtils"

local Utils = {}

Utils.logger = LrLogger("StockReviewAI")
Utils.logger:enable("logfile")

function Utils.safeString(value)
    if value == nil then
        return ""
    end
    return tostring(value)
end

function Utils.join(list, separator)
    if type(list) ~= "table" then
        return Utils.safeString(list)
    end

    local values = {}
    for i = 1, #list do
        values[#values + 1] = Utils.safeString(list[i])
    end
    return table.concat(values, separator or ", ")
end

function Utils.logPath()
    return LrPathUtils.child(_PLUGIN.path, "StockReviewAI.log")
end

function Utils.appendLog(line)
    local path = Utils.logPath()
    local file = io.open(path, "a")
    if file then
        file:write(line)
        file:write("\n")
        file:close()
    end
end

function Utils.formatResultLine(photoPath, result, status, errorMessage)
    local stamp = os.date("%Y-%m-%d %H:%M:%S")
    if result then
        local score = result.score or result.acceptance_likelihood
        local issues = result.issues or result.main_rejection_risks
        local reason = result.reason or result.commercial_value

        return table.concat({
            stamp,
            status or "reviewed",
            photoPath or "",
            "verdict=" .. Utils.safeString(result.verdict),
            "score=" .. Utils.safeString(score),
            "reason=" .. Utils.safeString(reason),
            "issues=" .. Utils.join(issues, ", "),
        }, "\t")
    end

    return table.concat({
        stamp,
        status or "error",
        photoPath or "",
        "error=" .. Utils.safeString(errorMessage),
    }, "\t")
end

function Utils.clearLog()
    local path = Utils.logPath()
    if LrFileUtils.exists(path) then
        LrFileUtils.delete(path)
    end
    LrDialogs.message("Stock Review AI", "AI review log cleared:\n" .. path, "info")
end

function Utils.fileToString(path)
    local file = io.open(path, "rb")
    if not file then
        return nil, "Could not open file: " .. Utils.safeString(path)
    end
    local data = file:read("*all")
    file:close()
    return data
end

return Utils
