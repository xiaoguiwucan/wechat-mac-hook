// =============================================================================
// 微信 macOS Frida 脚本 - 文件上传与发送
// 基于视频上传逻辑，0x98 位置从 0x04 改为 0x05 以支持文件类型
// =============================================================================


// =========================== 模块基址初始化 ===========================

var moduleName = "wechat.dylib";
var baseAddr = Process.findModuleByName(moduleName).base;
if (!baseAddr) {
    console.error("[!] 找不到 WeChat 模块基址，请检查进程名。");
}
console.log("[+] WeChat base address: " + baseAddr);


// =========================== 地址定义 ===========================

// --- 文件上传相关地址 ---
var uploadImageAddr = baseAddr.add(0x4ad95c0);
var uploadGetCallbackWrapperAddr = baseAddr.add(0x4aa7084);
var uploadGetCallbackWrapperFuncAddr = baseAddr.add(0x37C874C);
var uploadOnCompleteAddr = baseAddr.add(0x4AA7680);
var uploadOnCompleteFuncAddr = baseAddr.add(0x37C9930);
var cndOnCompleteAddr = baseAddr.add(0x37c8f00);

// --- 文件回调 & Protobuf 相关地址 ---
var fileCallbackFuncAddr = baseAddr.add(0x2544584);
var fileProtobufAddr = fileCallbackFuncAddr.add(0x50);
var patchFileProtobufFunc1 = fileCallbackFuncAddr.add(0x10);
var patchFileProtobufFunc2 = fileCallbackFuncAddr.add(0x30);
var fileProtobufDeleteAddr = fileCallbackFuncAddr.add(0x6c);
var fileMessageCallbackFunc1 = baseAddr.add(0x892DEF8);

// --- 发送 & Req2Buf 相关地址 ---
var sendFuncAddr = baseAddr.add(0x4992040);
var req2bufEnterAddr = baseAddr.add(0x380b950);
var req2bufExitAddr = baseAddr.add(0x380CA64);


// =========================== 全局状态变量 ===========================

// --- 上传相关指针 ---
var uploadFileX1 = ptr(0);
var fileIdAddr = ptr(0);
var md5Addr = ptr(0);
var uploadAesKeyAddr = ptr(0);
var filePathAddr1 = ptr(0);
var uploadGlobalX0 = ptr(0);
var uploadFunc1Addr = ptr(0);
var uploadFunc2Addr = ptr(0);
var uploadCallback = ptr(0);

// --- 发送消息相关指针 ---
var fileCgiAddr2 = ptr(0);
var sendFileMessageAddr = ptr(0);
var fileMessageAddr = ptr(0);
var fileProtoX1PayloadAddr = ptr(0);
var triggerX1Payload;
var triggerX0;
var insertMsgAddr = ptr(0);

// --- Patch 原始字节备份 ---
var patchFileProtobufFunc1Byte;
var patchFileProtobufFunc2Byte;
var fileProtobufDeleteAddrByte;

// --- 业务状态 ---
var taskIdGlobal = 0x0;
var receiverGlobal = "wxid_";
var contentGlobal = "";
var senderGlobal = "wxid_";
var lastSendTime = 0;
var sendMsgType = "";

// --- 常量 ---
const fileCp = generateBytes(16);


// =========================== 工具函数 ===========================

function stringToHexArray(str) {
    var utf8Str = unescape(encodeURIComponent(str));
    var arr = [];
    for (var i = 0; i < utf8Str.length; i++) {
        arr.push(utf8Str.charCodeAt(i));
    }
    return arr;
}

function toVarint(n) {
    let res = [];
    while (n >= 128) {
        res.push((n & 0x7F) | 0x80);
        n = n >> 7;
    }
    res.push(n);
    return res;
}

function generateRandom5ByteVarint() {
    let res = [];
    // 前 4 个字节：最高位(bit 7)必须是 1，低 7 位随机
    for (let i = 0; i < 4; i++) {
        let random7Bit = Math.floor(Math.random() * 128);
        res.push(random7Bit | 0x80);
    }
    // 第 5 个字节：最高位必须是 0，低 7 位不能全为 0
    let lastByte = Math.floor(Math.random() * 127) + 1;
    res.push(lastByte & 0x7F);
    return res;
}

