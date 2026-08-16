local LrHttp = import "LrHttp"

local Json = require "Json"
local Utils = require "Utils"

local AiClient = {}

local BASE64_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"

local function base64Encode(data)
    return ((data:gsub(".", function(char)
        local byte = char:byte()
        local bits = ""
        for i = 8, 1, -1 do
            bits = bits .. (byte % 2 ^ i - byte % 2 ^ (i - 1) > 0 and "1" or "0")
        end
        return bits
    end) .. "0000"):gsub("%d%d%d?%d?%d?%d?", function(bits)
        if #bits < 6 then
            return ""
        end
        local value = 0
        for i = 1, 6 do
            value = value + (bits:sub(i, i) == "1" and 2 ^ (6 - i) or 0)
        end
        return BASE64_CHARS:sub(value + 1, value + 1)
    end) .. ({ "", "==", "=" })[#data % 3 + 1])
end

local function decodeJson(text)
    local ok, value = pcall(function()
        return Json.decode(text)
    end)
    if ok then
        return value
    end
    return nil
end

local function responsePreview(text)
    local value = tostring(text or "")
    value = value:gsub("%s+", " ")
    if #value > 500 then
        return value:sub(1, 500) .. "..."
    end
    return value
end

local function tablePreview(value)
    if type(value) ~= "table" then
        return tostring(value)
    end

    local parts = {}
    local count = 0
    for key, item in pairs(value) do
        count = count + 1
        if count > 12 then
            parts[#parts + 1] = "..."
            break
        end
        parts[#parts + 1] = tostring(key) .. "=" .. tostring(item)
    end

    return table.concat(parts, "; ")
end

local function responseStatus(responseHeaders)
    if type(responseHeaders) ~= "table" then
        return nil
    end

    local status = responseHeaders.status or responseHeaders[":status"]
    if not status and type(responseHeaders[1]) == "string" then
        status = responseHeaders[1]
    end

    if status then
        local statusCode = tostring(status):match("%d%d%d")
        if statusCode then
            return tonumber(statusCode)
        end
    end

    return nil
end

local function jpegSignature(data)
    if type(data) ~= "string" or #data < 3 then
        return false
    end
    return data:byte(1) == 0xff and data:byte(2) == 0xd8 and data:byte(3) == 0xff
end

local function validateStringField(result, key, errors, required)
    if result[key] == nil and not required then
        return
    end

    if type(result[key]) ~= "string" or result[key] == "" then
        errors[#errors + 1] = key
    end
end

function AiClient.validateResult(result)
    if type(result) ~= "table" then
        return false, "AI result was not a JSON object"
    end

    local errors = {}
    validateStringField(result, "verdict", errors, true)
    validateStringField(result, "reason", errors, true)

    if result.verdict ~= "YES" and result.verdict ~= "NO" then
        errors[#errors + 1] = "verdict must be YES or NO"
    end

    local score = tonumber(result.score)
    if not score or score < 0 or score > 100 then
        errors[#errors + 1] = "score"
    else
        result.score = score
    end

    if result.issues == nil then
        result.issues = {}
    elseif type(result.issues) ~= "table" then
        errors[#errors + 1] = "issues"
    end

    if #errors > 0 then
        return false, "Invalid review server JSON schema: " .. table.concat(errors, ", ")
    end

    return true
end

local function buildBody(
    settings,
    filename,
    jpegBase64,
    locationContext
)
    local body = {
        filename = filename,
        image_base64 = jpegBase64,
        threshold = settings.minimumYesThreshold,
    }

    if locationContext then
        body.location_context = locationContext
    end

    return body
end

function AiClient.reviewPreview(
    previewPath,
    settings,
    filename,
    locationContext
)
    local jpegData, readError = Utils.fileToString(previewPath)
    if not jpegData then
        return nil, readError
    end

    local jpegBase64 = base64Encode(jpegData)
    local body = Json.encode(buildBody(
        settings,
        filename or previewPath,
        jpegBase64,
        locationContext
    ))
    local headers = {
        { field = "Content-Type", value = "application/json" },
    }

    local responseBody, responseHeaders = LrHttp.post(settings.endpointUrl, body, headers, settings.timeoutSeconds)
    if not responseBody then
        return nil, "StockReviewAI server did not return a response"
            .. "; endpoint=" .. Utils.safeString(settings.endpointUrl)
            .. "; preview_bytes=" .. tostring(#jpegData)
            .. "; base64_chars=" .. tostring(#jpegBase64)
            .. "; jpeg_signature=" .. tostring(jpegSignature(jpegData))
            .. "; headers=" .. tablePreview(responseHeaders)
    end

    local status = responseStatus(responseHeaders)
    if status and status >= 400 then
        return nil, "StockReviewAI server HTTP error " .. tostring(status) .. ": " .. responsePreview(responseBody)
            .. "; endpoint=" .. Utils.safeString(settings.endpointUrl)
            .. "; preview_bytes=" .. tostring(#jpegData)
            .. "; base64_chars=" .. tostring(#jpegBase64)
            .. "; jpeg_signature=" .. tostring(jpegSignature(jpegData))
            .. "; headers=" .. tablePreview(responseHeaders)
    end

    local result = decodeJson(responseBody)
    if not result then
        return nil, "StockReviewAI server response was not valid JSON: " .. responsePreview(responseBody)
            .. "; response_bytes=" .. tostring(#responseBody)
            .. "; endpoint=" .. Utils.safeString(settings.endpointUrl)
            .. "; preview_bytes=" .. tostring(#jpegData)
            .. "; base64_chars=" .. tostring(#jpegBase64)
            .. "; jpeg_signature=" .. tostring(jpegSignature(jpegData))
            .. "; headers=" .. tablePreview(responseHeaders)
    end

    local ok, validationError = AiClient.validateResult(result)
    if not ok then
        return nil, validationError
    end

    return result
end

return AiClient
