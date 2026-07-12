// 1. 获取微信主模块的基地址
var moduleName = "wechat.dylib";
var baseAddr = Process.findModuleByName(moduleName).base;
if (!baseAddr) {
    console.error("[!] 找不到 WeChat 模块基址，请检查进程名。");
}
console.log("[+] WeChat base address: " + baseAddr);