function getVarintTimestampBytes() {
    let ts = Math.floor(Date.now() / 1000);
    let encodedBytes = [];
    let tempTs = ts >>> 0;
    while (true) {
        let byte = tempTs & 0x7F;
        tempTs >>>= 7;
        if (tempTs !== 0) {
            encodedBytes.push(byte | 0x80);
        } else {
            encodedBytes.push(byte);
            break;
        }
    }
    return encodedBytes;
}

function patchString(addr, plainStr) {
    const bytes = [];
    for (let i = 0; i < plainStr.length; i++) {
        bytes.push(plainStr.charCodeAt(i));
    }
    addr.writeByteArray(bytes);
    addr.add(bytes.length).writeU8(0);
}

function generateAESKey() {
    const chars = 'abcdef0123456789';
    let key = '';
    for (let i = 0; i < 32; i++) {
        key += chars.charAt(Math.floor(Math.random() * chars.length));
    }
    return key;
}

function generateBytes(n) {
    const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
    let result = '';
    for (let i = 0; i < n; i++) {
        result += chars.charAt(Math.floor(Math.random() * chars.length));
    }
    return stringToHexArray(result);
}


// =========================== 文件上传队列 ===========================

var fileUploadQueue = [];

function getFileUploadInfo() {
    if (fileUploadQueue.length > 0) {
        return fileUploadQueue.shift();
    }
    return null;
}

function pushFileUploadInfo(info) {
    fileUploadQueue.push(info);
    console.log("[+] 文件上传信息已入队，当前队列长度:", fileUploadQueue.length);
}


// =========================== 内存初始化 ===========================

function setupUploadFileDynamic() {
    // 上传相关内存分配
    fileIdAddr = Memory.alloc(256);
    filePathAddr1 = Memory.alloc(256);
    uploadFileX1 = Memory.alloc(1024);
    uploadFunc1Addr = Memory.alloc(24);
    uploadFunc2Addr = Memory.alloc(24);
    uploadCallback = Memory.alloc(128);
    md5Addr = Memory.alloc(128);
    uploadAesKeyAddr = Memory.alloc(128);

    // 发送文件消息相关内存分配
    fileCgiAddr2 = Memory.alloc(128);
    sendFileMessageAddr = Memory.alloc(256);
    fileMessageAddr = Memory.alloc(256);
    fileProtoX1PayloadAddr = Memory.alloc(4096);
    triggerX1Payload = Memory.alloc(1024);

    patchString(fileCgiAddr2, "/cgi-bin/micromsg-bin/sendappmsg");

    // 初始化 sendFileMessageAddr 结构体
    sendFileMessageAddr.add(0x00).writeU64(0);
    sendFileMessageAddr.add(0x08).writeU64(0);
    sendFileMessageAddr.add(0x10).writeU64(0);
    sendFileMessageAddr.add(0x18).writeU64(1);
    sendFileMessageAddr.add(0x20).writeU32(taskIdGlobal);
    sendFileMessageAddr.add(0x28).writePointer(fileMessageAddr);

    // 初始化 fileMessageAddr 结构体
    fileMessageAddr.add(0x00).writePointer(fileMessageCallbackFunc1);
    fileMessageAddr.add(0x08).writeU32(taskIdGlobal);
    fileMessageAddr.add(0x0c).writeU32(0x20a);
    fileMessageAddr.add(0x10).writeU64(0x3);
    fileMessageAddr.add(0x18).writePointer(fileCgiAddr2);
    fileMessageAddr.add(0x20).writeU64(uint64("0x20"));

    // 备份 Patch 原始字节
    patchFileProtobufFunc1Byte = patchFileProtobufFunc1.readByteArray(4);
    patchFileProtobufFunc2Byte = patchFileProtobufFunc2.readByteArray(4);
    fileProtobufDeleteAddrByte = fileProtobufDeleteAddr.readByteArray(4);

    console.log("[+] setupUploadFileDynamic Complete.");
}

setImmediate(setupUploadFileDynamic);


// =========================== 文件上传触发 ===========================

