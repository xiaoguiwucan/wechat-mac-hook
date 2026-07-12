// -------------------------Reply消息全局变量分区-------------------------

// 模块信息 - 需要与file.js保持一致
var moduleName = "wechat.dylib";
var baseAddr = Process.findModuleByName(moduleName).base;

// 全局变量
var sendFuncAddr = baseAddr.add(0x4992040);
var req2bufEnterAddr = baseAddr.add(0x380b950);
var req2bufExitAddr = baseAddr.add(0x380CA64);
var taskIdGlobal = 0x0;
var lastSendTime = 0;
var receiverGlobal = "wxid_";
var senderGlobal = "wxid_";
var sendMsgType = "";
var triggerX0;
var triggerX1Payload;
var insertMsgAddr;

// 回复消息回调函数地址 (基于用户提供: 0x24BDE40)
var replyCallbackFuncAddr = baseAddr.add(0x260C444);
var replyProtobufAddr = replyCallbackFuncAddr.add(0x50);
var patchReplyProtobufFunc1 = replyCallbackFuncAddr.add(0x10);
var patchReplyProtobufFunc1Byte;
var patchReplyProtobufFunc2 = replyCallbackFuncAddr.add(0x30);
var patchReplyProtobufFunc2Byte;
var replyProtobufDeleteAddr = replyCallbackFuncAddr.add(0x6c);
var replyProtobufDeleteAddrByte;
var replyMessageCallbackFunc1 = baseAddr.add(0x8C29A08); // 复用图片的callback函数

// Reply消息相关地址 - 需要导出为全局变量供file.js使用
var sendReplyMessageAddr = ptr(0);
var replyMessageAddr = ptr(0);
var replyCgiAddr = ptr(0);
var replyProtobufBufAddr = ptr(0); // 用于存储protobuf数据的缓冲区


// Reply消息内容相关
var replyContent = ""; // 回复的文本内容
var replyReferMsgContent = ""; // 被引用消息的内容
var replyReferMsgType = 1; // 被引用消息的类型
var replyReferMsgSender = ""; // 被引用消息的发送者
var replyReferMsgId = ""; // 被引用消息的ID
var replyReferMsgCreateTime = ""; // 被引用消息的创建时间
var replyChatRoom = ""; // 是否是群聊
var replyFromUser = ""; // 回复者wxid

// -------------------------Reply消息全局变量分区-------------------------


// -------------------------发送Reply消息动态初始化-------------------------
function setupSendReplyMessageDynamic() {
    console.log("[+] Starting setupSendReplyMessageDynamic Dynamic Message Patching...");

    // 1. 动态分配内存块
    sendReplyMessageAddr = Memory.alloc(256);
    replyMessageAddr = Memory.alloc(256);
    replyCgiAddr = Memory.alloc(128);
    replyProtobufBufAddr = Memory.alloc(3069);

    // 写入reply的cgi路径
    patchString(replyCgiAddr, "/cgi-bin/micromsg-bin/sendappmsg");

    // 初始化sendReplyMessageAddr结构
    sendReplyMessageAddr.add(0x00).writeU64(0);
    sendReplyMessageAddr.add(0x08).writeU64(0);
    sendReplyMessageAddr.add(0x10).writeU64(0);
    sendReplyMessageAddr.add(0x18).writeU64(1);
    sendReplyMessageAddr.add(0x20).writeU32(taskIdGlobal);
    sendReplyMessageAddr.add(0x28).writePointer(replyMessageAddr);

    // 初始化replyMessageAddr结构
    replyMessageAddr.add(0x00).writePointer(replyMessageCallbackFunc1);
    replyMessageAddr.add(0x08).writeU32(taskIdGlobal);
    replyMessageAddr.add(0x0c).writeU32(0x6e);
    replyMessageAddr.add(0x10).writeU64(0x3);
    replyMessageAddr.add(0x18).writePointer(replyCgiAddr);  // 添加cgi地址
    replyMessageAddr.add(0x20).writeU64(0x22);
    replyMessageAddr.add(0x28).writeU64(uint64("0x8000000000000030"));
    replyMessageAddr.add(0x30).writeU64(uint64("0x0000000001010100"));

    // 保存原始字节用于恢复
    patchReplyProtobufFunc1Byte = patchReplyProtobufFunc1.readByteArray(4);
    patchReplyProtobufFunc2Byte = patchReplyProtobufFunc2.readByteArray(4);
    replyProtobufDeleteAddrByte = replyProtobufDeleteAddr.readByteArray(4);
}

