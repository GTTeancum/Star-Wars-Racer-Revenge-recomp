#include "ps2_runtime.h"
#include "ps2_runtime_macros.h"
#include "ps2_recompiled_functions.h"
#include "ps2_recompiled_stubs.h"
#include "ps2_syscalls.h"
#include "ps2_stubs.h"
#include "register_functions.h"

#ifdef _DEBUG
#include "ps2_log.h"
#endif

#include <iostream>
#include <string>
#include <filesystem>

// Thread entry trampolines for interior addresses not registered by the recompiler.
// These set ctx->pc and call the parent function. The parent's generated code uses
// ctx->pc to resume at the right point via its internal label/goto structure.
// When the parent function doesn't have pc-based dispatch, we call it directly
// and rely on the thread's separate stack/context to isolate execution.

// 0x2f69d0 is a thread entry inside sub_002F68F8 (IOP RPC handler thread)
extern void sub_002F68F8_0x2f68f8(uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime);
void thread_entry_0x2f69d0(uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime)
{
    ctx->pc = 0x2f69d0u;
    sub_002F68F8_0x2f68f8(rdram, ctx, runtime);
}

int main(int argc, char* argv[])
{
    std::string elfPath;

    if (argc >= 2)
    {
        elfPath = argv[1];
    }
    else
    {
        // Default: look for ELF next to executable
        std::filesystem::path exePath(argv[0]);
        elfPath = (exePath.parent_path() / "SLUS_202.68").string();
    }

    if (!std::filesystem::exists(elfPath))
    {
        std::cerr << "ELF not found: " << elfPath << std::endl;
        std::cerr << "Usage: " << argv[0] << " [path/to/SLUS_202.68]" << std::endl;
        return 1;
    }

    PS2Runtime runtime;
    if (!runtime.initialize("Star Wars: Racer Revenge | PS2Recomp"))
    {
        std::cerr << "Failed to initialize PS2 runtime" << std::endl;
        return 1;
    }

    registerAllFunctions(runtime);

    // Register thread entry trampolines for interior addresses
    runtime.registerFunction(0x2f69d0, thread_entry_0x2f69d0);

    if (!runtime.loadELF(elfPath))
    {
        std::cerr << "Failed to load ELF: " << elfPath << std::endl;
        return 1;
    }

    std::cout << "Starting Star Wars: Racer Revenge..." << std::endl;
    runtime.run();

#ifdef _DEBUG
    ps2_log::print_saved_location();
#endif
    return 0;
}