function triggerUploadFile(receiver, md5, filePath) {
    const payload = [
        0x20, 0x05, 0x33, 0x8C, 0x0B, 0x00, 0x00, 0x00, // 0x00 函数指针1
        0x00, 0x05, 0x33, 0x8C, 0x0B, 0x00, 0x00, 0x00, // 0x08 函数指针2
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x10
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x18
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x20
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x28
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x30
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x38
        0x01, 0x00, 0x00, 0x00, 0x0B, 0x00, 0x00, 0x00, // 0x40
        0xD0, 0x72, 0x20, 0x89, 0x0B, 0x00, 0x00, 0x00, // 0x48 文件id
        0x26, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x50
        0x28, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x80, // 0x58
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x60
        0x77, 0x78, 0x69, 0x64, 0x5F, 0x37, 0x77, 0x64, // 0x68 发送人
        0x31, 0x65, 0x63, 0x65, 0x39, 0x39, 0x66, 0x37, // 0x70
        0x69, 0x32, 0x31, 0x00, 0x00, 0x00, 0x00, 0x13, // 0x78 发送人id长度
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x80
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x88
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x90
        0x01, 0xAA, 0xAA, 0xAA, 0x05, 0x00, 0x00, 0x00, // 0x98 文件类型标记 (0x05)
        0x00, 0x00, 0x00, 0x00, 0xAA, 0xAA, 0xAA, 0xAA, // 0xa0
        0xA0, 0xBE, 0x2D, 0x8C, 0x0B, 0x00, 0x00, 0x00, // 0xa8
        0x20, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0xb0
        0x28, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x80, // 0xb8
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0xc0
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0xc8
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0xd0
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0xd8
        0x00, 0x55, 0xDB, 0x89, 0x0B, 0x00, 0x00, 0x00, // 0xe0 文件路径1
        0xB2, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0xe8
        0xB8, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x80, // 0xf0
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0xf8
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x100
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x108
        0x40, 0x54, 0xDB, 0x89, 0x0B, 0x00, 0x00, 0x00, // 0x110 文件路径2
        0xB2, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x118
        0xB8, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x80, // 0x120
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x128
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x130
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x138
        0x40, 0x5D, 0xDB, 0x89, 0x0B, 0x00, 0x00, 0x00, // 0x140 文件路径3
        0xB2, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x148
        0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x80, // 0x150
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x158
        0x00, 0x00, 0x00, 0x00, 0x04, 0x00, 0xE0, 0x03, // 0x160
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x168
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x170
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x178
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x180
        0x00, 0xAA, 0xAA, 0xAA, 0x01, 0x00, 0x00, 0x00, // 0x190
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x198
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x1a0
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x1a8
        0x00, 0x00, 0x00, 0x00, 0x0A, 0x0A, 0x0A, 0x0A, // 0x1b0
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x1b8
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x1c0
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x1c8
        0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x1d0
        0x00, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x1d8
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x1e0
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x1e8
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x1f0
        0xD0, 0x78, 0x46, 0x8C, 0x0B, 0x00, 0x00, 0x00, // 0x1f8 AES Key
        0x20, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x200
        0x28, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x80, // 0x208
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x210
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x218
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x220
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x228
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x230
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x238
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x240
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x248
        0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, // 0x250
        0x00, 0x01, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, // 0x258
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x260
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x268
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x270
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x278
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x280
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x288
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x290
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x298
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x2a0
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x2a8
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x2b0
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x2b8
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x2c0
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x2c8
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x2d0
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x2d8
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x2e0
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00  // 0x2e8
    ];

    patchString(fileIdAddr, receiver + "_" + String(Math.floor(Date.now() / 1000)) + "_" + Math.floor(Math.random() * 1001) + "_1");
    patchString(md5Addr, md5);
    patchString(uploadAesKeyAddr, generateAESKey());
    patchString(filePathAddr1, filePath);

    // 写入 payload 并覆盖关键字段指针
    uploadFileX1.writeByteArray(payload);
    uploadFileX1.writePointer(uploadFunc1Addr);
    uploadFileX1.add(0x08).writePointer(uploadFunc2Addr);
    uploadFileX1.add(0x48).writePointer(fileIdAddr);
    uploadFileX1.add(0x68).writeUtf8String(receiver);
    uploadFileX1.add(0xa8).writePointer(md5Addr);
    uploadFileX1.add(0xe0).writePointer(filePathAddr1);
    uploadFileX1.add(0x110).writePointer(filePathAddr1);
    uploadFileX1.add(0x140).writePointer(filePathAddr1);
    uploadFileX1.add(0x1f8).writePointer(uploadAesKeyAddr);

    const startUploadMedia = new NativeFunction(uploadImageAddr, 'int64', ['pointer', 'pointer']);

    console.log("开始手动触发文件上传 X0 " + uploadGlobalX0 + ", X1: " + uploadFileX1 + hexdump(uploadFileX1, {
        offset: 0,
        length: 256,
        header: true,
        ansi: true
    }));
    const result = startUploadMedia(uploadGlobalX0, uploadFileX1);
    console.log("文件上传调用结果: " + result);
}


