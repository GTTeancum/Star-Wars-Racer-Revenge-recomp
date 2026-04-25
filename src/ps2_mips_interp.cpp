// ps2_mips_interp.cpp — Minimal MIPS R5900 interpreter for kseg0 overlay code.
// See ps2_mips_interp.h for overview.

#include "ps2_mips_interp.h"
#include "ps2_runtime.h"
#include "ps2_runtime_macros.h"

#include <cstring>
#include <iostream>
#include <cassert>

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

static constexpr uint32_t PHYS_MASK    = 0x1FFFFFFFu; // strip kseg0/1 bits
static constexpr uint32_t RDRAM_SIZE   = 0x2000000u;  // 32 MB
static constexpr uint32_t MAX_INSNS    = 500000u;      // safety fuse per call
static constexpr int      MAX_DEPTH    = 64;           // max nested jal depth

static inline uint32_t physAddr(uint32_t va) { return va & PHYS_MASK; }

static inline uint32_t rdramRead32(const uint8_t* rdram, uint32_t va) {
    uint32_t phys = physAddr(va);
    if (phys + 4 > RDRAM_SIZE) return 0u;  // hardware register / out-of-range — return 0
    uint32_t w;
    memcpy(&w, rdram + phys, 4);
    return w;
}
static inline uint64_t rdramRead64(const uint8_t* rdram, uint32_t va) {
    uint32_t phys = physAddr(va);
    if (phys + 8 > RDRAM_SIZE) return 0ULL;
    uint64_t w;
    memcpy(&w, rdram + phys, 8);
    return w;
}
static inline uint16_t rdramRead16(const uint8_t* rdram, uint32_t va) {
    uint32_t phys = physAddr(va);
    if (phys + 2 > RDRAM_SIZE) return 0u;
    uint16_t w;
    memcpy(&w, rdram + phys, 2);
    return w;
}
static inline uint8_t rdramRead8(const uint8_t* rdram, uint32_t va) {
    uint32_t phys = physAddr(va);
    if (phys >= RDRAM_SIZE) return 0u;
    return rdram[phys];
}

static inline void rdramWrite32(uint8_t* rdram, uint32_t va, uint32_t v) {
    uint32_t phys = physAddr(va);
    if (phys + 4 > RDRAM_SIZE) return;  // hardware register / out-of-range — ignore
    memcpy(rdram + phys, &v, 4);
}
static inline void rdramWrite64(uint8_t* rdram, uint32_t va, uint64_t v) {
    uint32_t phys = physAddr(va);
    if (phys + 8 > RDRAM_SIZE) return;
    memcpy(rdram + phys, &v, 8);
}
static inline void rdramWrite16(uint8_t* rdram, uint32_t va, uint16_t v) {
    uint32_t phys = physAddr(va);
    if (phys + 2 > RDRAM_SIZE) return;
    memcpy(rdram + phys, &v, 2);
}
static inline void rdramWrite8(uint8_t* rdram, uint32_t va, uint8_t v) {
    uint32_t phys = physAddr(va);
    if (phys >= RDRAM_SIZE) return;
    rdram[phys] = v;
}

// GPR helpers — thin wrappers around the ps2_runtime macros.
// R5900 GPRs are 128-bit but integer code uses only the low 64.
// 32-bit operations store sign-extended 64-bit values.
static inline uint64_t gpr(R5900Context* ctx, uint32_t reg) {
    return (reg == 0) ? 0ULL : GPR_U64(ctx, reg);
}
static inline uint32_t gpr32(R5900Context* ctx, uint32_t reg) {
    return (reg == 0) ? 0u : GPR_U32(ctx, reg);
}
static inline void setGpr32(R5900Context* ctx, uint32_t reg, int32_t val) {
    if (reg != 0) SET_GPR_S32(ctx, reg, val);   // sign-extends to 64
}
static inline void setGpr64(R5900Context* ctx, uint32_t reg, uint64_t val) {
    if (reg != 0) SET_GPR_U64(ctx, reg, val);
}

