// 1. 获取微信主模块的基地址
var baseAddr = Process.getModuleByName("WeChat").base;
if (!baseAddr) {
    console.error("[!] 找不到 WeChat 模块基址，请检查进程名。");
}
console.log("[+] WeChat base address: " + baseAddr);

var CndOnCompleteAddr = baseAddr.add(0x36BAFC0);
var videoCallbackFuncAddr = baseAddr.add(0x24C76C8);
var videoProtobufAddr = videoCallbackFuncAddr.add(0x54);

function videoInit() {
    Interceptor.attach(videoProtobufAddr, {
        onEnter: function (args) {

            console.log("[+] 视频寄存器修改完成: X1=" + this.context.x1 + ", X2=" + this.context.x2, hexdump(this.context.x1, {
                offset: 0,
                length: 1028,
                header: true,
                ansi: true
            }));
        }
    })

    Interceptor.attach(CndOnCompleteAddr, {
        onEnter: function (args) {

            try {
                const x2 = this.context.x2;
                const currentFileId = x2.add(0x20).readPointer().readUtf8String();
                const targetId = x2.add(0x40).readUtf8String();

                const globalImageCdnKey = x2.add(0x60).readPointer().readUtf8String();
                const globalAesKey1 = x2.add(0x78).readPointer().readUtf8String();
                const globalMd5Key = x2.add(0x90).readPointer().readUtf8String();

                const mp4Identity = x2.add(0xf0).readPointer().readUtf8String();

                console.log("X2" + x2 + "[+] globalImageCdnKey: " + globalImageCdnKey + " globalAesKey1: " + globalAesKey1 +
                    " globalMd5Key: " + globalMd5Key + " currentFileId: " + currentFileId, " targetId:" + targetId + " mp4Identity:" + mp4Identity );
            } catch (e) {
                console.log("[-] Memory access error at onEnter: " + e);
            }
        }
    })
}

setImmediate(videoInit);