// =========================== 发送文件消息触发 ===========================

function triggerSendFileMessage(taskId, sender, receiver) {
    console.log("[+] File Manual Trigger Started...");
    if (!taskId || !receiver || !sender) {
        console.error("[!] taskId or receiver or sender is empty!");
        return "fail";
    }

    const timestamp = Math.floor(Date.now() / 1000);
    lastSendTime = timestamp;
    taskIdGlobal = taskId;
    receiverGlobal = receiver;
    senderGlobal = sender;

    fileMessageAddr.add(0x08).writeU32(taskIdGlobal);
    sendFileMessageAddr.add(0x20).writeU32(taskIdGlobal);

    const payloadData = [
        0x0A, 0x02, 0x00, 0x00,                         // 0x00
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x08
        0x03, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, // 0x10
        0x40, 0xec, 0x0e, 0x12, 0x01, 0x00, 0x00, 0x00, // 0x18
        0x20, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x20
        0x30, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x80, // 0x28
        0x00, 0x01, 0x01, 0x01, 0x00, 0xAA, 0xAA, 0xAA, // 0x30
        0x00, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00, // 0x38
        0x01, 0x00, 0x00, 0x00, 0xFF, 0xFF, 0xFF, 0xFF, // 0x40
        0xFF, 0xFF, 0xFF, 0xFF, 0x00, 0xAA, 0xAA, 0xAA, // 0x48
        0xFF, 0xFF, 0xFF, 0xFF, 0xAA, 0xAA, 0xAA, 0xAA, // 0x50
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x58
        0x0A, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x60
        0x64, 0x65, 0x66, 0x61, 0x75, 0x6C, 0x74, 0x2D, // 0x68 "default-"
        0x6C, 0x6F, 0x6E, 0x67, 0x6C, 0x69, 0x6E, 0x6B, // 0x70 "longlink"
        0x00, 0xAA, 0xAA, 0xAA, 0xAA, 0xAA, 0xAA, 0x10, // 0x78
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x80
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x88
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x90
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x98
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0xA0
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0xA8
        0x00, 0x00, 0x00, 0x00, 0xAA, 0xAA, 0xAA, 0xAA, // 0xB0
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0xB8
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0xC0
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0xC8
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0xD0
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0xD8
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0xE0
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0xE8
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0xF0
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0xF8
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x100
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x108
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x110
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x118
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x120
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x128
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x130
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x138
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x140
        0x01, 0x00, 0x00, 0x00, 0xAA, 0xAA, 0xAA, 0xAA, // 0x148
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x150
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x158
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x160
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x168
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x170
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x178
        0x03, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x180
        0x00, 0x00, 0xAA, 0xAA, 0xAA, 0xAA, 0xAA, 0xAA, // 0x188
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x190
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x198
    ];

    triggerX1Payload.writeU32(taskIdGlobal);
    triggerX1Payload.add(0x04).writeByteArray(payloadData);
    triggerX1Payload.add(0x18).writePointer(fileCgiAddr2);
    triggerX1Payload.add(0xb8).writePointer(triggerX1Payload.add(0xc0));
    triggerX1Payload.add(0x190).writePointer(triggerX1Payload.add(0x198));
    sendMsgType = "file";

    console.log("finished init file payload");
    const MMStartTask = new NativeFunction(sendFuncAddr, 'int64', ['pointer', 'pointer']);

    try {
        const result = MMStartTask(triggerX0, triggerX1Payload);
        console.log(`[+] Execution StartTask ${sendFuncAddr} with args: (${triggerX0}) (${triggerX1Payload})  Success. Return value: ` + result);
        return "ok";
    } catch (e) {
        console.error(`[!] Error trigger StartTask ${sendFuncAddr} with args: (${triggerX0}) (${triggerX1Payload}),   during execution: ` + e);
        return "fail";
    }
}