setImmediate(setupSendReplyMessageDynamic);


// -------------------------Patch Reply protobuf-------------------------
function patchReplyProtoBuf() {
    // attach到replyProtobufAddr (replyCallbackFuncAddr + 0x50) 来修改protobuf
    Interceptor.attach(replyCallbackFuncAddr, {
        onEnter: function (args) {
            var firstValue = this.context.sp.add(0x10).readU32();
            console.log("[+] 捕获到 ReplyProtobufAddr 调用，firstValue：", firstValue, "taskIdGlobal:", taskIdGlobal);

            if (firstValue === taskIdGlobal) {
                // 不匹配时，patch掉这些函数（NOP替换）
                console.log("[+] Reply taskId匹配，开始patch...");
                if (patchReplyProtobufFunc1.readU32() !== 3573751839) {
                    Memory.patchCode(patchReplyProtobufFunc1, 4, code => {
                        const cw = new Arm64Writer(code, {pc: patchReplyProtobufFunc1});
                        cw.putNop();
                        cw.flush();
                    });
                    Memory.patchCode(patchReplyProtobufFunc2, 4, code => {
                        const cw = new Arm64Writer(code, {pc: patchReplyProtobufFunc2});
                        cw.putNop();
                        cw.flush();
                    });
                    Memory.patchCode(replyProtobufDeleteAddr, 4, code => {
                        const cw = new Arm64Writer(code, {pc: replyProtobufDeleteAddr});
                        cw.putNop();
                        cw.flush();
                    });
                }
            } else {
                // 匹配时，恢复原来的字节
                console.log("[+] Reply taskId不匹配，开始修改protobuf...");
                if (patchReplyProtobufFunc1.readU32() === 3573751839) {
                    Memory.patchCode(patchReplyProtobufFunc1, 4, code => {
                        const cw = new Arm64Writer(code, {pc: patchReplyProtobufFunc1});
                        cw.putBytes(new Uint8Array(patchReplyProtobufFunc1Byte));
                        cw.flush();
                    });
                    Memory.patchCode(patchReplyProtobufFunc2, 4, code => {
                        const cw = new Arm64Writer(code, {pc: patchReplyProtobufFunc2});
                        cw.putBytes(new Uint8Array(patchReplyProtobufFunc2Byte));
                        cw.flush();
                    });
                    Memory.patchCode(replyProtobufDeleteAddr, 4, code => {
                        const cw = new Arm64Writer(code, {pc: replyProtobufDeleteAddr});
                        cw.putBytes(new Uint8Array(replyProtobufDeleteAddrByte));
                        cw.flush();
                    });
                }
            }
        }
    });
}

setImmediate(patchReplyProtoBuf);

function attachProto() {

    Interceptor.attach(replyProtobufAddr, {
        onEnter: function (args) {

            var currTaskId = this.context.sp.add(0x30).readU32();
            if (currTaskId !== taskIdGlobal) {
                console.log(`[+] 拦截到非目标 currTaskId: ${currTaskId} taskIdGlobal: ${taskIdGlobal}` + hexdump(this.context.x1, {
                    offset: 0,
                    length: 3069,
                    header: true,
                    ansi: true
                }));
                return;
            }

            const replyProtobuf = buildReplyProtobuf();

            // 写入protobuf数据到缓冲区
            replyProtobufBufAddr.writeByteArray(replyProtobuf);
            console.log("[+] Reply protobuf写入到缓冲区: ", replyProtobufBufAddr);

            // 设置x1指向缓冲区地址，x2为长度
            this.context.x1 = replyProtobufBufAddr;
            this.context.x2 = ptr(replyProtobuf.length);

            console.log("[+] Reply寄存器修改完成: X1=" + this.context.x1 + ", X2=" + this.context.x2, hexdump(replyProtobufBufAddr, {
                offset: 0,
                length: 3069,
                header: true,
                ansi: true
            }));
        }
    });
}

