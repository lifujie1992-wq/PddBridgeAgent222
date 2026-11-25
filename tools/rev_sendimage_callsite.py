# -*- coding: utf-8 -*-
"""逆探域插件怎么调用 SendImageMsg（参数布局真相）。

思路：
1. 插件把 "?SendImageMsg@CMChatImpl@@..." 这个字符串交给 GetProcAddress；
2. 找到该字符串地址的引用（lea rXX, [rip+disp]）；
3. 在附近找 `call qword ptr [global]`；
4. 把调用点之前的指令流打出来，看 rcx/rdx/r8/r9 和栈参数分别来自哪里。
"""
from __future__ import annotations

import struct
import sys

import capstone
import pefile

DLL = r"D:\kefuAgent\探域\tanyu3.0.2\3.5.0.40_PddMsgPlugin_x64.dll"
STRINGS = ("SendImageMsg@CMChatImpl", "SendImage@CMChatImpl", "SendTextMsg@CMChatImpl")


def sections(pe):
    out = []
    for s in pe.sections:
        name = s.Name.rstrip(b"\0").decode("latin1")
        out.append({"name": name, "va": s.VirtualAddress, "vsize": s.Misc_VirtualSize,
                    "raw": s.PointerToRawData, "rawsize": s.SizeOfRawData})
    return out


def va_to_off(secs, va):
    for s in secs:
        if s["va"] <= va < s["va"] + max(s["vsize"], s["rawsize"]):
            return s["raw"] + (va - s["va"])
    return None


def main():
    pe = pefile.PE(DLL, fast_load=True)
    image_base = pe.OPTIONAL_HEADER.ImageBase
    secs = sections(pe)
    data = open(DLL, "rb").read()

    # 1) 找字符串地址
    targets = {}
    for name in STRINGS:
        needle = ("?" + name).encode()
        idx = data.find(needle)
        if idx < 0:
            print("找不到字符串:", name)
            continue
        # 由文件偏移反推 RVA
        va = None
        for s in secs:
            if s["raw"] <= idx < s["raw"] + s["rawsize"]:
                va = s["va"] + (idx - s["raw"])
                break
        targets[name] = va
        print("%-24s 字符串 VA=0x%X" % (name, va or 0))

    # 2) 扫描 .text 里对字符串地址的 RIP 相对 lea
    text = next(s for s in secs if s["name"] == ".text")
    text_bytes = data[text["raw"]:text["raw"] + text["rawsize"]]
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = True

    refs = {}
    for insn in md.disasm(text_bytes, image_base + text["va"]):
        if insn.mnemonic != "lea" or len(insn.operands) != 2:
            continue
        op = insn.operands[1]
        if op.type != capstone.x86.X86_OP_MEM or op.mem.base != capstone.x86.X86_REG_RIP:
            continue
        target = insn.address + insn.size + op.mem.disp
        for name, va in targets.items():
            if va and target == image_base + va:
                refs.setdefault(name, []).append(insn.address)
    for name, addrs in refs.items():
        print("%-24s lea 引用 %d 处: %s" % (name, len(addrs),
                                            ", ".join(hex(a) for a in addrs[:6])))

    # 3) 在这些引用附近找 call qword ptr [rXX+disp] / call reg
    for name, addrs in refs.items():
        for addr in addrs[:4]:
            start = addr - 0x180
            off = va_to_off(secs, start - image_base)
            if off is None:
                continue
            chunk = data[off:off + 0x200]
            print("\n===== %s @0x%X 附近 =====" % (name, addr))
            for insn in md.disasm(chunk, start):
                if "call" in insn.mnemonic:
                    print("  >>> %-10s %s" % (insn.address and hex(insn.address), insn.op_str))
                    # 打印调用点前 12 条
    # 4) 单独把含 call 的上下文打出来（更直观）
    print("\n\n########## 调用点上下文 ##########")
    for name, addrs in refs.items():
        for addr in addrs[:2]:
            start = addr
            off = va_to_off(secs, start - image_base)
            if off is None:
                continue
            chunk = data[off:off + 0x300]
            hist = []
            for insn in md.disasm(chunk, start):
                hist.append("%s %s %s" % (hex(insn.address), insn.mnemonic, insn.op_str))
                if insn.mnemonic == "call" and ("qword ptr" in insn.op_str or "rip" in insn.op_str):
                    print("\n--- %s ---" % name)
                    for line in hist[-16:]:
                        print("   ", line)
                    break


if __name__ == "__main__":
    main()