// =========================== Interceptor Hook: 上传媒体 ===========================

function attachUploadFileMedia() {
    Interceptor.attach(uploadImageAddr.add(0x10), {
        onEnter: function (args) {
            uploadGlobalX0 = this.context.x0;
            const selfId = this.context.x1.add(0x68).readUtf8String();
            const filePath = this.context.x1.add(0xe0).readPointer().readUtf8String();
            send({
                type: "upload",
                self_id: selfId,
            });
            console.log("UploadFileMedia x0: " + uploadGlobalX0 + " filePath: " + filePath + " selfId: " + selfId);
        }
    });
}

setImmediate(attachUploadFileMedia);


// =========================== Interceptor Hook: CDN 上传完成 ===========================

function patchFileCdnOnComplete() {
    Interceptor.attach(cndOnCompleteAddr, {
        onEnter: function (args) {
            try {
                const x2 = this.context.x2;
                const currentFileId = x2.add(0x20).readPointer().readUtf8String();
                const fileId = fileIdAddr.readUtf8String();
                if (currentFileId !== fileId) {
                    console.log("[-] FileCdnOnComplete x2: " + x2 + " currentFileId: " + currentFileId + " fileId: " + fileId);
                    return;
                }

                const cdnKey = x2.add(0x60).readPointer().readUtf8String();
                const aesKey = x2.add(0x78).readPointer().readUtf8String();
                const md5Key = x2.add(0x90).readPointer().readUtf8String();
                const videoId = x2.add(0xf0).readPointer().readUtf8String();
                const targetId = x2.add(0x40).readUtf8String();

                console.log("File X2: " + x2 + "[+] cdnKey: " + cdnKey + " aesKey: " + aesKey +
                    " md5Key: " + md5Key + " videoId:" + videoId);

                send({ type: "finish" });

                if (cdnKey !== "" && cdnKey != null && aesKey !== "" && aesKey != null &&
                    md5Key !== "" && md5Key != null) {
                    pushFileUploadInfo({
                        cdnKey: cdnKey,
                        aesKey: aesKey,
                        md5Key: md5Key,
                        targetId: targetId
                    });
                    send({
                        type: "upload_file_finish",
                        target_id: targetId,
                        cdn_key: cdnKey,
                        aes_key: aesKey,
                        md5_key: md5Key
                    });
                } else {
                    console.error("cdnKey or aesKey or md5key 为空");
                }
            } catch (e) {
                console.log("[-] File Memory access error at onEnter: " + e);
            }
        }
    });
}

setImmediate(patchFileCdnOnComplete);


// =========================== Interceptor Hook: 上传回调包装 ===========================

function attachFileGetCallbackFromWrapper() {
    Interceptor.attach(uploadGetCallbackWrapperAddr, {
        onEnter: function (args) {
            const tmpFileId = this.context.x1.readPointer().readUtf8String();
            const fileId = fileIdAddr.readUtf8String();
            if (tmpFileId !== fileId) {
                console.log("[+] File GetCallbackFromWrapper tmpFileId: " + tmpFileId + " fileId: " + fileId);
                return;
            }
            uploadCallback.add(0x10).writePointer(uploadGetCallbackWrapperFuncAddr);
            this.context.x8 = uploadCallback;
            console.log("[+] File GetCallbackFromWrapper x8: " + this.context.x8);
        }
    });

    Interceptor.attach(uploadOnCompleteAddr, {
        onEnter: function (args) {
            const tmpFileId = this.context.x1.readPointer().readUtf8String();
            const fileId = fileIdAddr.readUtf8String();
            if (tmpFileId !== fileId) {
                console.log("[+] File OnComplete tmpFileId: " + tmpFileId + " fileId: " + fileId);
                return;
            }
            uploadCallback.add(0x30).writePointer(uploadOnCompleteFuncAddr);
            this.context.x8 = uploadCallback;
            console.log("[+] File OnComplete x8: " + this.context.x8);
        }
    });
}

