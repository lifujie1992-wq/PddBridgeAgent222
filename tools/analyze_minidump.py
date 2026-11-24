# -*- coding: utf-8 -*-
"""最小 minidump 解析：定位崩溃异常、模块归属、崩溃线程栈里的调用链。

不依赖 WinDbg（本机没装）。只解析这几条流：
  3 ThreadList / 4 ModuleList / 6 Exception
"""
from __future__ import annotations

import struct
import sys

STREAM_THREAD_LIST = 3
STREAM_MODULE_LIST = 4
STREAM_EXCEPTION = 6

EXC_NAMES = {
    0xC0000005: "ACCESS_VIOLATION",
    0xC000001D: "ILLEGAL_INSTRUCTION",
    0xC00000FD: "STACK_OVERFLOW",
    0xC0000094: "INT_DIVIDE_BY_ZERO",
    0xC0000409: "STACK_BUFFER_OVERRUN / __fastfail",
    0xC0000374: "HEAP_CORRUPTION",
    0x80000003: "BREAKPOINT",
}


def read_minidump(path):
    data = open(path, "rb").read()
    if data[:4] != b"MDMP":
        raise SystemExit("不是 minidump: %r" % data[:4])
    _ver, nstreams, dir_rva = struct.unpack_from("<III", data, 4)
    streams = {}
    for i in range(nstreams):
        stype, size, rva = struct.unpack_from("<III", data, dir_rva + i * 12)
        streams.setdefault(stype, (size, rva))
    return data, streams


def parse_modules(data, rva):
    count = struct.unpack_from("<I", data, rva)[0]
    off = rva + 4
    modules = []
    for _ in range(count):
        base, size = struct.unpack_from("<QI", data, off)
        name_rva = struct.unpack_from("<I", data, off + 20)[0]
        name_len = struct.unpack_from("<I", data, name_rva)[0]
        name = data[name_rva + 4: name_rva + 4 + name_len].decode("utf-16-le", "replace")
        modules.append({"base": base, "size": size, "name": name})
        off += 108
    return modules


def parse_threads(data, rva):
    count = struct.unpack_from("<I", data, rva)[0]
    off = rva + 4
    threads = []
    for _ in range(count):
        tid = struct.unpack_from("<I", data, off)[0]
        stack_start, stack_size, stack_rva = struct.unpack_from("<QII", data, off + 24)
        threads.append({"id": tid, "stack": (stack_start, stack_size, stack_rva)})
        off += 48
    return threads


def parse_exception(data, rva):
    tid = struct.unpack_from("<I", data, rva)[0]
    rec = rva + 8
    (code, flags, _nested, address, nparams) = struct.unpack_from("<IIQQI", data, rec)
    params = struct.unpack_from("<15Q", data, rec + 32)
    return {"thread": tid, "code": code, "flags": flags, "address": address,
            "params": params[:nparams]}


def module_of(modules, addr):
    for m in modules:
        if m["base"] <= addr < m["base"] + m["size"]:
            return m
    return None


def main(dump_path):
    data, streams = read_minidump(dump_path)
    modules = parse_modules(data, streams[STREAM_MODULE_LIST][1]) if STREAM_MODULE_LIST in streams else []
    threads = parse_threads(data, streams[STREAM_THREAD_LIST][1]) if STREAM_THREAD_LIST in streams else []
    print("dump       : %s (%.0f KB)" % (dump_path, len(data) / 1024))
    print("modules    : %d | threads: %d" % (len(modules), len(threads)))

    susp = [m for m in modules if "pdd_send" in m["name"].lower()]
    for m in susp:
        print("  注入模块 : %s base=0x%X size=0x%X" % (m["name"], m["base"], m["size"]))

    if STREAM_EXCEPTION not in streams:
        print("没有 ExceptionStream（可能不是崩溃转储）")
        return
    exc = parse_exception(data, streams[STREAM_EXCEPTION][1])
    name = EXC_NAMES.get(exc["code"], "0x%08X" % exc["code"])
    print("\n== 异常 ==")
    print("  code    : 0x%08X (%s)" % (exc["code"], name))
    print("  address : 0x%X" % exc["address"])
    if exc["params"]:
        kind = {0: "read", 1: "write", 8: "execute"}.get(exc["params"][0], str(exc["params"][0]))
        print("  访问类型: %s 目标地址: 0x%X" % (kind, exc["params"][1] if len(exc["params"]) > 1 else 0))
    owner = module_of(modules, exc["address"])
    print("  归属    : %s + 0x%X" % (owner["name"] if owner else "<未映射>",
                                     exc["address"] - owner["base"] if owner else 0))

    # 崩溃线程栈里的调用链（只报 pdd_send* 与 pddwebworkbench 内的返回地址）
    target = next((t for t in threads if t["id"] == exc["thread"]), None)
    if target is None:
        return
    start, size, rva = target["stack"]
    print("\n== 崩溃线程 tid=%d 栈内返回地址（最多 25 条） ==" % target["id"])
    seen = 0
    for off in range(0, min(size, 64 * 1024) - 8, 8):
        value = struct.unpack_from("<Q", data, rva + off)[0]
        m = module_of(modules, value)
        if not m:
            continue
        if "pdd_send" not in m["name"].lower() and "workbench" not in m["name"].lower():
            continue
        print("  [0x%X] %s + 0x%X" % (start + off, m["name"], value - m["base"]))
        seen += 1
        if seen >= 25:
            break
    if not seen:
        print("  (栈里没有落在 DLL/工作台内的返回地址)")


if __name__ == "__main__":
    main(sys.argv[1])
