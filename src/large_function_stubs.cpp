// Stubs for functions referenced by large (>2MB) generated files that MSVC
// excludes from compilation. These are either large functions themselves or
// functions with Ghidra-modified boundaries that don't have generated code.
//
// To compile the actual implementations, use build_large.bat with clang-cl.

#include "ps2_runtime.h"
#include "ps2_runtime_macros.h"
#include <iostream>

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
MAKE_STUB(sub_0031D200_0x31d200)
MAKE_STUB(entry_31d280_0x3d5a00)

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