setImmediate(attachFileGetCallbackFromWrapper);


// =========================== Interceptor Hook: Req2Buf 拦截 ===========================

function attachReq2buf() {
    console.log("[+] Target Req2buf enter Address: " + req2bufEnterAddr);

    Interceptor.attach(req2bufEnterAddr, {
        onEnter: function (args) {
            if (!this.context.x1.equals(taskIdGlobal)) {
                return;
            }
            console.log("[+] 已命中目标Req2Buf taskId:" + taskIdGlobal + " base:" + baseAddr);

            const x24_base = this.context.x24;
            insertMsgAddr = x24_base.add(0x60);
            console.log("[+] 当前 Req2Buf X24 基址: " + x24_base);

            if (sendMsgType === "file") {
                insertMsgAddr.writePointer(sendFileMessageAddr);
                console.log("[+] 发送文件消息成功! Req2Buf 已将 X24+0x60 指向新地址: " + sendFileMessageAddr +
                    " Req2Buf 写入后内存预览: " + insertMsgAddr);
            }
        }
    });

    console.log("[+] Target Req2buf leave Address: " + req2bufExitAddr);

    Interceptor.attach(req2bufExitAddr, {
        onEnter: function (args) {
            if (!this.context.x25.equals(taskIdGlobal)) {
                return;
            }
            insertMsgAddr.writeU64(0x0);
            console.log("[+] 清空写入后内存预览: " + insertMsgAddr.readPointer());
            taskIdGlobal = 0;
            receiverGlobal = "";
            senderGlobal = "";
            contentGlobal = "";
            send({ type: "finish" });
        }
    });
}

setImmediate(attachReq2buf);


// =========================== Interceptor Hook: 捕获 SendProto ===========================

function AttachSendProto() {
    Interceptor.attach(sendFuncAddr, {
        onEnter: function (args) {
            if (triggerX1Payload) {
                return;
            }
            triggerX0 = this.context.x0;
            triggerX1Payload = this.context.x1;
            console.log(`[+] 捕获到 StartTask 调用，X0地址：${triggerX0}, Payload 地址: ${triggerX1Payload}`);
        }
    });
}

setImmediate(AttachSendProto);


// =========================== Interceptor Hook: Patch File ProtoBuf ===========================

function patchFileProtoBuf() {
    Interceptor.attach(fileCallbackFuncAddr, {
        onEnter: function (args) {
            var firstValue = this.context.sp.add(0x10).readU32();
            console.log("[+] 捕获到 FileCallbackFunc 调用，firstValue：", firstValue, "taskIdGlobal：", taskIdGlobal);

            if (firstValue === taskIdGlobal) {
                // 目标任务：将指令 NOP 掉以跳过原逻辑
                if (patchFileProtobufFunc1.readU32() !== 3573751839) {
                    Memory.patchCode(patchFileProtobufFunc1, 4, code => {
                        const cw = new Arm64Writer(code, { pc: patchFileProtobufFunc1 });
                        cw.putNop();
                        cw.flush();
                    });
                    Memory.patchCode(patchFileProtobufFunc2, 4, code => {
                        const cw = new Arm64Writer(code, { pc: patchFileProtobufFunc2 });
                        cw.putNop();
                        cw.flush();
                    });
                    Memory.patchCode(fileProtobufDeleteAddr, 4, code => {
                        const cw = new Arm64Writer(code, { pc: fileProtobufDeleteAddr });
                        cw.putNop();
                        cw.flush();
                    });
                }
            } else {
                // 非目标任务：恢复原始指令
                if (patchFileProtobufFunc1.readU32() === 3573751839) {
                    Memory.patchCode(patchFileProtobufFunc1, 4, code => {
                        const cw = new Arm64Writer(code, { pc: patchFileProtobufFunc1 });
                        cw.putBytes(new Uint8Array(patchFileProtobufFunc1Byte));
                        cw.flush();
                    });
                    Memory.patchCode(patchFileProtobufFunc2, 4, code => {
                        const cw = new Arm64Writer(code, { pc: patchFileProtobufFunc2 });
                        cw.putBytes(new Uint8Array(patchFileProtobufFunc2Byte));
                        cw.flush();
                    });
                    Memory.patchCode(fileProtobufDeleteAddr, 4, code => {
                        const cw = new Arm64Writer(code, { pc: fileProtobufDeleteAddr });
                        cw.putBytes(new Uint8Array(fileProtobufDeleteAddrByte));
                        cw.flush();
                    });
                }
            }
        }
    });
}

