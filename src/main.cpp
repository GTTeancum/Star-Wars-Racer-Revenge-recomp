#include "ps2_runtime.h"
#include "ps2_runtime_macros.h"
#include "ps2_recompiled_functions.h"
#include "ps2_recompiled_stubs.h"
#include "ps2_syscalls.h"
#include "ps2_stubs.h"
#include "register_functions.h"
#include "raylib.h"

#ifdef _DEBUG
#include "ps2_log.h"
#endif

#include <iostream>
#include <string>
#include <string_view>
#include <filesystem>
#include <thread>
#include <chrono>
#include <mutex>
#include <condition_variable>
#include <atomic>

// ---------------------------------------------------------------------------
// VBlank synchronization
// ---------------------------------------------------------------------------
static std::mutex s_vblankMtx;
static std::condition_variable s_vblankCv;
static std::atomic<uint64_t> s_vblankCounter{0};

// INTC handler for VBlank — runs in the interrupt worker thread.
// Lightweight: just signal the condition variable and return.
void vblank_notify(uint8_t* /*rdram*/, R5900Context* ctx, PS2Runtime* /*runtime*/)
{
    s_vblankCounter.fetch_add(1, std::memory_order_release);
    s_vblankCv.notify_one();
    ctx->pc = 0u;
}

// ---------------------------------------------------------------------------
// Test state function — synthetic game state for pipeline validation
// ---------------------------------------------------------------------------
// Called each VBlank while the real game state functions are not yet wired up.
// Submits an animated colored rectangle via GIF DMA (channel 2) so we can
// confirm the GIF→GS→framebuffer→display pipeline is end-to-end working.
// Registered at sentinel address 0x00FFF300 and set as the initial game state
// in the setGameState override below.
// ---------------------------------------------------------------------------
static void test_state_fn(uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime)
{
    static uint32_t s_frame = 0;
    ++s_frame;

    // Build a minimal GIF A+D packet in RDRAM at a safe scratch area.
    const uint32_t pktAddr = 0x44C000u;
    uint8_t* pkt = rdram + pktAddr;
    uint32_t off = 0;

    auto write128 = [&](uint64_t lo, uint64_t hi) {
        memcpy(pkt + off,     &lo, 8);
        memcpy(pkt + off + 8, &hi, 8);
        off += 16;
    };

    // Bright cycling colors so the test rect is clearly visible on screen.
    // Phase cycles 0-255 over ~85 frames; offset channels by 85 each for
    // a rainbow sweep that never goes fully black.
    const uint8_t phase = (uint8_t)(s_frame & 0xFFu);
    const uint8_t r = (uint8_t)(128u + ((phase * 1u) & 0x7Fu));       // 128-255
    const uint8_t g = (uint8_t)(128u + (((phase + 85u) * 1u) & 0x7Fu));
    const uint8_t b = (uint8_t)(128u + (((phase + 170u) * 1u) & 0x7Fu));

    // GIFtag: NLOOP=10, EOP=1, FLG=PACKED, NREG=1, REGS[0]=A+D (0xE)
    // (8 setup registers + 1 PRIM + 1 RGBAQ + 3 XYZ2 = 12 total
    //  reg-writes; but we share RGBAQ across vertices since flat-shaded
    //  → 10 unique A+D pairs: FRAME, ZBUF, XYOFFSET, SCISSOR, TEST,
    //  PRIM, RGBAQ, XYZ2, XYZ2, XYZ2.)
    write128(10ULL | (1ULL << 15) | (1ULL << 60), 0x0EULL);

    // GS A+D registers: lo=DATA, hi=ADDR
    write128((uint64_t)(10u << 16),                                     0x4CULL); // FRAME_1: fbw=10
    write128(1ULL << 32,                                                 0x4EULL); // ZBUF_1: zmsk=1
    write128(0ULL,                                                       0x18ULL); // XYOFFSET_1
    write128((639ULL << 16) | (447ULL << 48),                           0x40ULL); // SCISSOR_1
    write128(0ULL,                                                       0x47ULL); // TEST_1
    write128(3ULL,                                                       0x00ULL); // PRIM: triangle
    write128((uint64_t)r | ((uint64_t)g << 8) | ((uint64_t)b << 16) |
             (0x80ULL << 24) | (0x3F800000ULL << 32),                   0x01ULL); // RGBAQ
    // Triangle vertices — three XYZ2 writes. Coordinates in 4.4 fixed-
    // point screen space: divide visible value by 16. Place a tall
    // triangle near the centre: apex top, base bottom.
    write128(((uint64_t)(320u << 4) << 0)  | ((uint64_t)(100u << 4) << 16), 0x05ULL); // V0 apex (320,100)
    write128(((uint64_t)(120u << 4) << 0)  | ((uint64_t)(400u << 4) << 16), 0x05ULL); // V1 bot-left (120,400)
    write128(((uint64_t)(520u << 4) << 0)  | ((uint64_t)(400u << 4) << 16), 0x05ULL); // V2 bot-right (520,400)

    // Submit via GIF DMA (channel 2)
    const uint32_t qwc = off / 16u;
    runtime->memory().writeIORegister(0x1000A010u, pktAddr); // MADR
    runtime->memory().writeIORegister(0x1000A020u, qwc);     // QWC
    runtime->memory().writeIORegister(0x1000A000u, 0x101u);  // CHCR: DIR=from-mem, STR=1

    if ((s_frame % 300u) == 1u) {
        std::cout << "[TestState] frame=" << s_frame
                  << " RGB=(" << (int)r << "," << (int)g << "," << (int)b
                  << ") qwc=" << qwc << std::endl;
    }

    ctx->pc = 0u; // state function does not "return" to a caller
}

// ---------------------------------------------------------------------------
// Game frame dispatch — runs in the main game thread (sentinel)
// ---------------------------------------------------------------------------
//
// The game's frame processing is driven by the custom kernel's VBlank handler.
// On a real PS2:
//   1. VBlank fires → exception handler saves context → sets EPC=0x2FE400
//   2. eret resumes at 0x2FE400 (frame dispatch)
//   3. Frame dispatch reads state function ptr from 0x384670, calls it via jalr
//   4. After state function returns, iClearEventFlag(-0x54) runs
//
// In our recompiler, we simulate this with a dedicated frame loop thread
// that calls sub_002FE100 (which contains both the init at 0x2FE100 and
// the frame dispatch at 0x2FE400).
//
extern void sub_002FE100_0x2fe100(uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime);