setImmediate(attachProto);


// -------------------------触发发送Reply消息-------------------------
function triggerSendReplyMessage(taskId, sender, receiver, content, referMsgContent, referMsgType, referMsgSender, referMsgId, chatRoom, referMsgCreateTime) {
    console.log("[+] Manual Trigger Reply Message Started...");

    if (!taskId || !receiver || !sender) {
        console.error("[!] taskId or receiver or sender is empty!");
        return "fail";
    }

    // 更新全局变量
    const timestamp = Math.floor(Date.now() / 1000);
    lastSendTime = timestamp;
    taskIdGlobal = taskId;
    receiverGlobal = receiver;
    senderGlobal = sender;
    replyContent = content || "";
    replyReferMsgContent = referMsgContent || "";
    replyReferMsgType = referMsgType || 1;
    replyReferMsgSender = referMsgSender || sender;
    replyReferMsgId = referMsgId || generateRandomMsgId();
    replyReferMsgCreateTime = referMsgCreateTime || Math.floor(Date.now() / 1000).toString();
    replyChatRoom = chatRoom || "";
    replyFromUser = sender;

    // 更新消息地址的taskId
    replyMessageAddr.add(0x08).writeU32(taskIdGlobal);
    sendReplyMessageAddr.add(0x20).writeU32(taskIdGlobal);

    console.log("start init reply payload");

    // 使用与triggerSendImgMessage类似的payload结构
    const payloadData = [
        0x6e, 0x00, 0x00, 0x00,                         // 0x00
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x08
        0x03, 0x00, 0x00, 0x00, 0x10, 0x00, 0x00, 0x00, // 0x10
        0x40, 0xec, 0x0e, 0x12, 0x01, 0x00, 0x00, 0x00, // 0x18
        0x22, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x20 cgi的长度
        0x30, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x80, // 0x28
        0x00, 0x01, 0x01, 0x01, 0x00, 0xAA, 0xAA, 0xAA, // 0x30
        0x00, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00, 0x00, // 0x38
        0x01, 0x00, 0x00, 0x00, 0xFF, 0xFF, 0xFF, 0xFF, // 0x40
        0xFF, 0xFF, 0xFF, 0xFF, 0x00, 0xAA, 0xAA, 0xAA, // 0x48
        0xFF, 0xFF, 0xFF, 0xFF, 0xAA, 0xAA, 0xAA, 0xAA, // 0x50
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x58
        0x6e, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, // 0x60
        0x64, 0x65, 0x66, 0x61, 0x75, 0x6C, 0x74, 0x2D, // 0x68 default-
        0x6C, 0x6F, 0x6E, 0x67, 0x6C, 0x69, 0x6E, 0x6B, // 0x70 longlink
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
    triggerX1Payload.add(0x18).writePointer(replyCgiAddr);  // 使用reply的cgi
    triggerX1Payload.add(0xb8).writePointer(triggerX1Payload.add(0xc0));
    triggerX1Payload.add(0x190).writePointer(triggerX1Payload.add(0x198));

    sendMsgType = "reply";

    console.log("finished init reply payload");

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


// -------------------------构建Reply消息Protobuf-------------------------
function buildReplyProtobuf() {
    // 构建appmsg的XML内容
    const appmsgXml = buildReplyAppmsgXml();

    // 根据用户提供的protobuf格式构建
    const payload = [];

    // 先构建内部数据
    const innerData = [];

    // 字段1: 0x0A 0x01 0x00 (固定)
    innerData.push(0x0A, 0x01, 0x00);

    // 字段2: 0x10 + varint (时间戳)
    innerData.push(0x10);
    const timestampVarint = encodeVarint(Math.floor(Date.now() / 1000));
    innerData.push(...timestampVarint);

    // 字段3: 0x1A 0x10 + md5 (16字节)
    innerData.push(0x1A, 0x10);
    const md5Bytes = stringToUtf8Bytes(generateRandomMd5());
    innerData.push(...md5Bytes);

    // 字段4: 0x20 + varint (大整数 0x1FF... )
    innerData.push(0x20, 0x9D, 0xB0, 0x90, 0x93, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x01);

    // 字段5: 0x2A 0x15 + "UnifiedPCMac 26 arm64"
    innerData.push(0x2A, 0x15);
    innerData.push(...stringToUtf8Bytes("UnifiedPCMac 26 arm64"));

    const fromUsrBytes = stringToUtf8Bytes(senderGlobal);
    innerData.push(0x30, 0xa8, 0x01, 0x12, 0xb9);
    innerData.push(...encodeVarint(fromUsrBytes.length + 2));
    innerData.push(0x0a)
    innerData.push(...encodeVarint(fromUsrBytes.length));
    innerData.push(...fromUsrBytes);

    innerData.push(0x12, 0x00, 0x18, 0x00);
    const receiverBytes = stringToUtf8Bytes(receiverGlobal)
    innerData.push(0x22);
    innerData.push(...encodeVarint(receiverBytes.length));
    innerData.push(...receiverBytes);


    // 结尾 varint
    innerData.push(0x28, 0x39, 0x32);
    const appmsgBytes = stringToUtf8Bytes(appmsgXml);
    innerData.push(...encodeVarint(appmsgBytes.length))
    innerData.push(...appmsgBytes)

    innerData.push(0x38, 0xb2, 0xe4, 0x87, 0xce, 0x06, 0x42)

    const receiverMsgId = receiverBytes.concat([0x5F])
        .concat(stringToUtf8Bytes(Math.floor(Date.now() / 1000).toString()))
        .concat([0x5F, 0x31, 0x36, 0x30, 0x5F, 0x78, 0x77, 0x65, 0x63, 0x68, 0x61, 0x74, 0x5F, 0x33]);
    innerData.push(...encodeVarint(receiverMsgId.length))
    innerData.push(...receiverMsgId)

    innerData.push(0x50, 0x01, 0x62, 0x32, 0x3c, 0x6d, 0x73, 0x67, 0x73, 0x6f, 0x75, 0x72,
        0x63, 0x65, 0x3e, 0x3c, 0x61, 0x6c, 0x6e, 0x6f, 0x64, 0x65, 0x3e, 0x3c,
        0x66, 0x72, 0x3e, 0x31, 0x3c, 0x2f, 0x66, 0x72, 0x3e, 0x3c, 0x2f, 0x61,
        0x6c, 0x6e, 0x6f, 0x64, 0x65, 0x3e, 0x3c, 0x2f, 0x6d, 0x73, 0x67, 0x73,
        0x6f, 0x75, 0x72, 0x63, 0x65, 0x3e);

    // 结尾字段 (根据成功版本)
    // 字段12: 0x6A (field 6) + 0x00
    innerData.push(0x6A, 0x00);
    // 字段13: 0x72 (field 7) + 0x00
    innerData.push(0x72, 0x00);
    // 字段14: 0x7A (field 8) + 0x00
    innerData.push(0x7A, 0x00);
    // 字段15: 0x2A (field 9) + 0x00
    innerData.push(0x2A, 0x00);
    // 字段16: 0x48 (field 10) + 0x00
    innerData.push(0x48, 0x00);
    // 字段17: 0x50 (field 11) + 0x00
    innerData.push(0x50, 0x00);
    // 字段18: 0x58 (field 12) + 0x02 + 10个0
    innerData.push(0x58);
    innerData.push(0x02);

    payload.push(0x0A, 0x40);
    payload.push(...innerData);

    return payload;
}

// 生成随机MD5格式字符串 (16字节)
function generateRandomMd5() {
    let result = "md4";
    for (let i = 0; i < 13; i++) {
        result += Math.floor(Math.random() * 10).toString();
    }
    return result;
}

// 生成接收者消息ID
function generateReceiverMsgId() {
    const timestamp = Math.floor(Date.now() / 1000);
    const receiver = replyChatRoom || receiverGlobal;
    return receiver + "_" + timestamp + "_" + Math.floor(Math.random() * 100) + "_xwechat_3";
}

// Varint编码
function encodeVarint(value) {
    const result = [];
    if (value === 0) {
        result.push(0);
        return result;
    }
    while (value > 0) {
        let byte = value & 0x7F;
        value >>= 7;
        if (value > 0) {
            byte |= 0x80;
        }
        result.push(byte);
    }
    return result;
}


// -------------------------构建Reply Appmsg XML-------------------------
function buildReplyAppmsgXml() {
    // 构建appmsg XML内容
    // type=57 是reply消息类型

    let xml = "<appmsg appid=\"\" sdkver=\"0\">";
    xml += "<title>" + escapeXml(replyContent) + "</title>";
    xml += "<des></des>";
    xml += "<action></action>";
    xml += "<type>57</type>";
    xml += "<showtype>0</showtype>";
    xml += "<soundtype>0</soundtype>";
    xml += "<mediatagname></mediatagname>";
    xml += "<messageext></messageext>";
    xml += "<messageaction></messageaction>";
    xml += "<content></content>";
    xml += "<contentattr>0</contentattr>";
    xml += "<url></url>";
    xml += "<lowurl></lowurl>";
    xml += "<dataurl></dataurl>";
    xml += "<lowdataurl></lowdataurl>";
    xml += "<songalbumurl></songalbumurl>";
    xml += "<songlyric></songlyric>";
    xml += "<template_id></template_id>";
    xml += "<appattach><totallen>0</totallen><attachid></attachid><emoticonmd5></emoticonmd5><fileext></fileext><aeskey></aeskey></appattach>";
    xml += "<extinfo></extinfo>";
    xml += "<sourceusername></sourceusername>";
    xml += "<sourcedisplayname></sourcedisplayname>";
    xml += "<thumburl></thumburl>";
    xml += "<md5></md5>";
    xml += "<statextstr></statextstr>";

    // 添加refermsg (引用消息)
    if (replyReferMsgContent || replyReferMsgSender) {
        xml += "<refermsg>";
        if (replyReferMsgSender) {
            xml += "<chatusr>" + escapeXml(replyReferMsgSender) + "</chatusr>";
        }
        xml += "<type>" + replyReferMsgType + "</type>";
        xml += "<createtime>" + replyReferMsgCreateTime + "</createtime>";
        xml += "<msgsource>" + escapeXml("<msgsource><alnode><fr>1</fr></alnode></msgsource>") + "</msgsource>";
        xml += "<displayname></displayname>";
        xml += "<svrid>" + escapeXml(replyReferMsgId || generateRandomMsgId()) + "</svrid>";
        xml += "<fromusr>" + escapeXml(replyReferMsgSender) + "</fromusr>";
        xml += "<content>" + escapeXml(replyReferMsgContent) + "</content>";
        xml += "</refermsg>";
    }

    xml += "</appmsg>";

    // 添加fromusername
    xml += "<fromusername>" + escapeXml(senderGlobal) + "</fromusername>";

    return xml;
}

// 生成UUID
function generateUuid() {
    return 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'.replace(/x/g, function (c) {
        var r = Math.random() * 16 | 0, v = c == 'x' ? r : (r & 0x3 | 0x8);
        return v.toString(16);
    }).replace(/-/g, '');
}

