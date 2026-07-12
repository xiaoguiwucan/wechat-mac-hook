import idc

cgiAddress = 0x1120eec40
callBackFuncAddress = 0x1120eec70

sendMessageAddress = 0x1120eed40 # 40 ed 0e 12 01
MessageAddress = 0x1120eee40

MessageContentAddress = 0x1120eed70 # 70 ed 0e 12 01
MessageAddrAddr = 0x1120eed80
ReceiverAddress = 0x1120eeb10
ReceiverAddrAddr = 0x1120eeb20

ContentAddr = 0x1120eeb50
HtmlAddr = 0x1120eeb60
HtmlAddrAddr = 0x1120eeb70


def setup_send_message():
    patch_string_at_address(cgiAddress,
                            "2F 63 67 69 2D 62 69 6E 2F 6D 69 63 72 6F 6D 73 67 2D 62 69 6E 2F 6E 65 77 73 65 6E 64 6D 73 67")

    idc.patch_qword(sendMessageAddress + 0x00, 0)  # num1_0
    idc.patch_qword(sendMessageAddress + 0x08, 0)  # num2_0
    idc.patch_qword(sendMessageAddress + 0x10, 0x10EDB4678)  # func1 (SendMessage 虚表)
    idc.patch_qword(sendMessageAddress + 0x18, 1)  # num3_1
    idc.patch_qword(sendMessageAddress + 0x20, 0x20000090)  # taskId
    idc.patch_qword(sendMessageAddress + 0x28, MessageAddress)  # message

    idc.patch_qword(MessageAddress + 0x00, 0x107f04f70)  # 回调函数
    idc.patch_dword(MessageAddress + 0x08, 0x20000090)  # taskId 4位
    idc.patch_dword(MessageAddress + 0x0c, 0x20a)  # cmdId 4位
    idc.patch_qword(MessageAddress + 0x10, 3)  # 数字
    idc.patch_qword(MessageAddress + 0x18, cgiAddress)  # param3
    idc.patch_qword(MessageAddress + 0x20, 0x0000000000000020)
    idc.patch_qword(MessageAddress + 0x28, 0x8000000000000030)
    idc.patch_qword(MessageAddress + 0x30, 0x0000000001010100)
    idc.patch_qword(MessageAddress + 0x38, 0)
    idc.patch_qword(MessageAddress + 0x40, 0)
    idc.patch_qword(MessageAddress + 0x48, 0)
    idc.patch_qword(MessageAddress + 0x50, 0)
    idc.patch_qword(MessageAddress + 0x58, 0x0101010100000001)
    idc.patch_qword(MessageAddress + 0x60, 0)
    idc.patch_qword(MessageAddress + 0x68, 0)
    idc.patch_qword(MessageAddress + 0x70, 0)
    idc.patch_qword(MessageAddress + 0x78, 0)
    idc.patch_qword(MessageAddress + 0x80, 0)
    idc.patch_qword(MessageAddress + 0x88, 0)
    idc.patch_qword(MessageAddress + 0x90, 0)

    idc.patch_qword(callBackFuncAddress, 0x107f04fc8)
    idc.patch_qword(MessageAddress + 0x98, callBackFuncAddress)
    idc.patch_qword(MessageAddress + 0xa0, 0)
    idc.patch_qword(MessageAddress + 0xa8, 0)
    idc.patch_qword(MessageAddress + 0xb0, 0)
    idc.patch_qword(MessageAddress + 0xb8, 0x10)
    idc.patch_qword(MessageAddress + 0xc0, MessageContentAddress)
    idc.patch_qword(MessageAddress + 0xc8, 0x0000000100000001)
    idc.patch_qword(MessageAddress + 0xd0, 4)
    idc.patch_qword(MessageAddress + 0xd8, 1)
    idc.patch_qword(MessageAddress + 0xe0, 1)
    idc.patch_qword(MessageAddress + 0xe8, 0x107f96a08)

    # 可能需要修改，再试一下
    idc.patch_qword(MessageContentAddress, MessageAddrAddr)

    idc.patch_qword(MessageAddrAddr, 0x107f968a0)
    idc.patch_qword(MessageAddrAddr+0x8, ContentAddr)
    patch_string_at_address(ContentAddr, "77 77 77")


def patch_string_at_address(target_addr, hex_str):
    data = bytes.fromhex(hex_str.replace(" ", "").replace("\n", ""))
    for i, byte in enumerate(data):
        idc.patch_byte(target_addr + i, byte)
    idc.patch_byte(target_addr + len(data), 0)  # 终止符
    print(f"Data at {hex(target_addr)} has been overwritten.")


setup_send_message()