// ---------------------------------------------------------------------------
// Forward declaration
// ---------------------------------------------------------------------------
static void execFunction(uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime,
                         uint32_t startVa, uint64_t& hi, uint64_t& lo, int depth);

// ---------------------------------------------------------------------------
// Execute a single instruction. Returns next PC.
// Sets *branchTarget if a branch/jump was resolved (to handle delay slots).
// ---------------------------------------------------------------------------
static void execOne(uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime,
                    uint32_t& pc, uint64_t& hi, uint64_t& lo,
                    uint32_t instr, int depth)
{
    const uint32_t op    = (instr >> 26) & 0x3F;
    const uint32_t rs    = (instr >> 21) & 0x1F;
    const uint32_t rt    = (instr >> 16) & 0x1F;
    const uint32_t rd    = (instr >> 11) & 0x1F;
    const uint32_t shamt = (instr >>  6) & 0x1F;
    const uint32_t funct =  instr        & 0x3F;
    const int32_t  simm  = (int16_t)(instr & 0xFFFF);   // sign-extended imm16
    const uint32_t uimm  = instr & 0xFFFF;               // zero-extended imm16
    const uint32_t imm26 = instr & 0x3FFFFFFu;

    // NOP fast-path
    if (instr == 0) { pc += 4; return; }

    // -----------------------------------------------------------------------
    // Decode
    // -----------------------------------------------------------------------
    switch (op) {

    // SPECIAL
    case 0x00:
        switch (funct) {
        case 0x00: // SLL rd, rt, shamt
            setGpr32(ctx, rd, (int32_t)((uint32_t)gpr32(ctx,rt) << shamt));
            break;
        case 0x02: // SRL rd, rt, shamt
            setGpr32(ctx, rd, (int32_t)((uint32_t)gpr32(ctx,rt) >> shamt));
            break;
        case 0x03: // SRA rd, rt, shamt
            setGpr32(ctx, rd, (int32_t)((int32_t)gpr32(ctx,rt) >> shamt));
            break;
        case 0x04: // SLLV rd, rt, rs
            setGpr32(ctx, rd, (int32_t)((uint32_t)gpr32(ctx,rt) << (gpr32(ctx,rs) & 31)));
            break;
        case 0x06: // SRLV rd, rt, rs
            setGpr32(ctx, rd, (int32_t)((uint32_t)gpr32(ctx,rt) >> (gpr32(ctx,rs) & 31)));
            break;
        case 0x07: // SRAV rd, rt, rs
            setGpr32(ctx, rd, (int32_t)((int32_t)gpr32(ctx,rt) >> (gpr32(ctx,rs) & 31)));
            break;

        case 0x08: { // JR rs — tail-jump / return
            const uint32_t target = gpr32(ctx, rs);
            // Execute delay slot
            const uint32_t dsInstr = rdramRead32(rdram, pc + 4);
            pc += 4;
            execOne(rdram, ctx, runtime, pc, hi, lo, dsInstr, depth);
            pc = target;
            // Signal caller this is a jr (handled by caller loop)
            return;
        }
        case 0x09: { // JALR rd, rs — call via register
            const uint32_t target = gpr32(ctx, rs);
            const uint32_t retAddr = pc + 8; // pc is still at jalr, ds is at pc+4
            setGpr32(ctx, rd, (int32_t)retAddr);
            // Execute delay slot
            const uint32_t dsInstr = rdramRead32(rdram, pc + 4);
            pc += 4;
            execOne(rdram, ctx, runtime, pc, hi, lo, dsInstr, depth);
            // Call the target
            if (target == 0u || target == retAddr) {
                // no-op / tail
            } else if (target < 0x80000000u && runtime->hasFunction(target)) {
                // Dispatch to recompiled function
                ctx->pc = target;
                SET_GPR_U32(ctx, 31, retAddr);
                auto fn = runtime->lookupFunction(target);
                fn(rdram, ctx, runtime);
                ctx->pc = retAddr;
            } else {
                // Interpret recursively
                execFunction(rdram, ctx, runtime, target, hi, lo, depth + 1);
            }
            pc = retAddr;
            return;
        }

        case 0x0C: // SYSCALL
            runtime->handleSyscall(rdram, ctx, 0u);
            break;
        case 0x0D: // BREAK
            break; // ignore

        case 0x10: // MFHI rd
            setGpr64(ctx, rd, hi);
            break;
        case 0x11: // MTHI rs
            hi = gpr(ctx, rs);
            break;
        case 0x12: // MFLO rd
            setGpr64(ctx, rd, lo);
            break;
        case 0x13: // MTLO rs
            lo = gpr(ctx, rs);
            break;

        case 0x14: { // DSLLV rd, rt, rs
            uint32_t sa = gpr32(ctx,rs) & 63;
            setGpr64(ctx, rd, gpr(ctx,rt) << sa);
            break;
        }
        case 0x16: { // DSRLV rd, rt, rs
            uint32_t sa = gpr32(ctx,rs) & 63;
            setGpr64(ctx, rd, gpr(ctx,rt) >> sa);
            break;
        }
        case 0x17: { // DSRAV rd, rt, rs
            uint32_t sa = gpr32(ctx,rs) & 63;
            setGpr64(ctx, rd, (uint64_t)((int64_t)gpr(ctx,rt) >> sa));
            break;
        }

        case 0x18: { // MULT rs, rt  (32×32→64 signed)
            int64_t r = (int64_t)(int32_t)gpr32(ctx,rs) * (int32_t)gpr32(ctx,rt);
            lo = (uint64_t)(int64_t)(int32_t)(uint32_t)(r & 0xFFFFFFFFu);
            hi = (uint64_t)(int64_t)(int32_t)(uint32_t)((uint64_t)r >> 32);
            break;
        }
        case 0x19: { // MULTU rs, rt  (32×32→64 unsigned)
            uint64_t r = (uint64_t)gpr32(ctx,rs) * (uint64_t)gpr32(ctx,rt);
            lo = (uint64_t)(int64_t)(int32_t)(uint32_t)(r & 0xFFFFFFFFu);
            hi = (uint64_t)(int64_t)(int32_t)(uint32_t)(r >> 32);
            break;
        }
        case 0x1A: { // DIV rs, rt
            int32_t a = (int32_t)gpr32(ctx,rs), b = (int32_t)gpr32(ctx,rt);
            if (b != 0) {
                lo = (uint64_t)(int64_t)(a / b);
                hi = (uint64_t)(int64_t)(a % b);
            }
            break;
        }
        case 0x1B: { // DIVU rs, rt
            uint32_t a = gpr32(ctx,rs), b = gpr32(ctx,rt);
            if (b != 0) {
                lo = (uint64_t)(int64_t)(int32_t)(a / b);
                hi = (uint64_t)(int64_t)(int32_t)(a % b);
            }
            break;
        }
        case 0x1C: { // DMULT (64-bit signed multiply)
            int64_t a = (int64_t)gpr(ctx,rs), b = (int64_t)gpr(ctx,rt);
            // Full 128-bit result; we only keep lo/hi 64 bits
            // Use __int128 if available
#ifdef __GNUC__
            __int128 r = (__int128)a * b;
            lo = (uint64_t)r;
            hi = (uint64_t)((unsigned __int128)r >> 64);
#else
            lo = (uint64_t)(a * b); hi = 0;
#endif
            break;
        }
        case 0x1D: { // DMULTU
            uint64_t a = gpr(ctx,rs), b = gpr(ctx,rt);
#ifdef __GNUC__
            unsigned __int128 r = (unsigned __int128)a * b;
            lo = (uint64_t)r; hi = (uint64_t)(r >> 64);
#else
            lo = a * b; hi = 0;
#endif
            break;
        }
        case 0x1E: { // DDIV
            int64_t a=(int64_t)gpr(ctx,rs), b=(int64_t)gpr(ctx,rt);
            if(b) { lo=(uint64_t)(a/b); hi=(uint64_t)(a%b); }
            break;
        }
        case 0x1F: { // DDIVU
            uint64_t a=gpr(ctx,rs), b=gpr(ctx,rt);
            if(b) { lo=a/b; hi=a%b; }
            break;
        }

        case 0x20: // ADD rd, rs, rt  (trap on overflow — we ignore trap)
        case 0x21: // ADDU rd, rs, rt
            setGpr32(ctx, rd, (int32_t)(gpr32(ctx,rs) + gpr32(ctx,rt)));
            break;
        case 0x22: // SUB rd, rs, rt
        case 0x23: // SUBU rd, rs, rt
            setGpr32(ctx, rd, (int32_t)(gpr32(ctx,rs) - gpr32(ctx,rt)));
            break;
        case 0x24: // AND rd, rs, rt
            setGpr64(ctx, rd, gpr(ctx,rs) & gpr(ctx,rt));
            break;
        case 0x25: // OR rd, rs, rt
            setGpr64(ctx, rd, gpr(ctx,rs) | gpr(ctx,rt));
            break;
        case 0x26: // XOR rd, rs, rt
            setGpr64(ctx, rd, gpr(ctx,rs) ^ gpr(ctx,rt));
            break;
        case 0x27: // NOR rd, rs, rt
            setGpr64(ctx, rd, ~(gpr(ctx,rs) | gpr(ctx,rt)));
            break;

        case 0x2A: // SLT rd, rs, rt
            setGpr32(ctx, rd, (int32_t)(((int64_t)gpr(ctx,rs) < (int64_t)gpr(ctx,rt)) ? 1 : 0));
            break;
        case 0x2B: // SLTU rd, rs, rt
            setGpr32(ctx, rd, (int32_t)((gpr(ctx,rs) < gpr(ctx,rt)) ? 1 : 0));
            break;

        case 0x2C: // DADD rd, rs, rt (trap on overflow — ignore)
        case 0x2D: // DADDU rd, rs, rt
            setGpr64(ctx, rd, gpr(ctx,rs) + gpr(ctx,rt));
            break;
        case 0x2E: // DSUB
        case 0x2F: // DSUBU
            setGpr64(ctx, rd, gpr(ctx,rs) - gpr(ctx,rt));
            break;

        case 0x38: // DSLL rd, rt, shamt
            setGpr64(ctx, rd, gpr(ctx,rt) << shamt);
            break;
        case 0x3A: // DSRL rd, rt, shamt
            setGpr64(ctx, rd, gpr(ctx,rt) >> shamt);
            break;
        case 0x3B: // DSRA rd, rt, shamt
            setGpr64(ctx, rd, (uint64_t)((int64_t)gpr(ctx,rt) >> shamt));
            break;
        case 0x3C: // DSLL32 rd, rt, shamt  (shift by shamt+32)
            setGpr64(ctx, rd, gpr(ctx,rt) << (shamt + 32));
            break;
        case 0x3E: // DSRL32
            setGpr64(ctx, rd, gpr(ctx,rt) >> (shamt + 32));
            break;
        case 0x3F: // DSRA32
            setGpr64(ctx, rd, (uint64_t)((int64_t)gpr(ctx,rt) >> (shamt + 32)));
            break;

        default:
            // Unknown SPECIAL funct — skip
            break;
        }
        break; // end SPECIAL

    // REGIMM (op=0x01) — branches by rt field
    case 0x01: {
        bool taken = false;
        switch (rt) {
        case 0x00: taken = ((int64_t)gpr(ctx,rs) <  0); break; // BLTZ
        case 0x01: taken = ((int64_t)gpr(ctx,rs) >= 0); break; // BGEZ
        case 0x10: taken = ((int64_t)gpr(ctx,rs) <  0); break; // BLTZAL
        case 0x11: taken = ((int64_t)gpr(ctx,rs) >= 0); break; // BGEZAL
        }
        const bool isLink = (rt == 0x10 || rt == 0x11);
        if (isLink) setGpr32(ctx, 31, (int32_t)(pc + 8));

        const uint32_t dsInstr = rdramRead32(rdram, pc + 4);
        pc += 4;
        execOne(rdram, ctx, runtime, pc, hi, lo, dsInstr, depth); // delay slot
        if (taken) pc = (pc + 1) + (simm << 2); // +1 because pc was already at ds
        else       pc += 4 - 4; // already advanced by ds exec; stay
        return;
    }

    case 0x02: { // J target
        uint32_t target = (pc & 0xF0000000u) | (imm26 << 2);
        const uint32_t dsInstr = rdramRead32(rdram, pc + 4);
        pc += 4;
        execOne(rdram, ctx, runtime, pc, hi, lo, dsInstr, depth);
        pc = target;
        return;
    }

    case 0x03: { // JAL target
        uint32_t target  = (pc & 0xF0000000u) | (imm26 << 2);
        uint32_t retAddr = pc + 8;
        setGpr32(ctx, 31, (int32_t)retAddr);
        // Execute delay slot
        const uint32_t dsInstr = rdramRead32(rdram, pc + 4);
        pc += 4;
        execOne(rdram, ctx, runtime, pc, hi, lo, dsInstr, depth);
        // Call target
        if (target < 0x80000000u && runtime->hasFunction(target)) {
            ctx->pc = target;
            SET_GPR_U32(ctx, 31, retAddr);
            auto fn = runtime->lookupFunction(target);
            fn(rdram, ctx, runtime);
            ctx->pc = retAddr;
        } else {
            execFunction(rdram, ctx, runtime, target, hi, lo, depth + 1);
        }
        pc = retAddr;
        return;
    }

    case 0x04: { // BEQ rs, rt, offset
        bool taken = (gpr(ctx,rs) == gpr(ctx,rt));
        const uint32_t dsInstr = rdramRead32(rdram, pc + 4);
        const int32_t  off4    = simm << 2;
        pc += 4;
        execOne(rdram, ctx, runtime, pc, hi, lo, dsInstr, depth);
        if (taken) { pc += off4 - 4; } // -4 because we already moved past ds
        return;
    }
    case 0x05: { // BNE rs, rt, offset
        bool taken = (gpr(ctx,rs) != gpr(ctx,rt));
        const uint32_t dsInstr = rdramRead32(rdram, pc + 4);
        const int32_t  off4    = simm << 2;
        pc += 4;
        execOne(rdram, ctx, runtime, pc, hi, lo, dsInstr, depth);
        if (taken) { pc += off4 - 4; }
        return;
    }
    case 0x06: { // BLEZ rs, offset
        bool taken = ((int64_t)gpr(ctx,rs) <= 0);
        const uint32_t dsInstr = rdramRead32(rdram, pc + 4);
        const int32_t  off4    = simm << 2;
        pc += 4;
        execOne(rdram, ctx, runtime, pc, hi, lo, dsInstr, depth);
        if (taken) { pc += off4 - 4; }
        return;
    }
    case 0x07: { // BGTZ rs, offset
        bool taken = ((int64_t)gpr(ctx,rs) > 0);
        const uint32_t dsInstr = rdramRead32(rdram, pc + 4);
        const int32_t  off4    = simm << 2;
        pc += 4;
        execOne(rdram, ctx, runtime, pc, hi, lo, dsInstr, depth);
        if (taken) { pc += off4 - 4; }
        return;
    }

    case 0x08: // ADDI rt, rs, imm (trap on overflow — ignore)
    case 0x09: // ADDIU rt, rs, imm
        setGpr32(ctx, rt, (int32_t)((uint32_t)gpr32(ctx,rs) + (uint32_t)(int32_t)simm));
        break;
    case 0x0A: // SLTI rt, rs, imm
        setGpr32(ctx, rt, ((int64_t)gpr(ctx,rs) < (int64_t)simm) ? 1 : 0);
        break;
    case 0x0B: // SLTIU rt, rs, imm
        setGpr32(ctx, rt, (gpr(ctx,rs) < (uint64_t)(int64_t)simm) ? 1 : 0);
        break;
    case 0x0C: // ANDI rt, rs, imm
        setGpr64(ctx, rt, gpr(ctx,rs) & (uint64_t)uimm);
        break;
    case 0x0D: // ORI rt, rs, imm
        setGpr64(ctx, rt, gpr(ctx,rs) | (uint64_t)uimm);
        break;
    case 0x0E: // XORI rt, rs, imm
        setGpr64(ctx, rt, gpr(ctx,rs) ^ (uint64_t)uimm);
        break;
    case 0x0F: // LUI rt, imm
        setGpr32(ctx, rt, (int32_t)((uint32_t)uimm << 16));
        break;

    case 0x10: // COP0 — NOP everything (no real COP0 state)
        // mtc0, mfc0, eret, tlbwi, etc. — silently skip.
        break;

    case 0x12: // COP2 (VU0 macro mode) — warn and skip
        {
            static int cop2Warn = 0;
            if (++cop2Warn <= 5)
                std::cerr << "[interp] COP2 instr 0x" << std::hex << instr
                          << " at pc=0x" << (pc - 4) << std::dec << " -- skipped" << std::endl;
        }
        break;

    // Branch-likely: delay slot ONLY executes if branch TAKEN.
    case 0x14: { // BEQL rs, rt, offset
        bool taken = (gpr(ctx,rs) == gpr(ctx,rt));
        const int32_t off4 = simm << 2;
        if (taken) {
            const uint32_t dsInstr = rdramRead32(rdram, pc + 4);
            pc += 4;
            execOne(rdram, ctx, runtime, pc, hi, lo, dsInstr, depth);
            pc += off4 - 4;
        } else {
            pc += 8; // skip instruction + delay slot
        }
        return;
    }
    case 0x15: { // BNEL rs, rt, offset
        bool taken = (gpr(ctx,rs) != gpr(ctx,rt));
        const int32_t off4 = simm << 2;
        if (taken) {
            const uint32_t dsInstr = rdramRead32(rdram, pc + 4);
            pc += 4;
            execOne(rdram, ctx, runtime, pc, hi, lo, dsInstr, depth);
            pc += off4 - 4;
        } else {
            pc += 8;
        }
        return;
    }
    case 0x16: { // BLEZL rs, offset
        bool taken = ((int64_t)gpr(ctx,rs) <= 0);
        const int32_t off4 = simm << 2;
        if (taken) {
            const uint32_t dsInstr = rdramRead32(rdram, pc + 4);
            pc += 4;
            execOne(rdram, ctx, runtime, pc, hi, lo, dsInstr, depth);
            pc += off4 - 4;
        } else { pc += 8; }
        return;
    }
    case 0x17: { // BGTZL rs, offset
        bool taken = ((int64_t)gpr(ctx,rs) > 0);
        const int32_t off4 = simm << 2;
        if (taken) {
            const uint32_t dsInstr = rdramRead32(rdram, pc + 4);
            pc += 4;
            execOne(rdram, ctx, runtime, pc, hi, lo, dsInstr, depth);
            pc += off4 - 4;
        } else { pc += 8; }
        return;
    }

    case 0x18: // DADDI rt, rs, imm (trap — ignore)
    case 0x19: // DADDIU rt, rs, imm
        setGpr64(ctx, rt, gpr(ctx,rs) + (uint64_t)(int64_t)simm);
        break;

    // MMI / R5900 multimedia (op=0x1C)
    case 0x1C: {
        // Very limited subset: MADD/MADDU/PMFHI/PMFLO/etc.
        // The overlay code primarily uses integer ops — if MMI appears,
        // it's unexpected. Log the first few and skip.
        static int mmiWarn = 0;
        if (++mmiWarn <= 5)
            std::cerr << "[interp] MMI instr 0x" << std::hex << instr
                      << " at pc=0x" << (pc - 4) << std::dec << " -- skipped" << std::endl;
        break;
    }

    // Memory loads
    case 0x20: // LB
        setGpr32(ctx, rt, (int32_t)(int8_t)rdramRead8(rdram, gpr32(ctx,rs) + simm));
        break;
    case 0x21: // LH
        setGpr32(ctx, rt, (int32_t)(int16_t)rdramRead16(rdram, gpr32(ctx,rs) + simm));
        break;
    case 0x22: // LWL (unaligned word left — simplified: treat as LW)
    case 0x23: // LW
        setGpr32(ctx, rt, (int32_t)rdramRead32(rdram, gpr32(ctx,rs) + simm));
        break;
    case 0x24: // LBU
        setGpr32(ctx, rt, (int32_t)(uint8_t)rdramRead8(rdram, gpr32(ctx,rs) + simm));
        break;
    case 0x25: // LHU
        setGpr32(ctx, rt, (int32_t)(uint16_t)rdramRead16(rdram, gpr32(ctx,rs) + simm));
        break;
    case 0x26: // LWR — simplified: just read 32-bit word
        setGpr32(ctx, rt, (int32_t)rdramRead32(rdram, (gpr32(ctx,rs) + simm) & ~3u));
        break;
    case 0x27: // LWU (unsigned 32-bit) - R5900 extension
        setGpr64(ctx, rt, (uint64_t)rdramRead32(rdram, gpr32(ctx,rs) + simm));
        break;

    // Memory stores
    case 0x28: // SB
        rdramWrite8(rdram, gpr32(ctx,rs) + simm, (uint8_t)gpr32(ctx,rt));
        break;
    case 0x29: // SH
        rdramWrite16(rdram, gpr32(ctx,rs) + simm, (uint16_t)gpr32(ctx,rt));
        break;
    case 0x2A: // SWL (simplified: treat as SW)
    case 0x2B: // SW
        rdramWrite32(rdram, gpr32(ctx,rs) + simm, gpr32(ctx,rt));
        break;
    case 0x2E: // SWR — simplified: just write 32-bit word
        rdramWrite32(rdram, (gpr32(ctx,rs) + simm) & ~3u, gpr32(ctx,rt));
        break;

    case 0x37: // LD rt, imm(rs)
        setGpr64(ctx, rt, rdramRead64(rdram, gpr32(ctx,rs) + simm));
        break;
    case 0x3F: // SD rt, imm(rs)
        rdramWrite64(rdram, gpr32(ctx,rs) + simm, gpr(ctx,rt));
        break;

    // FPU / LWC1 / SWC1 — skip (overlay code doesn't use FP)
    case 0x31: // LWC1
    case 0x35: // LDC1
    case 0x39: // SWC1
    case 0x3D: // SDC1
        break;

    // SQ / LQ (R5900 128-bit) — handle by treating as double read/write
    case 0x1E: { // LQ (opcode 0x1E on R5900)
        uint32_t addr = (gpr32(ctx,rs) + simm) & ~15u;
        if (physAddr(addr) + 16 <= RDRAM_SIZE) {
            uint64_t lo64, hi64;
            memcpy(&lo64, rdram + physAddr(addr),     8);
            memcpy(&hi64, rdram + physAddr(addr) + 8, 8);
            setGpr64(ctx, rt, lo64); // store low 64 in register low
        }
        break;
    }
    case 0x1F: { // SQ (opcode 0x1F on R5900)
        uint32_t addr = (gpr32(ctx,rs) + simm) & ~15u;
        if (physAddr(addr) + 16 <= RDRAM_SIZE) {
            uint64_t val64 = gpr(ctx, rt);
            memcpy(rdram + physAddr(addr), &val64, 8);
            // high 64 bits of the 128-bit register — we don't track them,
            // write zeros.
            uint64_t zero = 0;
            memcpy(rdram + physAddr(addr) + 8, &zero, 8);
        }
        break;
    }

    default:
        // Unknown opcode — skip silently
        break;
    } // end switch(op)

    // Default: advance PC by 4
    pc += 4;
}