function AttachSendTextProto() {
    Interceptor.attach(sendFuncAddr.add(0x08), {
        onEnter: function (args) {
            if (triggerX1Payload) {
                return
            }

            triggerX0 = this.context.x0;
            triggerX1Payload = this.context.x1;
            console.log(`[+] 捕获到 StartTask 调用，X0地址：${triggerX0}, Payload 地址: ${triggerX1Payload}`);
        }
    })
}

setImmediate(AttachSendTextProto);


// -------------------------Reply消息Req2Buf拦截-------------------------
function attachReplyReq2buf() {
    console.log("[+] Target Reply Req2buf enter Address: " + req2bufEnterAddr);

    // 拦截入口
    Interceptor.attach(req2bufEnterAddr, {
        onEnter: function (args) {
            if (!this.context.x1.equals(taskIdGlobal)) {
                return;
            }

            console.log("[+] 已命中Reply目标Req2Buf地址 taskId:" + taskIdGlobal);

            // 获取 X24 寄存器的值
            const x24_base = this.context.x24;
            insertMsgAddr = x24_base.add(0x60);
            console.log("[+] 当前 Reply Req2Buf X24 基址: " + x24_base);

            if (sendMsgType === "reply") {
                insertMsgAddr.writePointer(sendReplyMessageAddr);
                console.log("[+] 发送回复消息成功! Req2Buf 已将 X24+0x60 指向新地址: " + sendReplyMessageAddr);
            }
        }
    });

    // 拦截出口
    console.log("[+] Target Reply Req2buf leave Address: " + req2bufExitAddr);
    Interceptor.attach(req2bufExitAddr, {
        onEnter: function (args) {
            if (!this.context.x25.equals(taskIdGlobal)) {
                return;
            }
            insertMsgAddr.writeU64(0x0);
            console.log("[+] Reply Req2Buf 清空完成");
            taskIdGlobal = 0;
            receiverGlobal = "wxid_";
            senderGlobal = "wxid_";
        }
    });
}