static void gameFrameLoop(uint8_t* rdram, PS2Runtime* runtime)
{
    R5900Context frameCtx{};
    SET_GPR_U32(&frameCtx, 28, 0x3DD970u); // $gp from ELF
    SET_GPR_U32(&frameCtx, 29, 0x44BC80u); // $sp (game's exception handler stack)

    bool initialized = false;
    uint64_t lastTick = 0;

    while (!runtime->isStopRequested())
    {
        // Wait for VBlank signal from interrupt worker
        {
            std::unique_lock<std::mutex> lock(s_vblankMtx);
            s_vblankCv.wait_for(lock, std::chrono::milliseconds(50), [&]() {
                return s_vblankCounter.load(std::memory_order_acquire) > lastTick
                       || runtime->isStopRequested();
            });
        }
        if (runtime->isStopRequested()) break;
        lastTick = s_vblankCounter.load(std::memory_order_acquire);

        // Signal sema 3 each VBlank to keep thread 2 (RPC handler) alive.
        // On a real PS2, the custom kernel exception handler does this.
        {
            R5900Context tempCtx{};
            SET_GPR_U32(&tempCtx, 4, 3);
            SET_GPR_U32(&tempCtx, 3, 0x42); // SignalSema
            tempCtx.pc = 0;
            runtime->handleSyscall(rdram, &tempCtx, 0x0u);
        }

        if (!initialized) {
            initialized = true;

            // --- Phase 1: TLB/memory init ---
            // 0x2FE100 sets up virtual memory mappings from the subsystem table
            // at 0x384968. This must run before any game code accesses mapped memory.
            frameCtx.pc = 0x2FE100u;
            sub_002FE100_0x2fe100(rdram, &frameCtx, runtime);

            // --- Phase 2: Start the game's main loop in a separate thread ---
            //
            // The game's main loop at 0x302DF0 is an INFINITE LOOP. On a real PS2,
            // it runs in the exception handler context and gets preempted by VBlank.
            // In our recompiler, we can't preempt it — so we run it in its own
            // thread and let it loop independently.
            //
            // The main loop calls:
            //   0x308B00 (module manager check) → returns -1 (no modules)
            //   0x1000B8 → 0x2FEA30 → moduleDispatch → 0x2FE100 (TLB init)
            // Each iteration does a moduleDispatch which re-runs TLB init.
            //
            // DON'T set 0x384670 — the frame dispatch at 0x2FE400 will idle
            // (state func = 0 → skip jalr). The main loop runs independently.
            {
                // Pre-allocate the module manager state block.
                // The module table at 0x385160 has a state block pointer at
                // +0x1D4 (= 0x385334). The state block is 128 bytes: 32 slots
                // of 4 bytes each. Each slot holds a function pointer.
                //
                // When 0x308958 (module status checker) finds a non-zero, non-1
                // value in a slot, it CALLS it as a function pointer and clears
                // the slot. This is how IOP module init callbacks get dispatched.
                //
                // We populate the slots with the callerless IOP callback functions
                // discovered via Ghidra analysis. The module manager calls them
                // naturally as part of the main loop at 0x302DF0.
                uint32_t stateBlockAddr = *(uint32_t*)(rdram + 0x385334);
                if (stateBlockAddr == 0) {
                    stateBlockAddr = 0x44E000u;
                    Ps2FastWrite32(rdram, 0x385334u, stateBlockAddr);
                    std::memset(rdram + stateBlockAddr, 0, 128);
                }

                // Populate ALL module slots with callback function pointers.
                // The 15 callerless IOP callbacks (0x2FD1B8-0x2FDCE8) map
                // to module IDs. The module manager calls each one once and
                // clears the slot. After all slots are processed, the module
                // init chain eventually calls setGameState(0x2FDDF8).
                //
                // The last callback should be setGameState itself with the
                // game's first state function address.
                // Write setGameState (0x2FDDF8) to module slot 6.
                // When the module manager calls it, $a0 = module_id = 6.
                // Our custom setGameState override in main.cpp intercepts this
                // and writes a valid game state function instead.
                Ps2FastWrite32(rdram, stateBlockAddr + 6 * 4, 0x2FDDF8u);
                std::cout << "[Bootstrap] Wrote setGameState to module slot 6" << std::endl;

                // Start the main game loop in a background thread.
                //
                // The native main loop at 0x302DF0 is structurally an infinite
                // `while (1) { moduleDispatch(6); userInit(1); }` — on real PS2
                // it only exits via ExitThread on game shutdown. In our recomp,
                // the module dispatch path eventually tail-calls ExitThread as
                // part of its module-init cleanup; our ExitThread skip keeps
                // the thread alive but returns pc to the ExitThread caller's
                // $ra, which lies inside entry_2F6378 / entry_2FEA30 rather
                // than the main loop's fallthrough. That propagates up as a
                // non-fallthrough return and the main loop generator function
                // bails out.
                //
                // Workaround: restart the main loop in a tight outer loop so
                // subsequent iterations of the game's while(1) still run. This
                // is NOT a hack that masks state — it just restores the "loops
                // forever" semantics the recompiled control-flow didn't preserve.
                std::thread gameLoopThread([rdram, runtime]() {
                    R5900Context loopCtx{};
                    SET_GPR_U32(&loopCtx, 28, 0x3DD970u); // $gp
                    SET_GPR_U32(&loopCtx, 29, 0x44BC80u); // $sp
                    SET_GPR_U32(&loopCtx, 31, 0u);         // $ra = 0 (never returns)

                    std::cout << "[Bootstrap] Starting main game loop (0x302DF0) "
                              << "in background thread" << std::endl;

                    uint64_t restarts = 0;
                    while (!runtime->isStopRequested()) {
                        loopCtx.pc = 0x302DF0u;
                        SET_GPR_U32(&loopCtx, 29, 0x44BC80u); // reset $sp each restart
                        SET_GPR_U32(&loopCtx, 31, 0u);

                        try {
                            auto fn = runtime->lookupFunction(0x302DF0u);
                            if (fn) fn(rdram, &loopCtx, runtime);
                        } catch (const std::exception& e) {
                            std::cerr << "[GameLoop] Exception: " << e.what()
                                      << " (restart " << restarts << ")" << std::endl;
                        }

                        ++restarts;
                        if (restarts == 1 || (restarts % 1000u) == 0u) {
                            std::cerr << "[GameLoop] Main loop restart #" << restarts
                                      << std::endl;
                        }
                        // Yield briefly so we don't spin-burn if the loop
                        // returns instantly with no work done.
                        std::this_thread::sleep_for(std::chrono::microseconds(100));
                    }
                    std::cerr << "[GameLoop] Main loop final exit (stop requested)"
                              << std::endl;
                });
                gameLoopThread.detach();

                // Give the game loop a moment to start so setGameState fires
                // (module slot 6 → setGameState override → 0x384670 = 0xFFF300)
                std::this_thread::sleep_for(std::chrono::milliseconds(100));

                // --- Phase 3: One-shot initial GIF render ---
                // The frame loop will call test_state_fn each VBlank going forward.
                // Submit one packet immediately so the framebuffer is not black
                // during the first few frames while VBlank sync is warming up.
                {
                    R5900Context tsCtx{};
                    SET_GPR_U32(&tsCtx, 28, 0x3DD970u);
                    SET_GPR_U32(&tsCtx, 29, 0x44D000u);
                    test_state_fn(rdram, &tsCtx, runtime);
                    std::cout << "[Bootstrap] Initial GIF packet submitted." << std::endl;
                }

                // Check if setGameState was called by the module manager
                {
                    uint32_t sf = *(uint32_t*)(rdram + 0x384670);
                    std::cout << "[Bootstrap] 0x384670 (state fn) = 0x"
                              << std::hex << sf << std::dec << std::endl;
                }

                // Debug: dump key GS state values after first frame dispatch
                auto dumpGsState = [&]() {
                    uint32_t gsPtr = *(uint32_t*)(rdram + 0x443870);
                    uint32_t dat442b70 = *(uint32_t*)(rdram + 0x442B70);
                    // gp - 0x764C = 0x3D6324
                    uint8_t initFlag = rdram[0x3D6324];
                    std::cout << "[GS Debug] gsPtr=0x" << std::hex << gsPtr
                              << " DAT_442B70=0x" << dat442b70
                              << " initFlag=" << (int)initFlag
                              << std::dec << std::endl;
                    if (gsPtr != 0 && gsPtr < 0x2000000) {
                        uint32_t mode = *(uint32_t*)(rdram + gsPtr + 0x80);
                        uint32_t dbufPtr = *(uint32_t*)(rdram + gsPtr + 0x144);
                        uint16_t dbufIdx = *(uint16_t*)(rdram + gsPtr + 0x148);
                        std::cout << "[GS Debug] mode=0x" << std::hex << mode
                                  << " dbufPtr=0x" << dbufPtr
                                  << " dbufIdx=" << std::dec << dbufIdx << std::endl;
                    }
                };
                // --- Phase 4: Pre-populate GS state structure ---
                //
                // The game's GS subsystem reads DAT_00443870 (a GS state struct
                // pointer) on every render call. Pre-populate with a minimal
                // structure so GS functions don't early-return on null pointer.
                {
                    uint32_t gsStateAddr = 0x44C800u; // Safe BSS area
                    // Initialize the GS state structure with minimal valid data
                    std::memset(rdram + gsStateAddr, 0, 0x200);

                    // +0x80: GS mode/config value (passed to FUN_00258e70 as param_1)
                    // Set to a reasonable value: NTSC interlaced (common PS2 default)
                    Ps2FastWrite32(rdram, gsStateAddr + 0x80, 0x02); // mode = 2 (interlaced)

                    // +0x144: pointer to display buffer descriptor array
                    // +0x148: current display buffer index
                    // Allocate a display buffer descriptor at gsStateAddr + 0x180
                    uint32_t dbufDescAddr = gsStateAddr + 0x180;
                    Ps2FastWrite32(rdram, gsStateAddr + 0x144, dbufDescAddr);
                    Ps2FastWrite16(rdram, gsStateAddr + 0x148, 0); // buffer index 0

                    // Display buffer descriptor (0x20 bytes per entry):
                    // [+0x00] = display width (halfwords)
                    // [+0x0C] = some GS param
                    // [+0x10] = some GS param
                    Ps2FastWrite16(rdram, dbufDescAddr + 0x00, 640); // width
                    Ps2FastWrite16(rdram, dbufDescAddr + 0x0C, 448); // height?

                    // Write the GS state pointer to DAT_00443870
                    Ps2FastWrite32(rdram, 0x443870u, gsStateAddr);

                    // Unblock sub_00133660's $a0 gate.
                    //
                    // sub_00251A20 at label_251c04:
                    //   $v1 = mem[0x443DC8]
                    //   $a0 = sltiu($v1, 1)  →  $a0 = ($v1 < 1) ? 1 : 0
                    // sub_00133660 gate 2: if ($a0 != 0) early-return (no DMA)
                    //
                    // 0x443DC8 is in BSS (beyond ELF max VA 0x3D5BB0), so it
                    // stays zero-initialised. That makes $a0=1 every call →
                    // the gate always fires → no DMA is ever submitted.
                    //
                    // Write 1 here so $v1=1, sltiu(1,1)=0, gate passes.
                    // This is the "GS subsystem ready" flag written by the real
                    // boot sequence once IOP modules have initialised; we set it
                    // eagerly since we're bootstrapping without full IOP support.
                    Ps2FastWrite32(rdram, 0x443DC8u, 1u);
                    std::cout << "[Bootstrap] Initialized DAT_443DC8=1 (GS gate)" << std::endl;

                    // Unblock func_257080 (real VIF1 packet builder) first gate.
                    // READ32(0x442FB8) must equal 1 or func_257080 exits immediately.
                    // 0x442FB8 is BSS (zero-init) so we set it explicitly here.
                    Ps2FastWrite32(rdram, 0x442FB8u, 1u);
                    std::cout << "[Bootstrap] Initialized DAT_442FB8=1 (func_257080 gate)" << std::endl;

                    // Force-populate the render-list root pointer at
                    // 0x442F70. Normally sub_13FDA0 (deep in boot, never
                    // reached) writes a real ptr here from the IOP-loaded
                    // GFX module. Without it, func_257080 reads 0 →
                    // mem[0]+0x44 = junk → buffer-select branch effectively
                    // arbitrary; geometry submission never happens.
                    //
                    // Point to gsState (0x44c800) — a known-valid in-RAM
                    // struct (we set this up earlier in Phase 4). gsState
                    // +0x44 reads as zero (the struct was memset'd before
                    // we wrote specific fields), which makes the buffer-
                    // selector at sub_257080:0x2570ac branch one
                    // particular way. Either branch ought to let
                    // func_257080 reach its VIF1_MARK set.
                    Ps2FastWrite32(rdram, 0x442F70u, 0x44C800u);
                    std::cout << "[Bootstrap] Force-populated render-list root "
                                 "0x442F70 -> 0x44C800 (gsState)" << std::endl;

                    // sub_2F7DD8 returns mem[0x447B80 + ($a0 << 2)] — an
                    // indexed array of "subsystem ready" flags. sub_2F84F0
                    // (called via 0x239d2c by the boot subinit chain) spins
                    // at label_2f8650 calling sub_2F7DD8($a0=0) until it
                    // returns non-zero. Normally an IOP module init or
                    // hardware ack sets mem[0x447B80] when subsystem 0
                    // (probably IPU or VU) finishes initialising.
                    //
                    // We don't emulate that path, so the boot thread parks
                    // here forever (PC sampler: pc=0x239d2c streak=39+).
                    // Pre-populate slot 0 = 1 so the wait completes and
                    // boot proceeds to sub_237640 → sub_13FDA0 which
                    // populates the render-list root at mem[0x442F70].
                    //
                    // Populate slots 0..7 = 1 broadly — if other waits
                    // exist for other subsystem IDs we'll catch them too.
                    for (uint32_t i = 0; i < 8; ++i) {
                        Ps2FastWrite32(rdram, 0x447B80u + i * 4u, 1u);
                    }
                    std::cout << "[Bootstrap] Pre-populated subsystem-ready "
                              << "flags 0..7 at 0x447B80 (each = 1)" << std::endl;

                    // Fake module-table chain to break sub_239C40's jalr-spin
                    // at 0x23a124 → 0x23a134 (`jalr $t9` then
                    // `bnez $v0, label_23a124`). $t9 is derived from:
                    //   $a0 = mem[0x382B80]          (module table base ptr)
                    //   $t9 = mem[$a0 + 0x27C]       (sub-pointer / vtable)
                    //   $t9 = mem[$t9 + 0x30]        (function pointer)
                    //
                    // Everything in this chain is BSS-zero in our boot, so
                    // the dereferences read garbage from low memory and the
                    // resulting $v0 stays 1 → infinite spin.
                    //
                    // Bootstrap-fake the chain to land on sentinel 0x00FFF400
                    // (registered below as a `$v0 = 0; jr $ra` stub) so the
                    // spin exits.
                    {
                        // Use 0x44F000+ so we don't clobber the module-
                        // manager state block at 0x44E000 (allocated above
                        // and populated with callback slots, see Phase 2).
                        const uint32_t modBase    = 0x44F000u;
                        const uint32_t modVTable  = 0x44F300u;
                        Ps2FastWrite32(rdram, 0x382B80u, modBase);
                        Ps2FastWrite32(rdram, modBase + 0x27Cu, modVTable);
                        Ps2FastWrite32(rdram, modVTable + 0x30u, 0x00FFF400u);
                        std::cout << "[Bootstrap] Faked module-vtable chain "
                                     "[0x382B80] -> 0x" << std::hex << modBase
                                  << " [+0x27C] -> 0x" << modVTable
                                  << " [+0x30] -> 0xFFF400 (zero-return sentinel)"
                                  << std::dec << std::endl;
                    }

                    // Force GS PMODE to enable CRT1.
                    //
                    // Trace `[gs:latch-fail]` shows DISPFB1=0x1400 and
                    // DISPLAY1=0x1bf27f00000000 — both set — but
                    // pmode=0x0 means neither CRT is enabled, so
                    // `latchHostPresentationFrame` always magenta-fills.
                    // GsSetCrt (syscall 0x2) sets pmode|=1 if zero, but
                    // the boot path doesn't reach that syscall before
                    // the per-frame loop begins reading the GS. Force-
                    // enable here so the host can present whatever the
                    // GS rasterises.
                    //
                    // Bit 0 = EN1 (CRT1 enable). Other bits left at 0:
                    //   - MMOD/AMOD/SLBG: blend mode (single-CRT mode
                    //     doesn't use them).
                    //   - ALP: 0x00 = circuit-2 fully transparent
                    //     (irrelevant when CRT2 disabled).
                    {
                        auto& gs = runtime->memory().gs();
                        if ((gs.pmode & 0x3ull) == 0ull) {
                            gs.pmode |= 0x1ull;
                            std::cout << "[Bootstrap] Forced GS PMODE.EN1=1 (was 0)"
                                      << std::endl;
                        }
                    }

                    // VIF1_MARK (0x10003C30) must stay 0 so 0x251B10 calls func_257080
                    // (real VIF1 packet builder) instead of func_258E70 (stub).
                    // func_257080 will set VIF1_MARK=1 after building the packet;
                    // 0x251DF0 reads VIF1_MARK=1 to know it can submit the DMA.
                    // Do NOT write 1 here — that was the old (incorrect) approach.
                    std::cout << "[Bootstrap] Initialized GS state at 0x" << std::hex
                              << gsStateAddr << std::dec << " (VIF1_MARK stays 0)" << std::endl;
                }

                // Debug dump after all initialization
                dumpGsState();

                // --- Phase 6: Dump syscall dispatch table and state table ---
                //
                // The game's custom kernel keeps two tables at 0x384670+:
                //   0x384670      current game state function pointer
                //   0x384678+N*4  syscall dispatch table (N = syscall_id & 0x7C)
                //                 — also used for state-handler pointers
                //
                // Dump a couple seconds into the run, so any late initialization
                // (overlay copies, subsequent SetSyscall calls from module
                // callbacks, etc.) has a chance to complete.
                std::thread dumpThread([rdram]() {
                    std::this_thread::sleep_for(std::chrono::milliseconds(2500));
                    std::cout << "[Diag] Post-boot table dump:" << std::endl;
                    std::cout << "[Diag]   0x384670 (game state fn) = 0x"
                              << std::hex << *(uint32_t*)(rdram + 0x384670)
                              << std::dec << std::endl;
                    std::cout << "[Diag]   0x384678+ dispatch table:" << std::endl;
                    for (uint32_t i = 0; i < 32; ++i) {
                        uint32_t slot = *(uint32_t*)(rdram + 0x384678 + i * 4);
                        if (slot != 0) {
                            std::cout << "[Diag]     [" << i << "] (syscall 0x"
                                      << std::hex << (i * 4) << ") = 0x" << slot
                                      << std::dec << std::endl;
                        }
                    }

                    // Also peek at the loaded overlay regions to confirm memcpy
                    // actually ran — first 4 words of each kseg0 destination.
                    auto peek = [&](uint32_t physAddr, const char* label) {
                        uint32_t w0 = *(uint32_t*)(rdram + physAddr);
                        uint32_t w1 = *(uint32_t*)(rdram + physAddr + 4);
                        uint32_t w2 = *(uint32_t*)(rdram + physAddr + 8);
                        uint32_t w3 = *(uint32_t*)(rdram + physAddr + 12);
                        std::cout << "[Diag]   " << label << " (phys 0x"
                                  << std::hex << physAddr << ") = "
                                  << w0 << " " << w1 << " " << w2 << " " << w3
                                  << std::dec << std::endl;
                    };
                    peek(0x00074000, "overlay @ kseg0 0x80074000");
                    peek(0x00075000, "overlay @ kseg0 0x80075000");
                    peek(0x00076000, "overlay @ kseg0 0x80076000");
                    peek(0x00082000, "overlay @ phys  0x00082000");
                    peek(0x01F20000, "overlay @ phys  0x01F20000");

                    // Source data (in ELF), for comparison
                    peek(0x003849A0, "source 0x003849A0 (expected 0x80074000 content)");
                    peek(0x00384348, "source 0x00384348 (expected 0x80075000 content)");
                    peek(0x00383AD8, "source 0x00383AD8 (expected 0x80076000 content)");
                });
                dumpThread.detach();
            }
        } else {
            // Frame dispatch: read the game state function pointer from 0x384670
            // and call it directly.
            //
            // WHY NOT call sub_002FE100_0x2fe100 with ctx->pc = 0x2FE400:
            //   sub_002FE100 spans 0x2FE100-0x2FE660. The real frame dispatch is
            //   the code at 0x2FE400+, but there is an `eret` instruction at
            //   0x2FE3FC that compiles to `return` in C++. This makes everything
            //   after it unreachable — the function ALWAYS executes from 0x2FE100
            //   (ignoring ctx->pc), hits eret, and returns before reaching 0x2FE400.
            //   Adding 0x2FE400 to [[extra_entry_points]] would fix this properly
            //   (regeneration required); for now we replicate the dispatch inline.
            //
            // What 0x2FE400-0x2FE413 does on real hardware:
            //   lw  $at, 0x4670($zero+0x380000)  ; $at = mem[0x384670] = state fn ptr
            //   lui $sp, 0x45                     ; $sp base
            //   jalr $at (with addiu $sp,-0x4380 in delay slot)  ; call state fn
            const uint32_t stateFunc =
                *reinterpret_cast<const uint32_t*>(rdram + 0x384670u);

            // Log when the state function changes
            static uint32_t s_prevState = 0xFFFFFFFFu;
            if (stateFunc != s_prevState) {
                std::cout << "[Frame] State fn: 0x" << std::hex
                          << s_prevState << " -> 0x" << stateFunc
                          << std::dec << std::endl;
                s_prevState = stateFunc;
            }

            // Per-frame diagnostics: log key memory locations every 60 frames
            // to verify the GS state function is executing and passing its gates.
            static uint32_t s_frameCount = 0u;
            ++s_frameCount;
            if ((s_frameCount % 60u) == 1u) {
                const uint32_t dat443DC8 = *(uint32_t*)(rdram + 0x443DC8u);
                const uint32_t dat442B70 = *(uint32_t*)(rdram + 0x442B70u);
                const uint8_t  initFlag  = rdram[0x3D6324u];
                const uint32_t dat36C100 = *(uint32_t*)(rdram + 0x36C100u);
                const uint32_t modTable  = *(uint32_t*)(rdram + 0x38544Cu);
                // Rendering pipeline diagnostics:
                //   443870 = gsState ptr (needed by func_257080, 0x251DF0, func_256F60)
                //   442F70 = ptr dereferenced by func_257080 gate (READ32(READ32(442F70)+0x44))
                //   10003C30 = VIF1_MARK (1 → 0x251DF0 submits DMA; 0 → early-exit)
                const uint32_t gsStatePtr = *(uint32_t*)(rdram + 0x443870u);
                const uint32_t ptr442F70  = *(uint32_t*)(rdram + 0x442F70u);
                R5900Context diagCtx{};
                diagCtx.pc = 0u;
                const uint32_t vif1Mark   = runtime->Load32(rdram, &diagCtx, 0x10003C30u);
                std::cout << "[FrameDiag] frame=" << s_frameCount
                          << " state=0x" << std::hex << stateFunc
                          << " 443DC8=0x" << dat443DC8
                          << " 442B70=0x" << dat442B70
                          << " initFlag=" << std::dec << (int)initFlag
                          << " 36C100=0x" << std::hex << dat36C100
                          << " modTable=0x" << modTable
                          << " gsState=0x" << gsStatePtr
                          << " ptr442F70=0x" << ptr442F70
                          << " VIF1_MARK=0x" << vif1Mark
                          << std::dec << std::endl;
            }

            if (stateFunc != 0u) {
                try {
                    frameCtx.pc = stateFunc;
                    SET_GPR_U32(&frameCtx, 29, 0x44BC80u); // $sp
                    SET_GPR_U32(&frameCtx, 31, 0u);         // $ra = 0

                    auto fn = runtime->lookupFunction(stateFunc);
                    if (fn) {
                        fn(rdram, &frameCtx, runtime);
                    } else {
                        static uint32_t s_missingFn = 0u;
                        if (stateFunc != s_missingFn) {
                            s_missingFn = stateFunc;
                            std::cerr << "[Frame] No fn for state 0x"
                                      << std::hex << stateFunc
                                      << std::dec << std::endl;
                        }
                    }
                } catch (const std::exception& e) {
                    static uint32_t s_frameErrors = 0u;
                    if (s_frameErrors < 5u) {
                        const uint32_t ra =
                            (uint32_t)_mm_extract_epi32(frameCtx.r[31], 0);
                        const uint32_t sp =
                            (uint32_t)_mm_extract_epi32(frameCtx.r[29], 0);
                        std::cerr << "[Frame] Exception in state 0x"
                                  << std::hex << stateFunc
                                  << " pc=0x" << frameCtx.pc
                                  << " ra=0x" << ra
                                  << " sp=0x" << sp
                                  << ": " << e.what() << std::dec << std::endl;
                    }
                    ++s_frameErrors;
                    SET_GPR_U32(&frameCtx, 29, 0x44BC80u);
                }
            }

            // Run the VIF1 DMA submission path (0x251DF0) every frame.
            //
            // The rendering cycle:
            //   0x251B10 → func_257080 builds the VIF1 DMA packet, sets VIF1_MARK=1
            //   0x251DF0 → sees VIF1_MARK=1, submits DMA (CHCR=0x1C5), clears VIF1_MARK=0
            //
            // On real hardware these are driven by the game's thread/interrupt
            // structure.  Here we call 0x251DF0 explicitly after each stateFunc.
            {
                auto vif1Fn = runtime->lookupFunction(0x251DF0u);
                if (vif1Fn) {
                    try {
                        frameCtx.pc = 0x251DF0u;
                        SET_GPR_U32(&frameCtx, 29, 0x44BC80u);
                        SET_GPR_U32(&frameCtx, 31, 0u);
                        vif1Fn(rdram, &frameCtx, runtime);
                    } catch (const std::exception& e) {
                        static uint32_t s_vif1Errors = 0u;
                        if (s_vif1Errors < 5u)
                            std::cerr << "[Frame] Exception in 0x251DF0: "
                                      << e.what() << std::endl;
                        ++s_vif1Errors;
                        SET_GPR_U32(&frameCtx, 29, 0x44BC80u);
                    }
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Entry point sentinel — runs when _start returns (VA 0x100094)
//
// On a real PS2, _start never returns — the game runs via VSync interrupts.
// Our sentinel takes over after _start completes, registers the VBlank INTC
// handler, and enters the frame loop.
// ---------------------------------------------------------------------------
void entry_point_sentinel(uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime)
{
    std::cout << "Entry point completed — starting frame loop." << std::endl;

    // Register VBlank INTC handler (lightweight — just signals the CV)
    {
        R5900Context setupCtx{};
        SET_GPR_U32(&setupCtx, 4, 2);             // cause = VBLANK_START
        SET_GPR_U32(&setupCtx, 5, 0x00FFF100u);   // handler address
        SET_GPR_U32(&setupCtx, 6, 0);
        SET_GPR_U32(&setupCtx, 7, 0);
        SET_GPR_U32(&setupCtx, 28, 0);
        SET_GPR_U32(&setupCtx, 29, 0x1F80000u);
        ps2_syscalls::AddIntcHandler(rdram, &setupCtx, runtime);
    }

    // Remove the 0x54 (iClearEventFlag) syscall override.
    // The game registered 0x2FE440 (exception return handler) for this.
    // In the recompiler, the exception handler's eret causes ExitThread
    // when reached from the frame dispatch's post-state cleanup path.
    // We clear the override so -0x54 goes to the standard ClearEventFlag.
    {
        R5900Context clearCtx{};
        SET_GPR_U32(&clearCtx, 4, 0x54);
        SET_GPR_U32(&clearCtx, 5, 0);
        ps2_syscalls::SetSyscall(rdram, &clearCtx, runtime);
    }

    gameFrameLoop(rdram, runtime);
    ctx->pc = 0u;
}

// ---------------------------------------------------------------------------
// Headless smoke-test options (MANDATE §5: never steal user focus).
//   --headless              Hide the host window (FLAG_WINDOW_HIDDEN).
//   --frames N              Stop after N host frames (raylib BeginDrawing
//                           cycles, not VBlank ticks).
//   --screenshot PATH       Take a host-window screenshot just before stop.
//   --runtime-seconds N     Wall-clock watchdog; calls requestStop() after N.
// Positional arg is still the optional ELF path.
// ---------------------------------------------------------------------------
struct CliArgs
{
    std::string elfPath;
    bool        headless         = false;
    uint64_t    framesLimit      = 0;     // 0 = unlimited
    std::string screenshotPath;
    uint32_t    runtimeSeconds   = 0;     // 0 = unlimited
};

static CliArgs parseArgs(int argc, char* argv[])
{
    CliArgs a;
    for (int i = 1; i < argc; ++i)
    {
        std::string_view s(argv[i]);
        if (s == "--headless") { a.headless = true; }
        else if (s == "--frames" && i + 1 < argc) { a.framesLimit = std::stoull(argv[++i]); }
        else if (s == "--screenshot" && i + 1 < argc) { a.screenshotPath = argv[++i]; }
        else if (s == "--runtime-seconds" && i + 1 < argc) { a.runtimeSeconds = (uint32_t)std::stoul(argv[++i]); }
        else if (!s.empty() && s.front() != '-' && a.elfPath.empty()) { a.elfPath = std::string(s); }
        else
        {
            std::cerr << "Unrecognized arg: " << s << std::endl;
        }
    }
    return a;
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------
int main(int argc, char* argv[])
{
    CliArgs cli = parseArgs(argc, argv);
    std::string elfPath = cli.elfPath;

    if (elfPath.empty())
    {
        std::filesystem::path exePath(argv[0]);
        elfPath = (exePath.parent_path() / "SLUS_202.68").string();
    }

    if (!std::filesystem::exists(elfPath))
    {
        std::cerr << "ELF not found: " << elfPath << std::endl;
        std::cerr << "Usage: " << argv[0] << " [path/to/SLUS_202.68] "
                  << "[--headless] [--frames N] [--screenshot PATH] "
                  << "[--runtime-seconds N]" << std::endl;
        return 1;
    }

    if (cli.headless)
    {
        // raylib's SetConfigFlags ORs into CORE.Window.flags, so this is
        // additive with the runtime's later SetConfigFlags(FLAG_WINDOW_RESIZABLE).
        SetConfigFlags(FLAG_WINDOW_HIDDEN | FLAG_WINDOW_UNFOCUSED);
        std::cout << "[Headless] FLAG_WINDOW_HIDDEN set; smoke test mode." << std::endl;
    }

    PS2Runtime runtime;
    if (!runtime.initialize("Star Wars: Racer Revenge | PS2Recomp"))
    {
        std::cerr << "Failed to initialize PS2 runtime" << std::endl;
        return 1;
    }

    // Install post-frame hook for headless smoke-test bounds + screenshot.
    if (cli.framesLimit > 0 || !cli.screenshotPath.empty())
    {
        const uint64_t framesLimit = cli.framesLimit;
        const std::string screenshotPath = cli.screenshotPath;
        runtime.setPostFrameHook([framesLimit, screenshotPath](uint64_t frameIdx) -> bool {
            const bool isLastFrame =
                (framesLimit > 0) && (frameIdx + 1 >= framesLimit);
            // Screenshot policy:
            //   - If --frames is set: capture only on the last frame.
            //   - Else if --screenshot is set: capture every 60 frames so a
            //     wall-clock-stopped run (--runtime-seconds watchdog) still
            //     has a recent snapshot. Each call overwrites the previous;
            //     the final snapshot on disk is the one just before stop.
            if (!screenshotPath.empty())
            {
                const bool capture =
                    isLastFrame ||
                    (framesLimit == 0 && (frameIdx % 60u) == 59u);
                if (capture)
                {
                    TakeScreenshot(screenshotPath.c_str());
                    if (isLastFrame)
                    {
                        std::cout << "[Headless] Screenshot written: "
                                  << screenshotPath << " (frame " << frameIdx << ")"
                                  << std::endl;
                    }
                }
            }
            return isLastFrame;
        });
    }

    // PC-sampler watchdog: 4 Hz dump of the dispatcher's most-recent PC so
    // we can see where the game thread is sitting between FrameDiag prints.
    // gameFrameLoop only logs every 60 in-game frames; if the game thread
    // hangs inside a state-function spin, FrameDiag stops advancing and
    // we'd otherwise have no visibility. Disabled by default; enable via
    // --pc-sampler.
    if (true)
    {
        std::thread([&runtime]() {
            uint32_t lastPc = 0u;
            uint32_t streak = 0u;
            while (!runtime.isStopRequested())
            {
                std::this_thread::sleep_for(std::chrono::milliseconds(250));
                const uint32_t pc = runtime.debugPcSnapshot();
                const uint32_t ra = runtime.debugRaSnapshot();
                const uint32_t sp = runtime.debugSpSnapshot();
                if (pc == lastPc) {
                    ++streak;
                } else {
                    streak = 0u;
                    lastPc = pc;
                }
                std::cout << "[PCSample] pc=0x" << std::hex << pc
                          << " ra=0x" << ra
                          << " sp=0x" << sp
                          << std::dec << " streak=" << streak
                          << std::endl;
            }
        }).detach();
    }

    // Wall-clock watchdog: runs in a detached thread, calls requestStop().
    if (cli.runtimeSeconds > 0)
    {
        const uint32_t seconds = cli.runtimeSeconds;
        std::thread([&runtime, seconds]() {
            std::this_thread::sleep_for(std::chrono::seconds(seconds));
            std::cout << "[Headless] Runtime watchdog (" << seconds
                      << "s) expired; requesting stop." << std::endl;
            runtime.requestStop();
        }).detach();
    }

    registerAllFunctions(runtime);

    // Register the [[extra_entry_points]] trampolines. PS2Recomp v0.4 ignores
    // that TOML key, so `tools/inject_extra_entry_points.py` patches the
    // generated .cpp files (case + label) and emits this register function.
    extern void registerExtraEntryPoints(PS2Runtime& runtime);
    registerExtraEntryPoints(runtime);

    // Bulk-override all TOML `skip = [...]` functions with non-throwing
    // return-0 handlers. The default TODO_NAMED implementation throws,
    // which kills the gameLoopThread the moment it touches a skipped
    // function (most common cases: SIF helpers, VU0 code paths, large
    // loops with SIMD the recompiler can't yet emit). Each restart
    // cycles back from 0x302DF0 → ... → throw → restart, blocking
    // forward progress. Generated by tools/gen_stub_overrides.py.
    //
    // Targeted per-function overrides below this point take precedence
    // because they're registered AFTER this call.
    extern void registerStubOverrides(PS2Runtime& runtime);
    registerStubOverrides(runtime);

    // --- Game-specific interior trampolines NOT handled by extra_entry_points ---
    //
    // The PS2Recomp [[extra_entry_points]] feature now auto-generates
    // trampolines + pc-dispatch for the addresses listed in racer_revenge.toml
    // (0x2F7468, 0x2F7518, 0x2F68B0/memcpy_handler, 0x2FE820/memcpy_variant2,
    // 0x2FDEF0/memcpy_variant3, 0x2FE708/findAddress_handler, 0x308DF8, 0x30451C).
    // The trampolines below are for thread entry points that need explicit
    // registration outside that feature.
    {
        // 0x2F69D0  rpc_handlerThread
        // Thread 2 entry point — RPC command handler, blocks on semaphore 3.
        // Interior label inside sub_002F68F8 (which also contains memcpy_handler
        // at 0x2F68B0). The extra_entry_points TOML feature handles 0x2F68B0;
        // 0x2F69D0 is registered manually here for clarity.
        extern void sub_002F68F8_0x2f68f8(uint8_t*, R5900Context*, PS2Runtime*);
        runtime.registerFunction(0x2f69d0, +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime) {
            ctx->pc = 0x2f69d0u;
            sub_002F68F8_0x2f68f8(rdram, ctx, runtime);
        });

        // 0x2E9150  moduleMain
        // Module main entry — interior label inside sub_002E90F0.
        extern void sub_002E90F0_0x2e90f0(uint8_t*, R5900Context*, PS2Runtime*);
        runtime.registerFunction(0x2E9150, +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime) {
            ctx->pc = 0x2E9150u;
            sub_002E90F0_0x2e90f0(rdram, ctx, runtime);
        });

        // 0x251B10  gs_initState
        // Per-frame GS render dispatch — interior entry in sub_00251A20.
        // First call: initializes GS hardware registers.
        // Every call: invokes gs_dispatchHelper (0x133660) to drive the render path.
        extern void sub_00251A20_0x251a20(uint8_t*, R5900Context*, PS2Runtime*);
        runtime.registerFunction(0x251B10, +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime) {
            ctx->pc = 0x251B10u;
            sub_00251A20_0x251a20(rdram, ctx, runtime);
        });

        // --- Kseg0 overlay registration ---
        //
        // Racer Revenge copies three MIPS code blobs into kseg0 at boot:
        //   0x80074000 ← ELF VA 0x003849A0 (0x7A8 bytes): main syscall dispatcher
        //   0x80075000 ← ELF VA 0x00384348 (0x328 bytes): IOP/module dispatcher
        //   0x80076000 ← ELF VA 0x00383AD8 (0x740 bytes): kernel/thread dispatcher
        //
        // These are now compiled to C++ via [[overlays]] in racer_revenge.toml.
        // register_functions.cpp (generated by ps2xRecomp) registers them at their
        // kseg0 dest-VA addresses (0x80074000/5000/6000) with all interior
        // sub-entry-points auto-generated via [[extra_entry_points]].
        //
        // NO interpreter override needed here.  The recompiled overlay functions
        // run at native C++ speed, including all sub-handlers the game installs
        // via SetSyscall (0x80076440 → entry in overlay_kernel_76, etc.).
        // The old MIPS interpreter approach was removed when the overlay recompile
        // shipped (Foundation item #6, 2026-04-17).

        // 0x251DF0  vif1_frameSubmit
        // Per-frame VIF1 DMA handler — the "render" half of sub_00251A20.
        // This is the function that sits immediately after the 0x251B10 init path's `jr $ra`.
        // It saves ALL FP registers, calls func_2F5E50 + func_2F6870, then:
        //   1. Reads VIF1_MARK (0x10003C30): if != 1, skips to FP restore + return.
        //   2. Waits for previous VIF1 DMA to finish (polls CHCR bit 8).
        //   3. Waits for VIF1 idle (polls STAT bits 0-3).
        //   4. Clears VIF1_MARK to 0, then calls a display-buffer function pointer.
        //   5. Reads the VIF1 DMA packet ptr from gsState→dbuf[idx]+0x1C.
        //   6. Submits VIF1 DMA: MADR=pkt, TADR=0, CHCR=0x1C5 (chain+STR).
        // To use this instead of test_state_fn, change the setGameState override
        // stateFunc from 0x00FFF300u back to 0x00251DF0u and ensure func_258E70
        // (VIF packet builder, currently stubbed) populates dbuf[idx]+0x1C first.
        runtime.registerFunction(0x251DF0, +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime) {
            ctx->pc = 0x251DF0u;
            sub_00251A20_0x251a20(rdram, ctx, runtime);
        });
    }

    // --- 0x2FDDF8  setGameState override ---
    //
    // sub_002FDDF8 (native "setGameState") is NOT just a write to 0x384670.
    // Decompiled layout:
    //   mem[0x384670] = $a0                  (write game-state function pointer)
    //   call 0x2F5850(a0=1, a1=0x2FE300)     (install state-1 handler)
    //   call 0x2F5850(a0=2, a1=0x2FE300)     (install state-2 handler)
    //   call 0x2F5850(a0=3, a1=0x2FE300)     (install state-3 handler)
    //   return
    //
    // 0x2F5850 is a 3-insn wrapper: `$v1=0xD; syscall 0; jr $ra` — it invokes
    // the game's custom SetSyscall-equivalent (syscall 0x0D) to install state
    // transition handlers at 0x2FE300 for states 1/2/3 in the game's dispatch
    // table at 0x384678. My prior override SKIPPED all three calls, which may
    // leave the state machine without its state-1/2/3 entry handlers registered.
    //
    // The issue with the native function: when the module manager dispatches
    // it with $a0 = module_id (e.g. 6), it writes 6 into 0x384670. The frame
    // dispatch reads that as a function pointer and jumps to address 0x6 → crash.
    //
    // Fix: rewrite $a0 to a valid state function address BEFORE calling the
    // native function, so 0x384670 gets a real pointer AND the three handler-
    // install syscalls still fire.
    //
    // 0x251B10 = game's GS init function (callerless, found via Ghidra). It
    // self-terminates once DAT_442B70==1, which is safe to re-enter.
    // setGameState is at 0x2FDDF8, an interior label inside sub_002FDCE8_0x2fdce8.
    // After adding [[extra_entry_points]] address = "0x2FDDF8" to racer_revenge.toml
    // and regenerating, sub_002FDCE8_0x2fdce8 now has a pc-dispatch prologue that
    // routes to label_2fddf8 when ctx->pc == 0x2fddf8u.
    extern void sub_002FDCE8_0x2fdce8(uint8_t*, R5900Context*, PS2Runtime*);
    runtime.registerFunction(0x2FDDF8, +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime) {
        const uint32_t originalA0 = GPR_U32(ctx, 4);

        // When the module manager calls setGameState(module_id), $a0 is a small
        // integer (the module ID, e.g. 6), NOT a function pointer.  Writing that
        // raw value to 0x384670 would make the frame dispatch jump to address 6
        // → immediate crash.
        //
        // Phase 1: redirect module-id calls to the game's real GS init state
        // function (0x251B10) instead of test_state_fn (0xFFF300).
        //
        // 0x251B10 is the GS init / per-frame render dispatch:
        //   - First call:  writes DAT_442B70=1 and initFlag=1, then calls func_133660
        //   - Subsequent:  calls func_133660 every frame (the GIF/DMA render path)
        // func_258E70 (GS display-register setup) is called from the label_251c60
        // path (DAT_442B70=0), which only fires before the first frame.
        //
        // 0x251DF0 is the per-frame VIF1 DMA handler (registered in main.cpp).
        // Switch to it once func_258E70 is implemented and VIF1 is exercised.
        //
        // When $a0 is already a plausible code address (>= 0x100000), let it
        // through unchanged — that's the game's own state-transition logic.
        //
        // CYCLE 11 NOTE: we keep 0x251B10 here so that gameFrameLoop's
        // VIF1 pipeline (the real func_257080 → sub_2596A0 → sub_251DF0
        // chain) exercises every frame, even though the buffers are empty.
        // Switching to test_state_fn (0xFFF300) would make per-frame
        // triangles visible synthetically but defeat the infrastructure
        // verification of the full game-side VIF1 chain.
        const uint32_t stateFunc = (originalA0 < 0x100000u) ? 0x00251B10u : originalA0;
        if (originalA0 < 0x100000u) {
            SET_GPR_U32(ctx, 4, stateFunc);
            std::cout << "[setGameState] module-id 0x" << std::hex << originalA0
                      << " -> GS-init 0x" << stateFunc << std::dec << std::endl;
        } else {
            std::cout << "[setGameState] native call: $a0=0x" << std::hex << originalA0
                      << std::dec << std::endl;
        }

        // Run the native setGameState: writes $a0 to 0x384670 and calls
        // sub_002F5850 three times (syscall 0x0D) to install state-1/2/3
        // handlers at 0x2FE300 in the kernel dispatch table.
        ctx->pc = 0x2FDDF8u;
        sub_002FDCE8_0x2fdce8(rdram, ctx, runtime);
    });

    // sub_002EB0C8: real generated code now runs (was TODO stub; 2F6030/2F60C0 un-stubbed from TOML).
    // Calls sub_002EB050 (get GS state @ 0x383760), reads mem[0x383768], then calls
    // sub_002F6030 (GIF DMA sync/submit when flag=0) or sub_002F60C0 (flag!=0).

    // --- 0x1000ac  _start tail-jump trampoline ---
    // _start contains: jal 0x239c40 [0x1000a4] / move $a0,$v0 [delay 0x1000a8] / j 0x2fea30 [0x1000ac]
    // After ExitThread is skipped (tid=1), $ra = 0x1000ac.  The runtime tries
    // to dispatch to 0x1000ac which is inside sub_00100008 — not a registered
    // boundary — so recover-pc fires.  Register it as a trampoline that
    // executes the delay-slot side-effect then falls through to 0x2fea30.
    {
        extern void sub_002FEA30_0x2fea30(uint8_t*, R5900Context*, PS2Runtime*);
        runtime.registerFunction(0x1000ac, +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime) {
            // Delay slot: move $a0, $v0  (0x1000b0)
            SET_GPR_U32(ctx, 4, GPR_U32(ctx, 2));
            // j 0x2fea30
            ctx->pc = 0x2fea30u;
            sub_002FEA30_0x2fea30(rdram, ctx, runtime);
        });
    }

    // --- 0x00FFF100  vblank_notify (synthetic) ---
    // Native C++ handler registered at a sentinel VA. Installed as the INTC
    // VBLANK_START handler so the interrupt worker signals s_vblankCv on each
    // VBlank, waking gameFrameLoop. Not a real ELF function.
    runtime.registerFunction(0x00FFF100u, vblank_notify);

    // --- Boot-path stub overrides ---
    //
    // The TOML's [stubs] list routes these to `ps2_stubs::TODO_NAMED`,
    // which logs + throws "Unimplemented PS2 stub called". That kills the
    // gameLoopThread (background main-loop at 0x302DF0) and triggers a
    // catch+restart cycle that re-runs from 0x302DF0 indefinitely. After
    // 8 throws TODO_NAMED stops throwing and returns -1, but the
    // intervening cycle wastes 8x boot startup time and obscures real
    // state. Override the specific stubs we know the boot path crosses
    // with thin no-throw implementations.

    // sub_2F7E20 — first stub the boot subinit chain hits at
    // sub_2F84F0:label_2f8538. Real role unknown; in our context just
    // returning -1 lets sub_2F84F0 proceed past it.
    runtime.registerFunction(0x2F7E20, +[](uint8_t* /*rdram*/, R5900Context* ctx, PS2Runtime* /*runtime*/) {
        SET_GPR_S32(ctx, 2, -1);
        ctx->pc = GPR_U32(ctx, 31);
    });

    // sub_2F5FB0 — syscall 0x7A wrapper. Called from sub_2FDCB0 which
    // spins at sub_239C40:label_239d88 waiting for `(syscall_0x7A_result
    // & 0x40000) != 0`. On real PS2 this is a SIF/IOP status bit set by
    // the IOP after module init. Force-return a value with bit 0x40000
    // set so the gate passes and boot proceeds to func_311DF0 →
    // sub_237640 → sub_13FDA0 (the writer of 0x442F70).
    runtime.registerFunction(0x2F5FB0, +[](uint8_t* /*rdram*/, R5900Context* ctx, PS2Runtime* /*runtime*/) {
        SET_GPR_S32(ctx, 2, 0x40000);
        ctx->pc = GPR_U32(ctx, 31);
    });

    // sub_311DF0 — stub called at sub_239C40:label_239d34 (jal between
    // sub_2F84F0 and label_239d88's spin). With TODO_NAMED throwing,
    // this terminates the boot thread before it can reach the sub_237640
    // → sub_13FDA0 chain. Force-return 0 so boot proceeds.
    runtime.registerFunction(0x311DF0, +[](uint8_t* /*rdram*/, R5900Context* ctx, PS2Runtime* /*runtime*/) {
        SET_GPR_S32(ctx, 2, 0);
        ctx->pc = GPR_U32(ctx, 31);
    });

    // Boot-path stubs that throw and block forward progress. Each is the
    // next-in-line throw observed via smoke logs as we override the
    // previous one. Helper to keep the code short.
    auto stubNoThrowZero = +[](uint8_t* /*rdram*/, R5900Context* ctx, PS2Runtime* /*runtime*/) {
        SET_GPR_S32(ctx, 2, 0);
        ctx->pc = GPR_U32(ctx, 31);
    };
    runtime.registerFunction(0x2F8690, stubNoThrowZero); // sub_002F8690 — RA=0x2fdd6c

    // sub_2596A0 wrapper. Two purposes:
    //   1. Restore ctx->pc to $ra after the call. The recompiled body
    //      has a jr-$ra path whose switch table happens to include
    //      label 0x2596E0; when $ra equals that, the jr loops back
    //      into the function instead of returning, and the outer
    //      func_257080's check `ctx->pc != post-jal-ra` causes it to
    //      bail early.
    //   2. Force VIF1_MARK=1. The real function's purpose is
    //      DMA-submitting a VIF1 packet whose UNPACK command writes
    //      VIF1_MARK=1. Our DMA simulation drops this side effect.
    extern void sub_002596A0_0x2596a0(uint8_t*, R5900Context*, PS2Runtime*);
    runtime.registerFunction(0x2596A0, +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime) {
        ctx->pc = 0x2596A0u;
        sub_002596A0_0x2596a0(rdram, ctx, runtime);
        const uint32_t ra = GPR_U32(ctx, 31);
        if (ctx->pc != ra) {
            ctx->pc = ra;
        }
        runtime->memory().writeIORegister(0x10003C30u, 1u);
    });

    // Decision (cycle 3 / 2026-05-22 05:45): tried `runtime.registerFunction(
    // 0x239C40, returnZero)` to skip boot_subinit entirely. Result:
    // FrameDiag dropped from 7 to 1 (gameFrameLoop broken). Removed.
    // sub_239C40 does important setup gameFrameLoop depends on, even
    // when its deeper boot path is gated by IOP-loaded modules we
    // can't run.

    // sub_002FDCE8 (entry at 0x2FDCE8, NOT the 0x2FDDF8 setGameState
    // interior label). boot_subinit at sub_239C40:label_239d68 calls this
    // in a spin: while sub_002FDCE8($a0=$sp+0x260) returns 0, loop. The
    // function appears to be a syscall/RPC waiter that an IOP event
    // would normally satisfy. Force-return 1 so the spin exits and
    // boot proceeds.
    //
    // setGameState's interior label at 0x2FDDF8 is registered separately
    // and calls sub_002FDCE8_0x2fdce8 DIRECTLY (not through the
    // dispatcher) — so this override does not break the setGameState
    // path, which still runs the real function body.
    runtime.registerFunction(0x2FDCE8, +[](uint8_t* /*rdram*/, R5900Context* ctx, PS2Runtime* /*runtime*/) {
        SET_GPR_S32(ctx, 2, 1);
        ctx->pc = GPR_U32(ctx, 31);
    });

    // --- 0x2F7150  SIF/IOP wait-loop bypass ---
    //
    // sub_2F7150 is a SIF-style RPC dispatcher: it marks an in-progress flag
    // at mem[0x44765C] = 1, sends a request via func_2F6DA0, then enters a
    // spin loop at 0x2f7258 calling sub_2F6DD0 (syscall 0x7C) until the
    // flag clears. On real PS2 hardware the IOP side processes the request
    // and writes 0 to 0x44765C, exiting the spin.
    //
    // We don't emulate the IOP, so the flag never clears and the game
    // thread hangs forever on the first call to 0x251B10. Override the
    // function with a clean "no work done" return: clear the in-progress
    // flag immediately and set $v0=0 (zero items processed).
    //
    // Verified via PC sampler: pc=0x2f7258 ra=0x2f7264 streak forever
    // before this override.
    runtime.registerFunction(0x2F7150, +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* /*runtime*/) {
        // Clear the in-progress flag so the next caller can re-enter cleanly.
        Ps2FastWrite32(rdram, 0x44765Cu, 0u);
        SET_GPR_U32(ctx, 2, 0u);   // $v0 = 0 (success, 0 items)
        ctx->pc = GPR_U32(ctx, 31); // jr $ra
    });

    // --- 0x00FFF400  zero-return sentinel ---
    // Sets $v0=0 then jr $ra. Used to break boot-side jalr-spins where
    // the called function pointer would normally return 0 in $v0 to
    // signal completion. See the bootstrap comment for the fake module-
    // vtable chain at 0x382B80.
    runtime.registerFunction(0x00FFF400u, +[](uint8_t* /*rdram*/, R5900Context* ctx, PS2Runtime* /*runtime*/) {
        SET_GPR_S32(ctx, 2, 0);
        ctx->pc = GPR_U32(ctx, 31);
    });

    // --- 0x00FFF300  test_state_fn (synthetic) ---
    // Synthetic game state registered at a sentinel VA outside ELF space.
    // Submits an animated color rectangle via GIF DMA each frame to validate
    // the GIF→GS→framebuffer pipeline. The setGameState override installs
    // gs_initState (0x251B10) as the real state; test_state_fn is kept for
    // regression testing when gs_initState is unreachable.
    runtime.registerFunction(0x00FFF300u, test_state_fn);

    // --- No-op stubs for giant excluded function range ---
    //
    // The range 0x31D200-0x3D5A00 was excluded from TOML (748KB, ~2.1GB C++ output).
    // sub_002E91F0 (called by sub_002EADE0 from sub_00239C40 at startup) iterates a
    // function-pointer table (0x3CC450-0x3CC650) whose entries point into this range.
    // Without stubs, each call triggers lookupFunction's "Warning:" stderr write +
    // stack-scan fallback logic, making startup take 30-60 seconds for ~128 iterations.
    // These stubs return to $ra immediately (no-op), matching the fallback behaviour
    // but without the overhead.  The real functions in this range are small callbacks
    // (module init slots); returning early is equivalent to an empty module init.
    {
        auto dataSegNoop = +[](uint8_t* /*rdram*/, R5900Context* ctx, PS2Runtime* /*runtime*/) {
            if (ctx) ctx->pc = GPR_U32(ctx, 31); // jr $ra — return to caller
        };
        // Cover the observed dispatch range 0x3C7B80-0x3CE000 at 4-byte alignment.
        // The step is 4 so any function at any valid MIPS alignment is covered.
        // Upper bound extended from 0x3CA000 to 0x3CE000: boot output shows
        // "Warning: Function at address 0x3ca020 not found" through ~0x3cb300;
        // the data-segment module-registration callbacks run up to ~0x3D5BB0
        // but we cap at 0x3CE000 to avoid covering legitimate late-registered
        // functions.  If warnings persist above 0x3CE000, extend further.
        for (uint32_t addr = 0x3C7B80u; addr < 0x3CE000u; addr += 4u) {
            runtime.registerFunction(addr, dataSegNoop);
        }
    }

    // --- Load and run ---

    if (!runtime.loadELF(elfPath))
    {
        std::cerr << "Failed to load ELF: " << elfPath << std::endl;
        return 1;
    }

    // --- Bootstrap thread ---
    // Launches gameFrameLoop after a delay to let the ELF boot sequence
    // complete.  The delay is generous (3 s) so the module dispatcher and
    // overlay memcpys have all finished before we start calling into game
    // code from the frame side.
    //
    // This replaces the old "entry_point_sentinel at 0x100094" approach,
    // which relied on the dispatch loop seeing PC=0x100094.  It never did:
    // sub_00100008 executes through that address inline.
    {
        uint8_t* rdramPtr = runtime.memory().getRDRAM();
        std::thread([rdramPtr, &runtime]() {
            std::this_thread::sleep_for(std::chrono::seconds(3));
            if (!runtime.isStopRequested()) {
                std::cout << "[Bootstrap] 3-second delay elapsed — starting game frame loop."
                          << std::endl;
                // Run the same one-time sentinel logic that was in entry_point_sentinel:
                // clear the 0x54 override so iClearEventFlag routes to the standard handler.
                {
                    R5900Context clearCtx{};
                    SET_GPR_U32(&clearCtx, 4, 0x54);
                    SET_GPR_U32(&clearCtx, 5, 0);
                    ps2_syscalls::SetSyscall(rdramPtr, &clearCtx, &runtime);
                }

                // Start the interrupt worker (generates VBlank pulses, sets INTC_STAT bit 2).
                //
                // sub_002F6030 and sub_002F60C0 spin on INTC_STAT (0x1000F000) bit 2
                // (VBLANK_S) waiting for a VBlank before continuing.  The interrupt worker
                // fires every ~16.67ms and calls raiseIntcStat(1<<2) so those spin loops
                // see the bit and exit.  Without this, the first frame dispatch hangs
                // indefinitely inside sub_002F6030's polling loop.
                ps2_syscalls::EnsureVSyncWorkerRunning(rdramPtr, &runtime);

                // Register the VBlank INTC handler so vblank_notify fires and signals
                // the frame loop's condition variable (s_vblankCv).  This mirrors what
                // entry_point_sentinel used to do before it was removed.
                {
                    R5900Context intcCtx{};
                    SET_GPR_U32(&intcCtx, 4, 2);             // cause = VBLANK_START (bit 2)
                    SET_GPR_U32(&intcCtx, 5, 0x00FFF100u);   // handler address (vblank_notify)
                    SET_GPR_U32(&intcCtx, 6, 0);             // next = 0 (add to head)
                    SET_GPR_U32(&intcCtx, 7, 0);
                    SET_GPR_U32(&intcCtx, 28, 0);
                    SET_GPR_U32(&intcCtx, 29, 0x1F80000u);
                    ps2_syscalls::AddIntcHandler(rdramPtr, &intcCtx, &runtime);
                }

                gameFrameLoop(rdramPtr, &runtime);
            }
        }).detach();
    }

    std::cout << "Starting Star Wars: Racer Revenge..." << std::endl;
    runtime.run();

#ifdef _DEBUG
    ps2_log::print_saved_location();
#endif
    return 0;
}
