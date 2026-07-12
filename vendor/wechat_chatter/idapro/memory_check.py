import idc
import ida_dbg
import idautils
import ida_idd
import ida_name

def dbg_get_call_stack() -> list[dict[str, str]]:
    """Get the current call stack."""
    callstack = []
    try:
        tid = ida_dbg.get_current_thread()
        trace = ida_idd.call_stack_t()

        if not ida_dbg.collect_stack_trace(tid, trace):
            return []
        for frame in trace:
            frame_info = {
                "address": hex(frame.callea),
            }
            try:
                module_info = ida_idd.modinfo_t()
                if ida_dbg.get_module_info(frame.callea, module_info):
                    frame_info["module"] = os.path.basename(module_info.name)
                else:
                    frame_info["module"] = "<unknown>"

                name = (
                        ida_name.get_nice_colored_name(
                            frame.callea,
                            ida_name.GNCN_NOCOLOR
                            | ida_name.GNCN_NOLABEL
                            | ida_name.GNCN_NOSEG
                            | ida_name.GNCN_PREFDBG,
                            )
                        or "<unnamed>"
                )
                frame_info["symbol"] = name

            except Exception as e:
                frame_info["module"] = "<error>"
                frame_info["symbol"] = str(e)

            callstack.append(frame_info)

    except Exception as e:
        pass
    return callstack

def dbg_print_call_stack(callstack: list[dict[str, str]]):
    print("Call Stack:----------------------------------------")
    for i, frame in enumerate(reversed(callstack)):
        print(f"{i}: {frame['module']}.{frame['symbol']} @ {frame['address']}")



dbg_print_call_stack(dbg_get_call_stack())