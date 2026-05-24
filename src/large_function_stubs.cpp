// Stubs for functions referenced by large (>2MB) generated files that MSVC
// excludes from compilation. These are either large functions themselves or
// functions with Ghidra-modified boundaries that don't have generated code.
//
// To compile the actual implementations, use build_large.bat with clang-cl.

#include "ps2_runtime.h"
#include "ps2_runtime_macros.h"
#include "ps2_mips_interp.h"
#include <iostream>
#include <atomic>

#define MAKE_STUB(name) \
    void name(uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime) { \
        static bool w = false; \
        if (!w) { std::cerr << "[STUB] " #name << std::endl; w = true; } \
        ctx->pc = getRegU32(ctx, 31); \
    }

// Large functions excluded from MSVC build (>2MB generated C++)
MAKE_STUB(entry_270550_0x2742f0)
MAKE_STUB(entry_1eec40_0x1f1d80)
MAKE_STUB(entry_2a3c80_0x2a4b30)
MAKE_STUB(entry_2a3c80_0x2a4b70)
MAKE_STUB(sub_001BA130_0x1ba130)
// sub_0031D200_0x31d200 — game logic state machine (748KB, 55627 labels).
//
// When chunk .obj files are available (compiled via build_chunks_parallel.py),
// CMakeLists replaces this with the master dispatcher + 120 compiled chunks.
// Until then, we fall back to the MIPS interpreter reading opcodes from rdram.
// The interpreter dispatches JAL/JALR to the recompiled function table, so all
// OTHER compiled functions are called at native speed — only the scaffolding
// code within sub_0031D200 itself is interpreted.
void sub_0031D200_0x31d200(uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime)
{
    static std::atomic<uint64_t> s_calls{0};
    const auto n = s_calls.fetch_add(1u, std::memory_order_relaxed);
    if (n < 3u || (n % 50000u) == 0u) {
        std::cout << "[D200-interp] call#" << n
                  << " pc=0x" << std::hex << ctx->pc << std::dec << std::endl;
    }
    interpretMipsKseg0(rdram, ctx, runtime, ctx->pc);
}
// entry_31d280_0x3d5a00 — interior label within sub_0031D200, handled by master dispatcher
// (when chunks are compiled the master dispatcher handles all 558 entry points)

// Functions in the 0x308xxx-0x309xxx range introduced by Ghidra CSV
MAKE_STUB(sub_00308C90_0x308c90)
MAKE_STUB(FUN_003093a8_0x3093a8)
MAKE_STUB(sub_003090E8_0x3090e8)
MAKE_STUB(sub_00309980_0x309980)

// Functions referenced by large files with Ghidra-modified boundaries
MAKE_STUB(sub_00133660_0x133660)
MAKE_STUB(sub_001336C0_0x1336c0)
MAKE_STUB(sub_002A9FF0_0x2a9ff0)
MAKE_STUB(entry_1f1d80_0x1f2310)
MAKE_STUB(entry_1ba130_0x1be830)
MAKE_STUB(entry_1beeb0_0x1c9b10)
