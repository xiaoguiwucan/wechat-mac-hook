import ida_dbg
import ida_idaapi
import ida_bytes

def dbg_bpt(ea):
    # 获取X22寄存器的值（指向的内存地址）
    x22_value = ida_dbg.get_reg_val("X22")

    if x22_value != ida_idaapi.BADADDR:
        # 要写入的数据：'1','2','3' 的ASCII码
        patch_data = [ord('1'), ord('2'), ord('3')]

        # 修改内存
        for i, byte_val in enumerate(patch_data):
            ida_bytes.patch_byte(x22_value + i, byte_val)

        print(f"[断点 0x{ea:X}] 已将 X22(0x{x22_value:X}) 的前3字节修改为 '123'")

        # 可选：显示修改前后的内容对比
        original = []
        for i in range(3):
            original.append(ida_bytes.get_byte(x22_value + i))

        print(f"  修改前: {[hex(b) for b in original]}")
        print(f"  修改后: {[hex(b) for b in patch_data]}")

    # 返回0继续执行
    return 0

dbg_bpt(0x1006DDE30)