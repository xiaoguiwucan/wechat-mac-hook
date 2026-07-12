import idc

def patch_ptr_at_x0_offset_60(target_pointer_addr):
    """
    在 X0 寄存器指向的地址偏移 0x60 处写入一个新指针
    target_pointer_addr: 你想要填入的指针数值（目标地址）
    """
    # 1. 获取 X0 的当前地址
    x0_base = idc.get_reg_value("X0")
    target_field_ea = x0_base + 0x60

    # 3. 写入指针值 (AArch64 下指针为 8 字节)
    # idc.patch_qword 会自动处理小端序转换
    idc.patch_qword(target_field_ea, target_pointer_addr)

    print(f"[+] {target_pointer_addr} 操作成功!")

patch_ptr_at_x0_offset_60(0x1120eed40)