setImmediate(patchFileProtoBuf);


// =========================== Interceptor Hook: 文件 Protobuf 构建 ===========================

function attachFileProto() {
    Interceptor.attach(fileProtobufAddr, {
        onEnter: function (args) {
            var currTaskId = this.context.sp.add(0x30).readU32();
            if (currTaskId !== taskIdGlobal) {
                console.log(`[+] 文件拦截到非目标 currTaskId: ${currTaskId} taskIdGlobal: ${taskIdGlobal}`);
                return;
            }

            // 从队列获取上传完成信息
            const fileUploadInfo = getFileUploadInfo();
            if (!fileUploadInfo) {
                console.error("[!] 无法获取文件上传信息");
                return;
            }

            const cdnKey = fileUploadInfo.cdnKey;
            const aesKey = fileUploadInfo.aesKey;
            const md5Key = fileUploadInfo.md5Key;
            const targetId = fileUploadInfo.targetId;
            const fileName = fileUploadInfo.fileName || "";
            const fileSize = fileUploadInfo.fileSize || "0";
            const appId = fileUploadInfo.appId || "";
            const fileExt = fileUploadInfo.fileExt || "";
            const fileUploadToken = fileUploadInfo.fileUploadToken || "";

            // --- 构建 Protobuf Header (Field 1) ---
            const type = [0x0A, 0x40, 0x0A, 0x01, 0x00];
            const msgId = [0x10].concat(generateRandom5ByteVarint());
            const cpHeader = [0x1A, 0x10];
            const randomId = [0x20, 0x9D, 0xB0, 0x90, 0x93, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x01];
            const sysHeader = [0x2A, 0x15];
            // "UnifiedPCMac 26 arm64"
            const sys = [0x55, 0x6E, 0x69, 0x66, 0x69, 0x65, 0x64, 0x50, 0x43, 0x4D, 0x61, 0x63, 0x20, 0x32, 0x36, 0x20, 0x61, 0x72, 0x6D, 0x36, 0x34];
            const sysEnd = [0x30, 0xF8, 0x01];

            // --- 构建 Protobuf Body (Field 2) ---
            const senderBytes = stringToHexArray(senderGlobal);
            const senderHeader = [0x0A, senderBytes.length];

            const appIdBytes = stringToHexArray(appId);
            const appIdHeader = [0x12, appIdBytes.length];

            const field3 = [0x18, 0x00];

            const receiverBytes = stringToHexArray(targetId);
            const receiverHeader = [0x22, receiverBytes.length];

            const msgType = [0x28, 0x06]; // type=6 文件

            // 构建 appmsg XML
            const cdnAttachUrl = cdnKey.replace(/_[^_]*$/, '');
            const appmsgXml = '<appmsg appid="' + appId + '" sdkver="0">' +
                '<title>' + fileName + '</title>' +
                '<des></des>' +
                '<action></action>' +
                '<type>6</type>' +
                '<showtype>0</showtype>' +
                '<soundtype>0</soundtype>' +
                '<mediatagname></mediatagname>' +
                '<messageext></messageext>' +
                '<messageaction></messageaction>' +
                '<content></content>' +
                '<contentattr>0</contentattr>' +
                '<url></url>' +
                '<lowurl></lowurl>' +
                '<dataurl></dataurl>' +
                '<lowdataurl></lowdataurl>' +
                '<songalbumurl></songalbumurl>' +
                '<songlyric></songlyric>' +
                '<template_id></template_id>' +
                '<appattach>' +
                '<totallen>' + fileSize + '</totallen>' +
                '<attachid>' + cdnKey + '</attachid>' +
                '<emoticonmd5></emoticonmd5>' +
                '<fileext>' + fileExt + '</fileext>' +
                '<cdnattachurl>' + cdnAttachUrl + '</cdnattachurl>' +
                '<aeskey>' + aesKey + '</aeskey>' +
                '<encryver>0</encryver>' +
                '<overwrite_newmsgid>' + Math.floor(Date.now() * 1000000 + Math.random() * 1000000).toString() + '</overwrite_newmsgid>' +
                (fileUploadToken ? '<fileuploadtoken>' + fileUploadToken + '</fileuploadtoken>' : '') +
                '</appattach>' +
                '<extinfo></extinfo>' +
                '<sourceusername></sourceusername>' +
                '<sourcedisplayname></sourcedisplayname>' +
                '<thumburl></thumburl>' +
                '<md5>' + md5Key + '</md5>' +
                '<statextstr></statextstr>' +
                '</appmsg>';
            const appmsgBytes = stringToHexArray(appmsgXml);

            const fromUsernameXml = '<fromusername>' + senderGlobal + '</fromusername>';
            const fromUsernameBytes = stringToHexArray(fromUsernameXml);

            const contentBytes = appmsgBytes.concat(fromUsernameBytes);
            const contentHeader = [0x32].concat(toVarint(contentBytes.length));

            const tsHeader = [0x38];
            const tsBytes = getVarintTimestampBytes();

            // msgid: wxid_xxx_timestamp_3_xwechat_9
            const receiverMsgId = stringToHexArray(targetId).concat([0x5F])
                .concat(stringToHexArray(Math.floor(Date.now() / 1000).toString()))
                .concat([0x5F, 0x33, 0x5F, 0x78, 0x77, 0x65, 0x63, 0x68, 0x61, 0x74, 0x5F, 0x39]);
            const msgIdHeader2 = [0x42, receiverMsgId.length];

            const field10 = [0x50, 0x01];

            const msgsourceXml = '<msgsource><alnode><fr>1</fr><cf>2</cf></alnode></msgsource>';
            const msgsourceBytes = stringToHexArray(msgsourceXml);
            const msgsourceHeader = [0x62, msgsourceBytes.length];

            const field13 = [0x6A, 0x00];
            const field14 = [0x72, 0x00];
            const field15 = [0x7A, 0x00];

            // 组装 field 2 内部数据
            const field2Inner = senderHeader.concat(senderBytes, appIdHeader, appIdBytes, field3,
                receiverHeader, receiverBytes, msgType, contentHeader, contentBytes,
                tsHeader, tsBytes, msgIdHeader2, receiverMsgId, field10,
                msgsourceHeader, msgsourceBytes, field13, field14, field15);

            const field2HeaderBytes = [0x12].concat(toVarint(field2Inner.length));

            // --- 构建 Protobuf 尾部字段 ---
            const md5Trailing = [0x2A, 0x20].concat(stringToHexArray(md5Key));
            const field9 = [0x48, 0x01];
            const fileSizeVarint = toVarint(parseInt(fileSize));
            const field10b = [0x50].concat(fileSizeVarint);
            const field11 = [0x58, 0x02];

            // --- 组装最终 Payload ---
            const finalPayload = type.concat(msgId, cpHeader, fileCp, randomId, sysHeader, sys, sysEnd,
                field2HeaderBytes, field2Inner,
                md5Trailing, field9, field10b, field11);

            fileProtoX1PayloadAddr.writeByteArray(finalPayload);
            console.log("[+] 文件Payload 已写入，长度: " + finalPayload.length);

            this.context.x1 = fileProtoX1PayloadAddr;
            this.context.x2 = ptr(finalPayload.length);

            console.log("[+] 文件寄存器修改完成: X1=" + this.context.x1 + ", X2=" + this.context.x2, hexdump(fileProtoX1PayloadAddr, {
                offset: 0,
                length: Math.min(finalPayload.length, 512),
                header: true,
                ansi: true
            }));
        },
    });
}

setImmediate(attachFileProto);


// =========================== RPC 导出 ===========================

rpc.exports = {
    triggerUploadFile: triggerUploadFile,
    triggerSendFileMessage: triggerSendFileMessage,
};
