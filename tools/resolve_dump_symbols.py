# -*- coding: utf-8 -*-
"""把 minidump 里的 DLL 偏移映射回函数名。

DLL 是 MinGW 构建、**没有剥符号**（12073 个 COFF 符号），所以不需要 PDB：
直接解析 COFF 符号表（Value 是节内偏移，加上节 RVA），再按栈上返回地址查最近的函数。
"""
from __future__ import annotations

import bisect
import struct
import sys

import pefile

from analyze_minidump import (STREAM_EXCEPTION, STREAM_MODULE_LIST,
                              STREAM_THREAD_LIST, module_of, parse_exception,
                              parse_modules, parse_threads, read_minidump)


def load_function_symbols(dll_path):
    pe = pefile.PE(dll_path, fast_load=True)
    raw = open(dll_path, "rb").read()
    sections = [(s.VirtualAddress, s.Misc_VirtualSize) for s in pe.sections]
    table = pe.FILE_HEADER.PointerToSymbolTable
    count = pe.FILE_HEADER.NumberOfSymbols
    strtab = table + count * 18
    rows = []
    index = 0
    while index < count:
        entry = raw[table + index * 18: table + index * 18 + 18]
        if len(entry) < 18:
            break
        name8, value, secnum, stype, _sclass, naux = struct.unpack("<8sIhHBB", entry)
        if name8[:4] == b"\x00\x00\x00\x00":
            offset = struct.unpack("<I", name8[4:])[0]
            end = raw.find(b"\x00", strtab + offset)
            name = raw[strtab + offset: end if end > 0 else None].decode("utf-8", "replace")
        else:
            name = name8.rstrip(b"\x00").decode("utf-8", "replace")
        is_func = (stype & 0x20) == 0x20
        if is_func and 1 <= secnum <= len(sections) and value:
            rva = sections[secnum - 1][0] + value
            rows.append((rva, name))
        index += 1 + naux
    rows.sort()
    return rows


def resolve(rows, rva):
    idx = bisect.bisect_right(rows, (rva, "\uffff")) - 1
    if idx < 0:
        return None, 0
    base, name = rows[idx]
    return name, rva - base


def main(dump_path, dll_path):
    data, streams = read_minidump(dump_path)
    modules = parse_modules(data, streams[STREAM_MODULE_LIST][1])
    threads = parse_threads(data, streams[STREAM_THREAD_LIST][1])
    rows = load_function_symbols(dll_path)
    exc = parse_exception(data, streams[STREAM_EXCEPTION][1])

    print("函数符号: %d" % len(rows))
    check, delta = resolve(rows, 0xC460)
    print("自检 PDD_Init@0xC460 -> %s +0x%X" % (check, delta))

    print("\n== 异常 ==")
    print("  code=0x%08X  address=0x%X" % (exc["code"], exc["address"]))
    if exc["params"]:
        kind = {0: "read", 1: "write", 8: "execute"}.get(exc["params"][0], str(exc["params"][0]))
        target = exc["params"][1] if len(exc["params"]) > 1 else 0
        print("  访问类型=%s  目标地址=0x%X" % (kind, target))
    owner = module_of(modules, exc["address"])
    if owner:
        print("  归属=%s +0x%X" % (owner["name"], exc["address"] - owner["base"]))

    dll = next((m for m in modules if "pdd_send_v5" in m["name"].lower()), None)
    target_thread = next((t for t in threads if t["id"] == exc["thread"]), None)
    if dll is None or target_thread is None:
        print("dump 里缺 DLL 或线程信息")
        return
    start, size, rva = target_thread["stack"]
    print("\n== 崩溃线程栈 → 函数（按出现顺序去重） ==")
    seen = set()
    printed = 0
    for off in range(0, min(size, 256 * 1024) - 8, 8):
        value = int.from_bytes(data[rva + off: rva + off + 8], "little")
        if not (dll["base"] <= value < dll["base"] + dll["size"]):
            continue
        sym, delta = resolve(rows, value - dll["base"])
        key = (sym, delta)
        if key in seen:
            continue
        seen.add(key)
        print("  rva=0x%-6X %s +0x%X" % (value - dll["base"], sym or "?", delta))
        printed += 1
        if printed >= 30:
            break
    if not printed:
        print("  (栈里没有落在 DLL 内的返回地址)")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