setImmediate(attachReplyReq2buf);


// -------------------------辅助函数-------------------------

// 字符串转UTF8字节数组
function stringToUtf8Bytes(str) {
    const utf8 = [];
    for (let i = 0; i < str.length; i++) {
        let charcode = str.charCodeAt(i);
        if (charcode < 0x80) utf8.push(charcode);
        else if (charcode < 0x800) {
            utf8.push(0xc0 | (charcode >> 6),
                0x80 | (charcode & 0x3f));
        } else if (charcode < 0x10000) {
            utf8.push(0xe0 | (charcode >> 12),
                0x80 | ((charcode >> 6) & 0x3f),
                0x80 | (charcode & 0x3f));
        } else {
            utf8.push(0xf0 | (charcode >> 18),
                0x80 | ((charcode >> 12) & 0x3f),
                0x80 | ((charcode >> 6) & 0x3f),
                0x80 | (charcode & 0x3f));
        }
    }
    return utf8;
}

// XML转义
function escapeXml(str) {
    if (!str) return "";
    return str.replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&apos;");
}

// 生成随机消息ID
function generateRandomMsgId() {
    let result = '';
    for (let i = 0; i < 16; i++) {
        result += Math.floor(Math.random() * 10).toString();
    }
    return result;
}

// 辅助函数: 将字符串写入内存
function patchString(addr, str) {
    for (let i = 0; i < str.length; i++) {
        addr.add(i).writeU8(str.charCodeAt(i));
    }
    addr.add(str.length).writeU8(0);
}

// -------------------------RPC导出函数-------------------------

// 发送Reply消息的RPC接口
// 参数: taskId, sender, receiver, content, referMsgContent, referMsgType, referMsgSender, referMsgId, chatRoom
rpc.exports = {
    triggerSendReplyMessage: triggerSendReplyMessage
};
