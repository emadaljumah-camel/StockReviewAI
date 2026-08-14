local Json = {}

local ESCAPE_MAP = {
    ['"'] = '\\"',
    ["\\"] = "\\\\",
    ["\b"] = "\\b",
    ["\f"] = "\\f",
    ["\n"] = "\\n",
    ["\r"] = "\\r",
    ["\t"] = "\\t",
}

local UNESCAPE_MAP = {
    ['"'] = '"',
    ["\\"] = "\\",
    ["/"] = "/",
    b = "\b",
    f = "\f",
    n = "\n",
    r = "\r",
    t = "\t",
}

local function isArray(value)
    local max = 0
    local count = 0

    for key, _ in pairs(value) do
        if type(key) ~= "number" or key < 1 or key ~= math.floor(key) then
            return false
        end
        if key > max then
            max = key
        end
        count = count + 1
    end

    return max == count
end

local function encodeString(value)
    return '"' .. value:gsub('[%z\1-\31\\"]', function(char)
        return ESCAPE_MAP[char] or string.format("\\u%04x", char:byte())
    end) .. '"'
end

function Json.encode(value)
    local valueType = type(value)

    if valueType == "nil" then
        return "null"
    elseif valueType == "boolean" then
        return value and "true" or "false"
    elseif valueType == "number" then
        return tostring(value)
    elseif valueType == "string" then
        return encodeString(value)
    elseif valueType == "table" then
        local pieces = {}

        if isArray(value) then
            for i = 1, #value do
                pieces[#pieces + 1] = Json.encode(value[i])
            end
            return "[" .. table.concat(pieces, ",") .. "]"
        end

        for key, item in pairs(value) do
            pieces[#pieces + 1] = encodeString(tostring(key)) .. ":" .. Json.encode(item)
        end
        return "{" .. table.concat(pieces, ",") .. "}"
    end

    error("Cannot JSON encode value of type " .. valueType)
end

local Parser = {}
Parser.__index = Parser

function Parser:new(text)
    return setmetatable({ text = text, index = 1, length = #text }, self)
end

function Parser:peek()
    return self.text:sub(self.index, self.index)
end

function Parser:next()
    local char = self:peek()
    self.index = self.index + 1
    return char
end

function Parser:skipWhitespace()
    while self.index <= self.length do
        local char = self:peek()
        if char ~= " " and char ~= "\t" and char ~= "\r" and char ~= "\n" then
            break
        end
        self.index = self.index + 1
    end
end

function Parser:error(message)
    error(message .. " at JSON position " .. tostring(self.index), 0)
end

function Parser:parseString()
    if self:next() ~= '"' then
        self:error("Expected string")
    end

    local pieces = {}
    while self.index <= self.length do
        local char = self:next()
        if char == '"' then
            return table.concat(pieces)
        elseif char == "\\" then
            local escaped = self:next()
            if escaped == "u" then
                local hex = self.text:sub(self.index, self.index + 3)
                if not hex:match("^%x%x%x%x$") then
                    self:error("Invalid unicode escape")
                end
                local code = tonumber(hex, 16)
                self.index = self.index + 4
                if code < 128 then
                    pieces[#pieces + 1] = string.char(code)
                else
                    pieces[#pieces + 1] = "?"
                end
            elseif UNESCAPE_MAP[escaped] then
                pieces[#pieces + 1] = UNESCAPE_MAP[escaped]
            else
                self:error("Invalid escape")
            end
        else
            pieces[#pieces + 1] = char
        end
    end

    self:error("Unterminated string")
end

function Parser:parseNumber()
    local start = self.index
    local char = self:peek()

    if char == "-" then
        self.index = self.index + 1
    end

    while self:peek():match("%d") do
        self.index = self.index + 1
    end

    if self:peek() == "." then
        self.index = self.index + 1
        while self:peek():match("%d") do
            self.index = self.index + 1
        end
    end

    char = self:peek()
    if char == "e" or char == "E" then
        self.index = self.index + 1
        char = self:peek()
        if char == "+" or char == "-" then
            self.index = self.index + 1
        end
        while self:peek():match("%d") do
            self.index = self.index + 1
        end
    end

    local number = tonumber(self.text:sub(start, self.index - 1))
    if number == nil then
        self:error("Invalid number")
    end
    return number
end

function Parser:parseLiteral(literal, value)
    if self.text:sub(self.index, self.index + #literal - 1) ~= literal then
        self:error("Expected " .. literal)
    end
    self.index = self.index + #literal
    return value
end

function Parser:parseArray()
    self:next()
    local result = {}
    self:skipWhitespace()

    if self:peek() == "]" then
        self:next()
        return result
    end

    while true do
        result[#result + 1] = self:parseValue()
        self:skipWhitespace()
        local char = self:next()
        if char == "]" then
            return result
        elseif char ~= "," then
            self:error("Expected comma or closing bracket")
        end
    end
end

function Parser:parseObject()
    self:next()
    local result = {}
    self:skipWhitespace()

    if self:peek() == "}" then
        self:next()
        return result
    end

    while true do
        self:skipWhitespace()
        if self:peek() ~= '"' then
            self:error("Expected object key")
        end
        local key = self:parseString()
        self:skipWhitespace()
        if self:next() ~= ":" then
            self:error("Expected colon")
        end
        result[key] = self:parseValue()
        self:skipWhitespace()
        local char = self:next()
        if char == "}" then
            return result
        elseif char ~= "," then
            self:error("Expected comma or closing brace")
        end
    end
end

function Parser:parseValue()
    self:skipWhitespace()
    local char = self:peek()

    if char == '"' then
        return self:parseString()
    elseif char == "{" then
        return self:parseObject()
    elseif char == "[" then
        return self:parseArray()
    elseif char == "t" then
        return self:parseLiteral("true", true)
    elseif char == "f" then
        return self:parseLiteral("false", false)
    elseif char == "n" then
        return self:parseLiteral("null", nil)
    elseif char == "-" or char:match("%d") then
        return self:parseNumber()
    end

    self:error("Unexpected character")
end

function Json.decode(text)
    if type(text) ~= "string" then
        error("JSON input must be a string")
    end

    local parser = Parser:new(text)
    local value = parser:parseValue()
    parser:skipWhitespace()
    if parser.index <= parser.length then
        parser:error("Trailing JSON content")
    end
    return value
end

return Json
