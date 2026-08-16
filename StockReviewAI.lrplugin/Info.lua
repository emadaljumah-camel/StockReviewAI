return {
    LrSdkVersion = 15.0,
    LrSdkMinimumVersion = 6.0,

    LrToolkitIdentifier = "com.stockreviewai.lightroom",
    LrPluginName = "02 Stock Review AI",

    LrPluginInfoUrl = "https://localhost/stockreviewai",
    LrPluginInfoProvider = "PluginInfoProvider.lua",

    LrLibraryMenuItems = {
        {
            title = "Review Selected Photos for Stock AI",
            file = "ReviewSelected.lua",
        },
        {
            title = "Check Job Status / Apply Results",
            file = "CheckJobStatus.lua",
        },
    },

    VERSION = { major = 1, minor = 1, revision = 0, build = 2 },
}