// ---------------------------------------------------------------------------
// Execute a complete MIPS function starting at startVa.
// Returns when jr $ra jumps to an address < 0x80000000 or to 0.
// The return address is left in ctx->pc by the JR case.
// ---------------------------------------------------------------------------
static void execFunction(uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime,
                         uint32_t startVa, uint64_t& hi, uint64_t& lo, int depth)
{
    if (depth > MAX_DEPTH) {
        std::cerr << "[interp] Max call depth exceeded at va=0x"
                  << std::hex << startVa << std::dec << std::endl;
        return;
    }

    // Save the caller's return address — when this function runs JR $ra
    // and jumps to an ELF address or 0, we stop.
    const uint32_t savedRa = gpr32(ctx, 31);

    uint32_t pc = startVa;
    uint32_t count = 0;

    static thread_local uint32_t s_lastLogPc = 0;
    static thread_local uint32_t s_callCount = 0;
    if (depth == 0) {
        ++s_callCount;
        if (s_callCount <= 8) {
            std::cout << "[interp] Executing kseg0 0x" << std::hex
                      << startVa << std::dec
                      << " ($ra=0x" << std::hex << savedRa << std::dec << ")" << std::endl;
        }
    }

    while (count++ < MAX_INSNS) {
        const uint32_t phys = physAddr(pc);
        if (phys + 4 > RDRAM_SIZE) {
            std::cerr << "[interp] PC out of range: va=0x"
                      << std::hex << pc << std::dec << std::endl;
            break;
        }

        const uint32_t instr = rdramRead32(rdram, pc);

        // JR $ra (the normal function return): op=0, funct=8, rs=31
        // Handle here rather than in execOne so we can detect "are we done"
        const uint32_t op    = (instr >> 26) & 0x3F;
        const uint32_t rs    = (instr >> 21) & 0x1F;
        const uint32_t funct =  instr        & 0x3F;

        if (op == 0x00 && funct == 0x08) { // JR rs
            const uint32_t target = gpr32(ctx, rs);
            // Execute delay slot
            const uint32_t dsInstr = rdramRead32(rdram, pc + 4);
            uint32_t dsPc = pc + 4;
            execOne(rdram, ctx, runtime, dsPc, hi, lo, dsInstr, depth);
            ctx->pc = target;

            if (depth == 0 && s_callCount <= 8) {
                std::cout << "[interp] JR -> 0x" << std::hex << target << std::dec << std::endl;
            }

            // If target is outside kseg0 (ELF range or 0), this function is done.
            if (target < 0x80000000u) return;
            // Tail-call into another kseg0 region — continue the loop
            pc = target;
            continue;
        }

        // For all other instructions, delegate to execOne which also
        // handles JAL, JALR, and all branch instructions (they update pc
        // themselves and return early).
        execOne(rdram, ctx, runtime, pc, hi, lo, instr, depth);

        // execOne advances pc by 4 for non-branch instructions, or
        // sets pc itself for branches and returns. For branches it
        // already returned via early-return, so if we're here, pc
        // was simply incremented.
    }

    if (count >= MAX_INSNS) {
        std::cerr << "[interp] Instruction limit hit at va=0x"
                  << std::hex << pc << std::dec << std::endl;
    }
}

// ---------------------------------------------------------------------------
// Public entry point
// ---------------------------------------------------------------------------
void interpretMipsKseg0(uint8_t* rdram, R5900Context* ctx,
                        PS2Runtime* runtime, uint32_t startVa)
{
    // Thread-local HI/LO so nested calls share the state across the
    // interpreter session.
    thread_local uint64_t tl_hi = 0;
    thread_local uint64_t tl_lo = 0;

    execFunction(rdram, ctx, runtime, startVa, tl_hi, tl_lo, 0);
}
