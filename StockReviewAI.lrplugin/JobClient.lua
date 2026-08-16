local LrHttp = import "LrHttp"

local AiClient = require "AiClient"
local Json = require "Json"
local Utils = require "Utils"

local JobClient = {}

local function responseStatus(headers)
    if type(headers) ~= "table" then
        return nil
    end
    local value = headers.status or headers[":status"] or headers[1]
    local code = value and tostring(value):match("%d%d%d")
    return code and tonumber(code) or nil
end

local function decodeResponse(body, headers)
    local status = responseStatus(headers)
    if not body then
        return nil, "StockReviewAI server did not return a response"
    end
    local ok, result = pcall(function()
        return Json.decode(body)
    end)
    if not ok or type(result) ~= "table" then
        return nil, "StockReviewAI server returned invalid JSON"
    end
    if status and status >= 400 then
        local detail = result.detail or result.error or body
        return nil, "StockReviewAI server HTTP error " .. tostring(status)
            .. ": " .. Utils.safeString(detail)
    end
    return result
end

local function jobBaseUrl(settings)
    local url = tostring(settings.endpointUrl or ""):gsub("/+$", "")
    return url:gsub("/review$", "")
end

local function post(settings, path, payload)
    local body, headers = LrHttp.post(
        jobBaseUrl(settings) .. path,
        Json.encode(payload or {}),
        { { field = "Content-Type", value = "application/json" } },
        "POST",
        settings.timeoutSeconds
    )
    return decodeResponse(body, headers)
end

local function get(settings, path)
    local body, headers = LrHttp.get(
        jobBaseUrl(settings) .. path,
        {},
        settings.timeoutSeconds
    )
    return decodeResponse(body, headers)
end

function JobClient.create(settings, manifest)
    return post(settings, "/jobs", {
        client_job_id = manifest.client_job_id,
        client = "lightroom",
        plugin_version = manifest.plugin_version,
        photos = manifest.server_photos,
    })
end

function JobClient.upload(settings, jobId, photoId, previewPath)
    local jpeg, readError = Utils.fileToString(previewPath)
    if not jpeg then
        return nil, readError
    end
    return post(
        settings,
        "/jobs/" .. jobId .. "/photos/" .. photoId .. "/preview",
        { image_base64 = AiClient.base64Encode(jpeg) }
    )
end

function JobClient.uploaded(settings, jobId)
    return get(settings, "/jobs/" .. jobId .. "/uploads")
end

function JobClient.run(settings, jobId)
    return post(settings, "/jobs/" .. jobId .. "/run", {})
end

function JobClient.status(settings, jobId)
    return get(settings, "/jobs/" .. jobId .. "/status")
end

function JobClient.result(settings, jobId)
    return get(settings, "/jobs/" .. jobId .. "/result")
end

function JobClient.submit(settings, manifest, progress)
    local created, createError = JobClient.create(settings, manifest)
    if not created then
        return nil, createError
    end
    local jobId = created.job_id
    local uploadState, uploadError = JobClient.uploaded(settings, jobId)
    if not uploadState then
        return nil, uploadError
    end
    local uploaded = {}
    for _, photoId in ipairs(uploadState.uploaded_photo_ids or {}) do
        uploaded[photoId] = true
    end

    for index, photo in ipairs(manifest.photos) do
        if progress and progress:isCanceled() then
            return nil, "Upload canceled. The saved job can be resumed later."
        end
        if not uploaded[photo.photo_id] then
            if progress then
                progress:setCaption(
                    "Uploading preview " .. tostring(index) .. " of "
                        .. tostring(#manifest.photos) .. ": " .. photo.filename
                )
                progress:setPortionComplete(50 + (index - 1) * 40 / #manifest.photos, 100)
            end
            local response, errorMessage = JobClient.upload(
                settings,
                jobId,
                photo.photo_id,
                photo.preview_path
            )
            if not response then
                return nil, errorMessage
            end
        end
    end
    return JobClient.run(settings, jobId)
end

return JobClient
