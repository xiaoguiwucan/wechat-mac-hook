var targetPath = "/Applications/WeChat.app/Contents/Resources/wechat.dylib";
var module = Process.enumerateModules().find(function(m) {
	return m.path === targetPath;
});
const baseAddr = module.base
console.log("[+] WeChat base address: " + baseAddr);

function scanPatterns() {
    myPatterns.forEach((item, index) => {
        const { name, pattern } = item;

        Memory.scan(baseAddr, module.size, pattern, {
            onMatch: function(address, size) {
                const offset = address.sub(baseAddr);
                switch (name) {
                    case "cndOnCompleteAddr":
                        console.log(`"${name}": "${offset.sub(0xCC)}", 绝对地址: ${address.sub(0xCC)}`);
                        break;
                    case "uploadGetCallbackWrapperAddr":
                        console.log(`"${name}": "${offset}", 绝对地址: ${address}`);
                        break;
                    case "req2bufEnterAddr":
                        console.log(`"${name}": "${offset}", 绝对地址: ${address}`);
                        break;
                    case "downloadFileAddr":
                        console.log(`"${name}": "${offset.sub(0x08)}", 绝对地址: ${address.sub(0x08)}`);
                        break;
                    case "uploadImageAddr":
                        console.log(`"${name}": "${offset.sub(0x30)}", 绝对地址: ${address.sub(0x30)}`);
                        break;
                    case "sendFuncAddr":
                        console.log(`"${name}": "${offset.sub(0x44)}", 绝对地址: ${address.sub(0x44)}`);
                        break;
                    case "buf2RespAddr":
                        console.log(`"${name}": "${offset.sub(0x14)}", 绝对地址: ${address.sub(0x14)}`);
                        break;
                    case "startDownloadMedia":
                        console.log(`"${name}": "${offset.sub(0x28)}", 绝对地址: ${address.sub(0x28)}`);
                        break;
                    case "downloadVideoAddr":
                        console.log(`"${name}": "${offset.sub(0x2c)}", 绝对地址: ${address.sub(0x2c)}`);
                        break;
                    case "downloadImageAddr":
                        console.log(`"${name}": "${offset.sub(0x18)}", 绝对地址: ${address.sub(0x18)}`);
                        break;
                }
            },
            onError: function(reason) {
                console.error(`[-] 扫描 [${name}] 时出错: ${reason}`);
            },
        });
    });
}


// 特征码数组：? 代表通配符，空格可选
const myPatterns = [
    {
        name: "cndOnCompleteAddr",
        pattern: "68 42 00 91 29 00 80 52 08 01 29 F8 88 12 40 B9"
    },
    {
        name: "uploadGetCallbackWrapperAddr",
        pattern: "08 09 40 F9 E1 03 15 AA E2 03 14 AA E3 03 13 AA 00 01 3F D6 F3 07 40 F9 B3 00 00 B4"
    },
    {
        name: "req2bufEnterAddr",
        pattern: "09 0F 46 F8 C9 01 00 B4 E8 03 18 AA 2A 21 40 B9"
    },
	{
		name: "downloadImageAddr",
		pattern: "68 52 41 F9 19 75 41 F9 1A 00 14 8B 5F 03 19 EB"
	},
    {
        name: "downloadFileAddr",
        pattern: "FF 07 00 B9 68 52 41 F9 15 71 41 F9 E0 03 14 AA"
    },
    {
        name: "uploadImageAddr",
        pattern: "08 01 40 F9 A8 83 1A F8  28 9C 40 B9 1F 0D 00 71",
    },
    {
        name: "sendFuncAddr",
        pattern: "E0 03 00 91 21 00 80 52 E5 03 03 AA 46 8A 80 52"
    },
    {
        name: "buf2RespAddr",
        pattern: "E8 DF C1 39 28 E6 FF 36 E0 33 40 F9 E8 3B 40 F9"
    },
    {
        name: "startDownloadMedia",
        pattern: "08 01 40 F9 A8 83 1C F8 28 7C 42 39 09 1D 00 13 2A 48 40 F9"
    },
    {
        name: "downloadVideoAddr",
        pattern: "E0 03 01 91 01 00 80 52 A5 5B 80 52 06 00 80 52"
    }
];

// 执行扫描
scanPatterns();
