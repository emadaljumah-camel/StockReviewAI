return {
    LrSdkVersion = 15.0,
    LrSdkMinimumVersion = 6.0,

    LrToolkitIdentifier = "com.stockreviewai.lightroom",
    LrPluginName = "02 Stock Review AI",

    LrPluginInfoUrl = "https://localhost/stockreviewai",

    LrLibraryMenuItems = {
        {
            title = "Review Selected Photos for Stock AI",
            file = "ReviewSelected.lua",
        },
        {
            title = "Check Job Status / Apply Results",
            file = "CheckJobStatus.lua",
        },
        {
            title = "Open Stock Review AI Settings",
            file = "OpenSettings.lua",
        },
        {
            title = "Clear AI Review Log",
            file = "ClearLog.lua",
        },
    },

    VERSION = { major = 1, minor = 1, revision = 0, build = 1 },
}
