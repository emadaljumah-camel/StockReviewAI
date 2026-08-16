local Settings = require "Settings"
local Utils = require "Utils"

local provider = {}

function provider.sectionsForTopOfDialog(f, propertyTable)
    return {
        {
            title = "Stock Review AI",
            synopsis = "Settings and maintenance",

            f:column {
                spacing = f:control_spacing(),
                fill_horizontal = 1,

                f:static_text {
                    title = "Configure Stock Review AI or clear its local review log.",
                },

                f:row {
                    spacing = f:control_spacing(),

                    f:push_button {
                        title = "Open Settings...",
                        action = function()
                            Settings.showDialog()
                        end,
                    },

                    f:push_button {
                        title = "Clear AI Review Log",
                        action = function()
                            Utils.clearLog()
                        end,
                    },
                },
            },
        },
    }
end

return provider
