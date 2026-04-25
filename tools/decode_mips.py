import struct, sys

ELF = 'SLUS_202.68'
VA_BASE = 0x100000
FILE_OFFSET_ADJUST = 0x80

def va_to_offset(va):
    return va - VA_BASE + FILE_OFFSET_ADJUST

def read_words(va, count):
    off = va_to_offset(va)
    with open(ELF, 'rb') as f:
        f.seek(off)
        data = f.read(count * 4)
    return [struct.unpack_from('<I', data, i*4)[0] for i in range(count)]

REGS = ['zero','at','v0','v1','a0','a1','a2','a3',
        's0','s1','s2','s3','s4','s5','s6','s7',
        't0','t1','t2','t3','t4','t5','t6','t7',
        't8','t9','k0','k1','gp','sp','fp','ra']

def disasm_one(va, word):
    op = (word >> 26) & 0x3F
    rs = (word >> 21) & 0x1F
    rt = (word >> 16) & 0x1F
    rd = (word >> 11) & 0x1F
    shamt = (word >> 6) & 0x1F
    funct = word & 0x3F
    imm16s = struct.unpack('<h', struct.pack('<H', word & 0xFFFF))[0]
    imm16u = word & 0xFFFF
    target26 = word & 0x3FFFFFF
    target_va = ((va + 4) & 0xF0000000) | (target26 << 2)

    if word == 0:
        return 'nop'
    if op == 0:  # SPECIAL
        map0 = {
            0x00: f'sll ${REGS[rd]}, ${REGS[rt]}, {shamt}',
            0x02: f'srl ${REGS[rd]}, ${REGS[rt]}, {shamt}',
            0x03: f'sra ${REGS[rd]}, ${REGS[rt]}, {shamt}',
            0x04: f'sllv ${REGS[rd]}, ${REGS[rt]}, ${REGS[rs]}',
            0x06: f'srlv ${REGS[rd]}, ${REGS[rt]}, ${REGS[rs]}',
            0x07: f'srav ${REGS[rd]}, ${REGS[rt]}, ${REGS[rs]}',
            0x08: f'jr ${REGS[rs]}',
            0x09: f'jalr ${REGS[rd]}, ${REGS[rs]}',
            0x0A: f'movz ${REGS[rd]}, ${REGS[rs]}, ${REGS[rt]}',
            0x0B: f'movn ${REGS[rd]}, ${REGS[rs]}, ${REGS[rt]}',
            0x0C: f'syscall',
            0x0F: f'sync',
            0x10: f'mfhi ${REGS[rd]}',
            0x11: f'mthi ${REGS[rs]}',
            0x12: f'mflo ${REGS[rd]}',
            0x13: f'mtlo ${REGS[rs]}',
            0x14: f'dsllv ${REGS[rd]}, ${REGS[rt]}, ${REGS[rs]}',
            0x16: f'dsrlv ${REGS[rd]}, ${REGS[rt]}, ${REGS[rs]}',
            0x17: f'dsrav ${REGS[rd]}, ${REGS[rt]}, ${REGS[rs]}',
            0x18: f'mult ${REGS[rs]}, ${REGS[rt]}',
            0x19: f'multu ${REGS[rs]}, ${REGS[rt]}',
            0x1A: f'div ${REGS[rs]}, ${REGS[rt]}',
            0x1B: f'divu ${REGS[rs]}, ${REGS[rt]}',
            0x1C: f'dmult ${REGS[rs]}, ${REGS[rt]}',
            0x1D: f'dmultu ${REGS[rs]}, ${REGS[rt]}',
            0x1E: f'ddiv ${REGS[rs]}, ${REGS[rt]}',
            0x1F: f'ddivu ${REGS[rs]}, ${REGS[rt]}',
            0x20: f'add ${REGS[rd]}, ${REGS[rs]}, ${REGS[rt]}',
            0x21: f'addu ${REGS[rd]}, ${REGS[rs]}, ${REGS[rt]}',
            0x22: f'sub ${REGS[rd]}, ${REGS[rs]}, ${REGS[rt]}',
            0x23: f'subu ${REGS[rd]}, ${REGS[rs]}, ${REGS[rt]}',
            0x24: f'and ${REGS[rd]}, ${REGS[rs]}, ${REGS[rt]}',
            0x25: f'or ${REGS[rd]}, ${REGS[rs]}, ${REGS[rt]}',
            0x26: f'xor ${REGS[rd]}, ${REGS[rs]}, ${REGS[rt]}',
            0x27: f'nor ${REGS[rd]}, ${REGS[rs]}, ${REGS[rt]}',
            0x28: f'mfsa ${REGS[rd]}',
            0x29: f'mtsa ${REGS[rs]}',
            0x2A: f'slt ${REGS[rd]}, ${REGS[rs]}, ${REGS[rt]}',
            0x2B: f'sltu ${REGS[rd]}, ${REGS[rs]}, ${REGS[rt]}',
            0x2C: f'dadd ${REGS[rd]}, ${REGS[rs]}, ${REGS[rt]}',
            0x2D: f'daddu ${REGS[rd]}, ${REGS[rs]}, ${REGS[rt]}',
            0x2E: f'dsub ${REGS[rd]}, ${REGS[rs]}, ${REGS[rt]}',
            0x2F: f'dsubu ${REGS[rd]}, ${REGS[rs]}, ${REGS[rt]}',
            0x38: f'dsll ${REGS[rd]}, ${REGS[rt]}, {shamt}',
            0x3A: f'dsrl ${REGS[rd]}, ${REGS[rt]}, {shamt}',
            0x3B: f'dsra ${REGS[rd]}, ${REGS[rt]}, {shamt}',
            0x3C: f'dsll32 ${REGS[rd]}, ${REGS[rt]}, {shamt}',
            0x3E: f'dsrl32 ${REGS[rd]}, ${REGS[rt]}, {shamt}',
            0x3F: f'dsra32 ${REGS[rd]}, ${REGS[rt]}, {shamt}',
        }
        return map0.get(funct, f'special_{funct:02x} r{rs},r{rt},r{rd}')
    elif op == 1:  # REGIMM
        ri = {0: 'bltz', 1: 'bgez', 2: 'bltzl', 3: 'bgezl', 16: 'bltzal', 17: 'bgezal'}
        mnem = ri.get(rt, f'regimm_{rt}')
        return f'{mnem} ${REGS[rs]}, 0x{va+4+imm16s*4:08X}'
    elif op == 2:
        return f'j 0x{target_va:08X}'
    elif op == 3:
        return f'jal 0x{target_va:08X}'
    elif op == 4:
        return f'beq ${REGS[rs]}, ${REGS[rt]}, 0x{va+4+imm16s*4:08X}'
    elif op == 5:
        return f'bne ${REGS[rs]}, ${REGS[rt]}, 0x{va+4+imm16s*4:08X}'
    elif op == 6:
        return f'blez ${REGS[rs]}, 0x{va+4+imm16s*4:08X}'
    elif op == 7:
        return f'bgtz ${REGS[rs]}, 0x{va+4+imm16s*4:08X}'
    elif op == 8:
        return f'addi ${REGS[rt]}, ${REGS[rs]}, {imm16s}'
    elif op == 9:
        return f'addiu ${REGS[rt]}, ${REGS[rs]}, {imm16s}'
    elif op == 10:
        return f'slti ${REGS[rt]}, ${REGS[rs]}, {imm16s}'
    elif op == 11:
        return f'sltiu ${REGS[rt]}, ${REGS[rs]}, {imm16u}'
    elif op == 12:
        return f'andi ${REGS[rt]}, ${REGS[rs]}, 0x{imm16u:04X}'
    elif op == 13:
        return f'ori ${REGS[rt]}, ${REGS[rs]}, 0x{imm16u:04X}'
    elif op == 14:
        return f'xori ${REGS[rt]}, ${REGS[rs]}, 0x{imm16u:04X}'
    elif op == 15:
        return f'lui ${REGS[rt]}, 0x{imm16u:04X}'
    elif op == 16:  # COP0
        if rs == 0:
            return f'mfc0 ${REGS[rt]}, ${rd}'
        if rs == 4:
            return f'mtc0 ${REGS[rt]}, ${rd}'
        if rs == 16 and funct == 2:
            return 'tlbwi'
        if rs == 16 and funct == 6:
            return 'tlbwr'
        if rs == 16 and funct == 24:
            return 'eret'
        if rs == 16 and funct == 8:
            return 'tlbp'
        return f'cop0_rs{rs}_f{funct}'
    elif op == 18:  # COP2 / VU0 macro
        return f'cop2_0x{word:08X}'
    elif op == 20:
        return f'beql ${REGS[rs]}, ${REGS[rt]}, 0x{va+4+imm16s*4:08X}'
    elif op == 21:
        return f'bnel ${REGS[rs]}, ${REGS[rt]}, 0x{va+4+imm16s*4:08X}'
    elif op == 22:
        return f'blezl ${REGS[rs]}, 0x{va+4+imm16s*4:08X}'
    elif op == 23:
        return f'bgtzl ${REGS[rs]}, 0x{va+4+imm16s*4:08X}'
    elif op == 24:
        return f'daddi ${REGS[rt]}, ${REGS[rs]}, {imm16s}'
    elif op == 25:
        return f'daddiu ${REGS[rt]}, ${REGS[rs]}, {imm16s}'
    elif op == 26:
        return f'ldl ${REGS[rt]}, {imm16s}(${REGS[rs]})'
    elif op == 27:
        return f'ldr ${REGS[rt]}, {imm16s}(${REGS[rs]})'
    elif op == 32:
        return f'lb ${REGS[rt]}, {imm16s}(${REGS[rs]})'
    elif op == 33:
        return f'lh ${REGS[rt]}, {imm16s}(${REGS[rs]})'
    elif op == 34:
        return f'lwl ${REGS[rt]}, {imm16s}(${REGS[rs]})'
    elif op == 35:
        return f'lw ${REGS[rt]}, {imm16s}(${REGS[rs]})'
    elif op == 36:
        return f'lbu ${REGS[rt]}, {imm16s}(${REGS[rs]})'
    elif op == 37:
        return f'lhu ${REGS[rt]}, {imm16s}(${REGS[rs]})'
    elif op == 38:
        return f'lwr ${REGS[rt]}, {imm16s}(${REGS[rs]})'
    elif op == 39:
        return f'lwu ${REGS[rt]}, {imm16s}(${REGS[rs]})'
    elif op == 40:
        return f'sb ${REGS[rt]}, {imm16s}(${REGS[rs]})'
    elif op == 41:
        return f'sh ${REGS[rt]}, {imm16s}(${REGS[rs]})'
    elif op == 42:
        return f'swl ${REGS[rt]}, {imm16s}(${REGS[rs]})'
    elif op == 43:
        return f'sw ${REGS[rt]}, {imm16s}(${REGS[rs]})'
    elif op == 46:
        return f'swr ${REGS[rt]}, {imm16s}(${REGS[rs]})'
    elif op == 47:
        return f'cache {rt}, {imm16s}(${REGS[rs]})'
    elif op == 49:
        return f'lwc1 $f{rt}, {imm16s}(${REGS[rs]})'
    elif op == 55:
        return f'ld ${REGS[rt]}, {imm16s}(${REGS[rs]})'
    elif op == 57:
        return f'swc1 $f{rt}, {imm16s}(${REGS[rs]})'
    elif op == 63:
        return f'sd ${REGS[rt]}, {imm16s}(${REGS[rs]})'
    return f'op{op:02x}_{word:08X}'


def disasm_range(start_va, count, label=''):
    if label:
        print(f'\n=== {label} (VA 0x{start_va:08X}) ===')
    words = read_words(start_va, count)
    for i, w in enumerate(words):
        va = start_va + i*4
        print(f'  {va:08X}: {w:08X}  {disasm_one(va, w)}')


if __name__ == '__main__':
    target = sys.argv[1] if len(sys.argv) > 1 else 'all'
    count = int(sys.argv[2], 0) if len(sys.argv) > 2 else 120

    ranges = {
        '258e70':  (0x258E70, 200, 'func_258E70 (VIF1 packet builder)'),
        'ov74':    (0x3849A0, 494, 'overlay 0x80074000 (src 0x3849A0, 0x7A8 bytes)'),
        'ov74_488':(0x3849A0 + (0x80074498 - 0x80074000), 60, 'overlay sub 0x80074498 (FFFFFC402 handler)'),
        'ov74_138':(0x3849A0 + (0x80074138 - 0x80074000), 40, 'overlay sub 0x80074138 (a0=0x4A)'),
        'ov74_088':(0x3849A0 + (0x80074088 - 0x80074000), 40, 'overlay sub 0x80074088 (a0=0x4B)'),
        'ov75':    (0x384348, 202, 'overlay 0x80075000 (src 0x384348, 0x328 bytes)'),
        'ov75_038':(0x384348 + (0x80075038 - 0x80075000), 40, 'overlay sub 0x80075038 (a0=0x55)'),
        'ov76':    (0x383AD8, 464, 'overlay 0x80076000 (src 0x383AD8, 0x740 bytes)'),
        'ov76_488':(0x383AD8 + (0x80076488 - 0x80076000), 60, 'overlay sub 0x80076488 (a0=0x12C)'),
        'ov76_6c0':(0x383AD8 + (0x800766C0 - 0x80076000), 60, 'overlay sub 0x800766C0 (a0=0x08, exception return)'),
        'ov76_440':(0x383AD8 + (0x80076440 - 0x80076000), 30, 'overlay sub 0x80076440 (a0=0xFC/0xFE)'),
        'ov76_2a0':(0x383AD8 + (0x800762A0 - 0x80076000), 30, 'overlay sub 0x800762A0 (a0=0xFD/0xFF)'),
    }

    if target == 'all':
        for key, (va, cnt, lbl) in ranges.items():
            disasm_range(va, min(cnt, count), lbl)
    elif target in ranges:
        va, cnt, lbl = ranges[target]
        disasm_range(va, cnt, lbl)
    else:
        va = int(target, 0)
        disasm_range(va, count, f'VA 0x{va:08X}')
