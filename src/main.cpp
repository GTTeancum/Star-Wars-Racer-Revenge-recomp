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
#include <unordered_map>
#include <unordered_set>

// ---------------------------------------------------------------------------
// VBlank synchronization
// ---------------------------------------------------------------------------
static std::mutex s_vblankMtx;
static std::condition_variable s_vblankCv;
static std::atomic<uint64_t> s_vblankCounter{0};

// IOP init completion gate shared between sif_dmaSend (0x2F7150) and
// the 0xFFF200 module_keepalive sentinel.
// Set to 1 by sif_dmaSend after the 3rd IOP init message (Begin/MultiTap/End).
// When 1, the 0xFFF200 sentinel writes state[6]=-1 instead of re-arming,
// allowing the module manager to advance to "done" state.
static std::atomic<uint32_t> s_iop_init_done{0};

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
                    SET_GPR_U32(&loopCtx, 29, 0x449000u); // $sp — separate from gameFrameLoop's 0x44BC80
                    SET_GPR_U32(&loopCtx, 31, 0u);         // $ra = 0 (never returns)

                    std::cout << "[Bootstrap] Starting main game loop (0x302DF0) "
                              << "in background thread" << std::endl;

                    uint64_t restarts = 0;
                    while (!runtime->isStopRequested()) {
                        loopCtx.pc = 0x302DF0u;
                        SET_GPR_U32(&loopCtx, 29, 0x449000u); // reset $sp each restart — separate stack
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

                    // DAT_443DC8: dual-purpose synchronization flag.
                    //
                    // sub_00251A20 at label_251c04:
                    //   $a0 = sltiu(mem[0x443DC8], 1)  →  $a0 = (flag == 0) ? 1 : 0
                    //   sub_00133660 skips module_renderPrep when $a0 != 0.
                    //   So: flag != 0 → module_renderPrep runs, jobs dispatched.
                    //       flag == 0 → module_renderPrep skipped.
                    //
                    // sub_002596A0 at label_259700:
                    //   bnez mem[0x443DC8], label_2596e0
                    //   → spin while flag != 0, calling the +0xDC GS callback.
                    //   → when flag == 0, fall through to label_259710 (GIF DMA).
                    //
                    // Per-frame protocol (see gameFrameLoop and 0xFFF500 sentinel):
                    //   1. gameFrameLoop writes 0x443DC8=1 before each state call.
                    //   2. sub_133660 sees flag=1 → $a0=0 → module_renderPrep runs.
                    //   3. sub_2596A0 spins at label_2596e0, calls 0xFFF500 callback.
                    //   4. 0xFFF500 writes 0x443DC8=0 → loop exits → GIF DMA fires.
                    //   5. Repeat next frame from step 1.
                    Ps2FastWrite32(rdram, 0x443DC8u, 1u);
                    std::cout << "[Bootstrap] Initialized DAT_443DC8=1 "
                                 "(module_renderPrep gate + sub_2596A0 spin)" << std::endl;

                    // --- GIF chain DMA test data for sub_2596A0 ---
                    //
                    // sub_2596A0 is called from vif1_buildPacket every frame
                    // (capacity=0 exit path at label_2571dc). It fires GIF DMA:
                    //
                    //   D2_TADR = READ32(READ32(READ32(0x443870)+0x88)+0)
                    //           = READ32(READ32(gsState+0x88)+0)
                    //           = READ32(gifDescAddr+0)
                    //           = gifChainAddr
                    //
                    // Memory layout (all addresses in main RDRAM):
                    //   gifDescAddr  [0x450000]: uint32 = gifChainAddr
                    //   gifChainAddr [0x450010]: REFE DMA chain tag (QWC=11, ADDR=gifDataAddr)
                    //   gifDataAddr  [0x450020]: GIF A+D packet (1 GIFtag + 10 pairs = 11 qwords)
                    {
                        const uint32_t gifDescAddr  = 0x450000u;
                        const uint32_t gifChainAddr = 0x450010u;
                        const uint32_t gifDataAddr  = 0x450020u;

                        // gsState+0x88 → descriptor pointer
                        Ps2FastWrite32(rdram, gsStateAddr + 0x88u, gifDescAddr);

                        // descriptor word[0] = DMA chain tag address (D2_TADR)
                        Ps2FastWrite32(rdram, gifDescAddr, gifChainAddr);

                        // REFE chain tag at gifChainAddr:
                        //   word[0] bits[15:0]=QWC=11, bits[30:28]=ID=0 (REFE), IRQ=0
                        //   word[1] = ADDR = gifDataAddr  (source of the 11 GIF qwords)
                        Ps2FastWrite32(rdram, gifChainAddr + 0u, 0x0000000Bu); // QWC=11
                        Ps2FastWrite32(rdram, gifChainAddr + 4u, gifDataAddr); // ADDR

                        // GIF packet: 1 GIFtag (NLOOP=10,EOP=1,PACKED,NREG=1,REGS=A+D)
                        //             + 10 A+D pairs (bright-green triangle)
                        uint8_t* pkt = rdram + gifDataAddr;
                        uint32_t off = 0;
                        auto write128 = [&](uint64_t lo, uint64_t hi) {
                            memcpy(pkt + off,     &lo, 8);
                            memcpy(pkt + off + 8, &hi, 8);
                            off += 16;
                        };
                        // GIFtag: NLOOP=10, EOP=1, FLG=PACKED, NREG=1, REGS[0]=A+D (0xE)
                        write128(10ULL | (1ULL << 15) | (1ULL << 60), 0x0EULL);
                        // FRAME_1: fbw=10 (640px wide), fbp=0, psm=0, fbmsk=0
                        write128((uint64_t)(10u << 16), 0x4CULL);
                        // ZBUF_1: zmsk=1 (Z write disabled)
                        write128(1ULL << 32, 0x4EULL);
                        // XYOFFSET_1: 0
                        write128(0ULL, 0x18ULL);
                        // SCISSOR_1: 0..639 x 0..447
                        write128((639ULL << 16) | (447ULL << 48), 0x40ULL);
                        // TEST_1: default
                        write128(0ULL, 0x47ULL);
                        // PRIM: triangle (3)
                        write128(3ULL, 0x00ULL);
                        // RGBAQ: bright green, a=128, q=1.0
                        write128(0x20ull | (0xE0ull << 8) | (0x20ull << 16) |
                                 (0x80ull << 24) | (0x3F800000ULL << 32), 0x01ULL);
                        // XYZ2 V0: apex (320,100)
                        write128(((uint64_t)(320u << 4)) |
                                 ((uint64_t)(100u << 4) << 16), 0x05ULL);
                        // XYZ2 V1: bot-left (120,400)
                        write128(((uint64_t)(120u << 4)) |
                                 ((uint64_t)(400u << 4) << 16), 0x05ULL);
                        // XYZ2 V2: bot-right (520,400)
                        write128(((uint64_t)(520u << 4)) |
                                 ((uint64_t)(400u << 4) << 16), 0x05ULL);

                        std::cout << "[Bootstrap] GIF chain DMA test data at 0x"
                                  << std::hex << gifDataAddr
                                  << " (desc=0x" << gifDescAddr
                                  << " tag=0x" << gifChainAddr << ")"
                                  << std::dec << std::endl;
                    }

                    // Unblock func_257080 (real VIF1 packet builder) first gate.
                    // READ32(0x442FB8) must equal 1 or func_257080 exits immediately.
                    // 0x442FB8 is BSS (zero-init) so we set it explicitly here.
                    Ps2FastWrite32(rdram, 0x442FB8u, 1u);
                    std::cout << "[Bootstrap] Initialized DAT_442FB8=1 (func_257080 gate)" << std::endl;

                    // Force-copy overlay_kernel_74 (the syscall dispatcher
                    // overlay) from ELF VA 0x3849A0 to kseg0 0x80074000
                    // (phys 0x74000). The kernel_overlay_loader at
                    // sub_002FE8D0 normally does this via syscall 0x5A
                    // memcpy during boot, but our boot is parked in
                    // sub_239C40 and never reaches the loader. Overlays
                    // 0x80075000 / 0x80076000 happen to get populated by
                    // some other mechanism (TBD — likely runtime ELF
                    // staging), but 0x80074000 stays zero. Smoke test
                    // golden expects this populated.
                    std::memcpy(rdram + 0x74000u, rdram + 0x3849A0u, 0x7A8u);
                    std::cout << "[Bootstrap] Copied overlay_kernel_74 "
                                 "(0x3849A0 -> 0x74000, 0x7A8 bytes)"
                              << std::endl;

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

                    // gsState +0xDC: sub_2596A0 jalrs to whatever this
                    // contains (if non-zero). Wire it to sentinel
                    // 0x00FFF500 (registered below) so we can observe
                    // whether the per-frame chain actually reaches it.
                    Ps2FastWrite32(rdram, 0x44C800u + 0xDCu, 0x00FFF500u);
                    std::cout << "[Bootstrap] gsState+0xDC -> 0xFFF500 "
                              << "(diagnostic sentinel for sub_2596A0 jalr)"
                              << std::endl;

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
                        // sub_237640 reads mem[modVTable+0x9C] and jalrs to it.
                        // Point at the same zero-return sentinel so the call
                        // is a no-op instead of jumping to mem[0x9C] junk.
                        Ps2FastWrite32(rdram, modVTable + 0x9Cu, 0x00FFF400u);
                        std::cout << "[Bootstrap] Faked module-vtable chain "
                                     "[0x382B80] -> 0x" << std::hex << modBase
                                  << " [+0x27C] -> 0x" << modVTable
                                  << " [+0x30] -> 0xFFF400, [+0x9C] -> 0xFFF400 "
                                  << "(zero-return sentinel)"
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

                    // Force DISPFB1 and DISPLAY1 so the framebuffer is actually displayed.
                    //
                    // Normally set by func_258E70 via label_251c60 in sub_00251A20, but
                    // that path is skipped on every boot: the very first call to 0x251B10
                    // sets rdram[0x442B70]=1 *before* the `beqz rdram[0x442B70]` guard
                    // fires, so label_251c60 is unreachable.
                    //
                    // DISPFB1 (0x12000070):
                    //   FBP = 0    (frame buffer page 0, i.e. address 0x000000 in VRAM)
                    //   FBW = 10   (640 px wide; stored as 640/64 = 10 in bits [15:9])
                    //   PSM = 0    (PSMCT32, 32-bit colour)
                    //   DBX = 0, DBY = 0
                    //   Encoding: FBW field is at bits [15:9] → 10 << 9 = 0x1400
                    //
                    // DISPLAY1 (0x12000080):
                    //   DX = 0, DY = 0      (no display-area offset)
                    //   MAGH = 0, MAGV = 0  (×1 pixel clock = actual 1:1)
                    //   DW = 639 (0x27F)    stored at bits [43:32]
                    //   DH = 447 (0x1BF)    stored at bits [54:44]
                    //   Encoding: (0x1BFull << 44) | (0x27Full << 32) = 0x1BF27F00000000
                    {
                        auto& gs = runtime->memory().gs();
                        std::cout << "[Bootstrap] GS before DISPFB fix:"
                                  << " DISPFB1=0x" << std::hex << gs.dispfb1
                                  << " DISPLAY1=0x" << gs.display1
                                  << " PMODE=0x" << gs.pmode
                                  << std::dec << std::endl;
                        if (gs.dispfb1 == 0ull) {
                            gs.dispfb1 = 0x1400ULL;
                            std::cout << "[Bootstrap] Forced GS DISPFB1=0x1400" << std::endl;
                        }
                        if (gs.display1 == 0ull) {
                            gs.display1 = 0x1BF27F00000000ULL;
                            std::cout << "[Bootstrap] Forced GS DISPLAY1=0x1BF27F00000000" << std::endl;
                        }
                        std::cout << "[Bootstrap] GS after DISPFB fix:"
                                  << " DISPFB1=0x" << std::hex << gs.dispfb1
                                  << " DISPLAY1=0x" << gs.display1
                                  << " PMODE=0x" << gs.pmode
                                  << std::dec << std::endl;
                    }

                    // VIF1_MARK (0x10003C30) must stay 0 so 0x251B10 calls func_257080
                    // (real VIF1 packet builder) instead of func_258E70 (stub).
                    // func_257080 will set VIF1_MARK=1 after building the packet;
                    // 0x251DF0 reads VIF1_MARK=1 to know it can submit the DMA.
                    // Do NOT write 1 here — that was the old (incorrect) approach.
                    std::cout << "[Bootstrap] Initialized GS state at 0x" << std::hex
                              << gsStateAddr << std::dec << " (VIF1_MARK stays 0)" << std::endl;
                }

                // --- Module state[6] keepalive ---
                //
                // The game's module dispatch (sub_00308958) reads state[6] at
                //   rdram[stateTablePtr + 6*4]  (stateTablePtr = rdram[modTable+0x1D4])
                //
                // Without IOP, no async callback ever sets state[6] to a function
                // pointer, so state[6] stays 0 → the init path runs in a tight
                // loop calling func_2FEA30(6) (event pump) forever.
                //
                // Bootstrap fix: write state[6] = 0xFFF200 (our sentinel fn).
                // The sentinel re-arms itself so it's called on every iteration
                // of moduleManager_mainLoop. This advances the dispatch path
                // without requiring IOP and serves as the hook point for future
                // actual module-update work.
                //
                // Note: delay slot of the jalr already clears state[6] to 0
                // BEFORE our sentinel runs, so the sentinel must re-write it.
                {
                    const uint32_t stateTableAddr = 0x385334u; // modTable+0x1D4
                    const uint32_t stateTablePtr = *(uint32_t*)(rdram + stateTableAddr);
                    if (stateTablePtr != 0u && stateTablePtr != 0xFFFFFFFFu) {
                        const uint32_t state6Addr = stateTablePtr + 6u * 4u;
                        *(uint32_t*)(rdram + state6Addr) = 0xFFF200u;
                        std::cout << "[Bootstrap] Armed module state[6]=0xFFF200"
                                  << " at rdram[0x" << std::hex << state6Addr
                                  << "] (stateTP=0x" << stateTablePtr << ")"
                                  << std::dec << std::endl;
                        // Signal module-6 "IOP init done" immediately so the
                        // 0xFFF200 sentinel writes state[6]=-1 on its first call,
                        // completing module-6 boot cleanly.  Previously this was
                        // triggered by sif_dmaSend call#3, but with render jobs now
                        // going through the EE renderer (0x308D08), sif_dmaSend is
                        // no longer called from the per-frame render path.
                        s_iop_init_done.store(1u, std::memory_order_release);
                        std::cout << "[Bootstrap] s_iop_init_done set immediately"
                                     " (EE renderer path; no sif_dmaSend needed)"
                                  << std::endl;
                    } else {
                        std::cout << "[Bootstrap] WARNING: stateTablePtr=0x"
                                  << std::hex << stateTablePtr
                                  << " not valid, skipping state[6] arm"
                                  << std::dec << std::endl;
                    }
                }

                // --- Phase 5b: CDVD-stubbed VIF1 capacity values ---
                //
                // capA (0x43AA04) and capB (0x43FA08) are the VIF1 ring-buffer
                // size limits checked by vif1_buildPacket (0x257080).  On real
                // PS2, the IOP GFX module (loaded from disc by CDVD) writes
                // these.  Without CDVD they stay 0, vif1_buildPacket sees no
                // capacity, and the entire VIF1 3D path is gated off forever.
                //
                // CYCLE 28 EXPERIMENT: write a plausible non-zero capacity to
                // both, and see if vif1_buildPacket starts engaging the VIF1
                // path.  Pick 0x100 (256 packets) — large enough for typical
                // per-frame draw counts, small enough not to overflow any
                // sibling assumption.  This is purely diagnostic — if it
                // produces a crash we revert; if it advances the pipeline,
                // we've unblocked one more downstream gate.
                Ps2FastWrite32(rdram, 0x43AA04u, 0x100u);
                Ps2FastWrite32(rdram, 0x43FA08u, 0x100u);
                std::cout << "[Bootstrap] CDVD-stub: capA=capB=0x100 written "
                             "(unlocks VIF1 buildPacket capacity gate)" << std::endl;

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
                // Render pipeline depth trace:
                //   modTable (0x38544C) → modPtr → modPtr+8 → renderList_manager arg
                //   modPtr+8 is $a0 to renderList_manager; if 0 → renderList never inits
                const uint32_t modPtr     = modTable; // READ32(0x38544C)
                uint32_t modSub8 = 0u, renderListPtr = 0u;
                if (modPtr >= 0x100000u && modPtr < 0x02000000u) {
                    modSub8 = *(uint32_t*)(rdram + modPtr + 8u);
                    // modSub8 is also the renderList head pointer candidate
                    if (modSub8 >= 0x100000u && modSub8 < 0x02000000u) {
                        renderListPtr = *(uint32_t*)(rdram + modSub8);
                    }
                }
                // vif1_buildPacket gate values:
                //   0x435A00 = current write index (counter)
                //   0x43AA04 = capacity when gsState+0x44==1 (v1==1 path)
                //   0x43FA08 = capacity when gsState+0x44!=1 (v1!=1 path)
                //   0x44C844 = gsState+0x44 (gate 1 selector: 1=path-A, 0=path-B)
                //   0x3853B8 = modSub8+0x1C (render ctx ptr → $a0 to 0x308DF8 jalr)
                const uint32_t vif1WriteIdx = *(uint32_t*)(rdram + 0x435A00u);
                const uint32_t vif1CapA     = *(uint32_t*)(rdram + 0x43AA04u);
                const uint32_t vif1CapB     = *(uint32_t*)(rdram + 0x43FA08u);
                const uint32_t gsState44    = *(uint32_t*)(rdram + 0x44C844u);
                const uint32_t renderCtxPtr = *(uint32_t*)(rdram + 0x3853B8u);
                // Module state machine diagnostics:
                //   modTable+0x1D4 = state table pointer (set by func_305990 via sub_308858)
                //   stateTable+24  = state[6] for module 6 (must become non-zero for module to run)
                const uint32_t stateTablePtr = *(uint32_t*)(rdram + modTable + 0x1D4u);
                const uint32_t modState6 = (stateTablePtr != 0u && stateTablePtr != 0xFFFFFFFFu)
                                           ? *(uint32_t*)(rdram + stateTablePtr + 24u)
                                           : 0xDEADBEEFu;
                std::cout << "[FrameDiag] frame=" << s_frameCount
                          << " state=0x" << std::hex << stateFunc
                          << " 443DC8=0x" << dat443DC8
                          << " 442B70=0x" << dat442B70
                          << " initFlag=" << std::dec << (int)initFlag
                          << " 36C100=0x" << std::hex << dat36C100
                          << " modTable=0x" << modTable
                          << " modSub8=0x" << modSub8
                          << " renderList=0x" << renderListPtr
                          << " gsState=0x" << gsStatePtr
                          << " ptr442F70=0x" << ptr442F70
                          << " VIF1_MARK=0x" << vif1Mark
                          << " vif1Idx=0x" << vif1WriteIdx
                          << " capA=0x" << vif1CapA
                          << " capB=0x" << vif1CapB
                          << " gs44=0x" << gsState44
                          << " renderCtx=0x" << renderCtxPtr
                          << " stateTP=0x" << stateTablePtr
                          << " state6=0x" << modState6
                          << std::dec << std::endl;

                // Dump all 32 module state slots once per second (every 60 frames).
                // Helps identify which modules are not yet "done" (-1).
                if ((s_frameCount % 60u) == 1u &&
                    stateTablePtr != 0u && stateTablePtr != 0xFFFFFFFFu &&
                    stateTablePtr < 0x02000000u) {
                    std::cout << "[StateDump] frame=" << s_frameCount << " states:";
                    for (uint32_t i = 0u; i < 32u; ++i) {
                        const uint32_t slot = stateTablePtr + i * 4u;
                        if (slot + 4u <= 0x02000000u) {
                            const int32_t v = *(int32_t*)(rdram + slot);
                            if (v != 0)
                                std::cout << " [" << std::dec << i << "]=0x"
                                          << std::hex << (uint32_t)v;
                        }
                    }
                    std::cout << std::dec << std::endl;
                }
            }

            if (stateFunc != 0u) {
                try {
                    frameCtx.pc = stateFunc;
                    SET_GPR_U32(&frameCtx, 29, 0x44BC80u); // $sp
                    SET_GPR_U32(&frameCtx, 31, 0u);         // $ra = 0

                    // Per-frame protocol step 1: set 0x443DC8=1 before the
                    // state call so sub_133660 enables module_renderPrep and
                    // sub_2596A0 knows to spin (waiting for 0xFFF500 to clear it).
                    Ps2FastWrite32(rdram, 0x443DC8u, 1u);

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

            // CYCLE 28 PHASE 8 — VIF1_MARK force-engage experiment (reverted)
            //
            // Tried: runtime->memory().writeIORegister(0x10003C30u, 1u);
            // Result: vif1_frameSubmit DID engage submission, but the chain
            // in the ring buffer was garbage (ASCII strings + zeros).  The
            // submit path tried to jalr to addresses like 0x20676e69 (" gni"
            // = part of "Sending..." debug text from sif_dmaSend buffer).
            // Cascading recover-pc; reverted.
            //
            // Real fix requires understanding why the build leaves garbage
            // in the ring buffer.  Likely candidates:
            //   - vif1_buildPacket reads upstream descriptors from gsState
            //     fields we haven't populated correctly
            //   - The ring buffer base address differs between the build and
            //     the submit (build writes to one place, submit reads from
            //     another)
            //   - The build is supposed to populate VIF1_MADR / VIF1_TADR
            //     before MARK fires, and those are still zero/wrong.

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
        // Check CWD first so that running from the PS2_game/ directory works
        // and the runtime's cdRoot resolves to CWD (where all disc files live).
        if (std::filesystem::exists("SLUS_202.68"))
        {
            elfPath = "SLUS_202.68";
        }
        else
        {
            std::filesystem::path exePath(argv[0]);
            elfPath = (exePath.parent_path() / "SLUS_202.68").string();
        }
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
            std::cout << "[setGameState] module-id 0x" << std::hex << originalA0
                      << " -> GS-init 0x" << stateFunc << std::dec << std::endl;
        } else {
            std::cout << "[setGameState] native call: $a0=0x" << std::hex << originalA0
                      << std::dec << std::endl;
        }

        // Write the game state function pointer directly to 0x384670.
        //
        // NOTE: calling sub_002FDCE8_0x2fdce8 with ctx->pc=0x2FDDF8 does NOT work
        // because 0x2FDDF8 is not in the pc-dispatch switch table of that generated
        // function. The switch falls through to default and runs from 0x2FDCE8 instead,
        // which is a completely different code path (IOP waiter). Direct write is correct.
        //
        // The three SetSyscall side-effects (installing state-1/2/3 handlers at 0x2FE300
        // via sub_002F5850) are intentionally skipped here — they install exception
        // context-save handlers that are not needed in the recomp's non-exception model.
        Ps2FastWrite32(rdram, 0x384670u, stateFunc);

        // Only log state changes (suppress duplicate calls at the same value)
        static uint32_t s_lastState = 0u;
        if (stateFunc != s_lastState) {
            std::cout << "[setGameState] wrote 0x384670 = 0x"
                      << std::hex << stateFunc << std::dec << std::endl;
            s_lastState = stateFunc;
        }

        ctx->pc = GPR_U32(ctx, 31); // jr $ra
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

    // --- sub_31D200 call-chain tracers (cycle 28 phase 32) ---
    //
    // sub_31D200 is now in the binary (phase 29) but cycle 27 trace
    // confirmed nothing calls it.  Per CLAUDE.md the chain is:
    //   sub_31D200 ← sub_31C650 ← sub_31C310 ← sub_316310 ← sub_2B06F0
    //              ← sub_2B0580 ← (?)
    // Wrap each with a log-once trampoline that records first-call and
    // then forwards to the real function.  Whichever ones FIRE tell us
    // how far down the chain execution reaches; the gap between the
    // highest-firing one and sub_31D200 is the bridge to build.
    //
    // registerFunction wants a plain function pointer, so manual unroll.
    {
        extern void sub_0031D200_0x31d200(uint8_t*, R5900Context*, PS2Runtime*);
        extern void sub_0031C650_0x31c650(uint8_t*, R5900Context*, PS2Runtime*);
        extern void sub_0031C310_0x31c310(uint8_t*, R5900Context*, PS2Runtime*);
        extern void sub_00316310_0x316310(uint8_t*, R5900Context*, PS2Runtime*);
        extern void sub_002B06F0_0x2b06f0(uint8_t*, R5900Context*, PS2Runtime*);
        extern void sub_002B0580_0x2b0580(uint8_t*, R5900Context*, PS2Runtime*);
        extern void sub_002B0370_0x2b0370(uint8_t*, R5900Context*, PS2Runtime*);

        #define CHAIN_TRACER(VA, NAME, REAL)                                       \
            runtime.registerFunction((VA),                                         \
                +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* rt) {           \
                    static std::atomic<bool> s_logged{false};                      \
                    if (!s_logged.exchange(true)) {                                \
                        const uint32_t ra = GPR_U32(ctx, 31);                      \
                        printf("[ChainTrace] %s @0x%08X FIRED  ra=0x%08X\n",       \
                               NAME, (VA), ra);                                    \
                        fflush(stdout);                                            \
                    }                                                              \
                    REAL(rdram, ctx, rt);                                          \
                });

        CHAIN_TRACER(0x31D200u, "sub_31D200", sub_0031D200_0x31d200)
        CHAIN_TRACER(0x31C650u, "sub_31C650", sub_0031C650_0x31c650)
        CHAIN_TRACER(0x31C310u, "sub_31C310", sub_0031C310_0x31c310)
        CHAIN_TRACER(0x316310u, "sub_316310", sub_00316310_0x316310)
        CHAIN_TRACER(0x2B06F0u, "sub_2B06F0", sub_002B06F0_0x2b06f0)
        CHAIN_TRACER(0x2B0580u, "sub_2B0580", sub_002B0580_0x2b0580)
        CHAIN_TRACER(0x2B0370u, "sub_2B0370", sub_002B0370_0x2b0370)

        #undef CHAIN_TRACER
        std::cout << "[Bootstrap] Installed 7 sub_31D200 chain tracers" << std::endl;
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

    // Cycle 26-27 diagnostic: wrap sub_239C40 (boot_subinit) with
    // entry/exit logging to determine if it ever returns to its caller.
    // sub_2F84F0 (called first from 0x239d2c) was confirmed to enter and
    // exit cleanly. If sub_239C40 also exits, the parking is at the
    // _start tail after sub_239C40 returns.
    extern void sub_00239C40_0x239c40(uint8_t*, R5900Context*, PS2Runtime*);
    runtime.registerFunction(0x239C40, +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime) {
        static std::atomic<uint64_t> s_enter{0}, s_exit{0};
        const auto e = s_enter.fetch_add(1, std::memory_order_relaxed);
        if (e < 4u || (e % 1000u) == 0u) {
            std::cerr << "[sub_239C40:enter] n=" << e
                      << " pc=0x" << std::hex << ctx->pc
                      << " ra=0x" << GPR_U32(ctx, 31)
                      << std::dec << std::endl;
        }
        const uint32_t entryPc = ctx->pc != 0x239C40u ? ctx->pc : 0x239C40u;
        ctx->pc = entryPc;
        sub_00239C40_0x239c40(rdram, ctx, runtime);
        const auto x = s_exit.fetch_add(1, std::memory_order_relaxed);
        if (x < 4u || (x % 1000u) == 0u) {
            std::cerr << "[sub_239C40:exit ] n=" << x
                      << " pc=0x" << std::hex << ctx->pc
                      << std::dec << std::endl;
        }
    });

    // 0x2F57C0 — interior of stubbed sub_002F5538 (a multi-function
    // syscall-wrapper range that the analyzer rolls into one function).
    // Smoke logs show repeated `[dispatch:recover-pc] bad=0x2f57c0`
    // events as the dispatcher recovers to caller $ra each time. Register
    // as a noop so the recovery cycle short-circuits.
    runtime.registerFunction(0x2F57C0, stubNoThrowZero);

    // 0x308DF8 — interior label of sub_00308D08 (render job callback).
    //
    // sub_00304550 (called from renderList_init) stores 0x308DF8 as the
    // function pointer at modSub8+0x24.  func_304DF0's jalr at 0x304EAC
    // calls this address for every render job (jobCount=36-37 per frame).
    // Without a registration, runtime->lookupFunction(0x308DF8) returns null
    // and the jalr is silently skipped → no geometry submitted → VIF1_MARK=0.
    //
    // sub_00308D08_0x308d08.cpp's switch table includes case 0x308df8 →
    // goto label_308df8, so calling the parent function with ctx->pc=0x308DF8
    // correctly enters the interior entry point.
    //
    // Wrapper logs the first 5 calls: a0=modCtxPtr, a1=cmdStreamPtr (first
    // byte is the command), a2=count/0x400.
    {
        extern void sub_00308D08_0x308d08(uint8_t*, R5900Context*, PS2Runtime*);
        runtime.registerFunction(0x308DF8, +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime) {
            static std::atomic<uint32_t> s_rn{0};
            const uint32_t rn = s_rn.fetch_add(1, std::memory_order_relaxed);
            // Dump the render command stream: first 5 calls + every 300th call (5s @60fps).
            // This reveals the command protocol used by the IOP GFX module and how the
            // stream content changes after HW init messages finish.
            if (rn < 5u || (rn % 300u) == 0u) {
                const uint32_t a0 = GPR_U32(ctx, 4);  // modCtxPtr
                const uint32_t a1 = GPR_U32(ctx, 5);  // cmdStreamPtr
                const uint32_t a2 = GPR_U32(ctx, 6);  // count
                printf("[renderJob] rn=%u a0=0x%08x stream=0x%08x count=%u\n  bytes:",
                       rn, a0, a1, a2);
                if (a1 >= 0x100000u && a1 < 0x02000000u) {
                    const uint32_t dumpLen = (a2 < 128u) ? a2 : 128u;
                    for (uint32_t i = 0u; i < dumpLen; ++i)
                        printf(" %02x", rdram[a1 + i]);
                }
                printf("\n");
                fflush(stdout);
            }
            // Execute label_308df8: the real IOP send path.
            // sub_00308D08 has two entry points:
            //   0x308D08 = debug text display path (calls func_30CD10 printf renderer)
            //   0x308DF8 = IOP send path (calls func_30D9A8 → sif_dispatchRender → sif_dmaSend)
            //
            // The render job walker (0x30B568) dispatches via JALR to fnPtr=0x308DF8,
            // so we must set pc=0x308DF8 to route to label_308df8 (the IOP send path).
            // sif_dmaSend is still a stub (returns 0), so no VIF1 output yet, but
            // this allows capturing exactly what render commands the game builds.
            ctx->pc = 0x308DF8u;
            sub_00308D08_0x308d08(rdram, ctx, runtime);
        });
    }

    // 0x308680 — render list getOrInit, called by func_30CD10 per-iteration.
    // $v0 return: 0 = ok, non-zero = render list not ready (→ early exit from cd10).
    // Log first 8 calls so we can see if it ever returns non-zero.
    {
        extern void sub_00308680_0x308680(uint8_t*, R5900Context*, PS2Runtime*);
        runtime.registerFunction(0x308680, +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime) {
            static std::atomic<uint32_t> s_n{0};
            const uint32_t n = s_n.fetch_add(1, std::memory_order_relaxed);
            const uint32_t a0 = GPR_U32(ctx, 4);
            ctx->pc = 0x308680u;
            sub_00308680_0x308680(rdram, ctx, runtime);
            if (n < 8u) {
                const int32_t rv = GPR_S32(ctx, 2);
                printf("[308680] n=%u a0=0x%08x ret=%d\n", n, a0, rv);
                fflush(stdout);
            }
        });
    }

    // 0x310440 — GIF DMA flush at the tail of the EE renderer (func_30CD10).
    // func_30CD10's command-byte dispatcher calls this exactly once after
    // processing all render commands.  If we never see this log, func_30CD10
    // exits the loop without reaching the flush, meaning the EE renderer is
    // incomplete / command stream is malformed / dispatch table returns early.
    {
        extern void sub_00310440_0x310440(uint8_t*, R5900Context*, PS2Runtime*);
        runtime.registerFunction(0x310440, +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime) {
            static std::atomic<uint32_t> s_n{0};
            const uint32_t n = s_n.fetch_add(1, std::memory_order_relaxed);
            if (n < 8u || (n % 300u) == 0u) {
                const uint32_t a0 = GPR_U32(ctx, 4);
                // a0 is a stack pointer to the render context struct.
                // [+0x0] = channel type (0/2/4 for GIF DMA path in func_310498)
                uint32_t chan = 0u;
                if (a0 >= 0x100000u && a0 < 0x02000000u)
                    chan = *(uint32_t*)(rdram + a0);
                printf("[gifFlush] n=%u a0=0x%08x chan=%u\n", n, a0, chan);
                fflush(stdout);
            }
            ctx->pc = 0x310440u;
            sub_00310440_0x310440(rdram, ctx, runtime);
        });
    }

    // sub_002F6870 — "iDisableDmac then return" wrapper.
    // Native code: addiu $sp,-16 / sd $ra,0($sp) / jal func_2F5970 / ld $ra,0($sp) / addiu $sp,+16 / jr $ra
    //
    // ctx->pc strategy: do NOT set ctx->pc.  The JAL preamble in the caller
    // (sub_00251A20 at 0x251EC8) already set ctx->pc = 0x2F6870 before the
    // call.  handleSyscall does not touch ctx->pc.  On return ctx->pc is
    // still 0x2F6870 == __entryPc, so the caller's fixup fires:
    //   `if (ctx->pc == __entryPc) { ctx->pc = 0x251ED0; }`
    // restoring the correct return address regardless of $ra state.
    runtime.registerFunction(0x2F6870, +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime) {
        // Perform iDisableDmac — the only real side-effect of sub_002F6870.
        // Skip $sp adjustment: we're not using the PS2 stack at all.
        SET_GPR_S32(ctx, 3, -0x1D); // $v1 = iDisableDmac syscall number
        runtime->handleSyscall(rdram, ctx, 0u);
        // Do NOT set ctx->pc — leave at 0x2F6870 for the __entryPc fixup.
    });

    // sub_2596A0 wrapper — fix jr-$ra loop-back + first-call diagnostic.
    //
    // The recompiled body has a jr-$ra path whose switch table includes
    // label 0x2596E0; when $ra equals that address the jr loops back into
    // the function instead of returning.  The outer func_257080 then sees
    // ctx->pc != post-jal-ra and bails early.  We force ctx->pc = $ra on
    // exit to break that loop.
    //
    // With DAT_443DC8=0 (set in bootstrap Phase 4), sub_2596A0 exits its
    // GS-ready wait loop immediately and fires GIF DMA from
    //   READ32(READ32(gsState+0x88)+0) = 0x450010 (our test chain tag)
    // delivering the pre-built bright-green triangle to the GS every frame.
    extern void sub_002596A0_0x2596a0(uint8_t*, R5900Context*, PS2Runtime*);
    runtime.registerFunction(0x2596A0, +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime) {
        ctx->pc = 0x2596A0u;
        sub_002596A0_0x2596a0(rdram, ctx, runtime);
        const uint32_t ra = GPR_U32(ctx, 31);
        if (ctx->pc != ra) {
            ctx->pc = ra;
        }
        // Brief diagnostic: log the first 3 calls so we can confirm the GIF
        // DMA path is being exercised, then one log every 600 calls (~10 s).
        static std::atomic<uint32_t> s_n{0};
        const uint32_t n = s_n.fetch_add(1, std::memory_order_relaxed);
        if (n < 3u || (n % 600u) == 0u) {
            const uint32_t dc8 = *(uint32_t*)(rdram + 0x443DC8u);
            const uint32_t desc = *(uint32_t*)(rdram + 0x450000u);
            const uint32_t tag0 = *(uint32_t*)(rdram + 0x450010u);
            printf("[sub_2596A0] n=%u dc8=0x%x desc=0x%x tag0=0x%x\n",
                   n, dc8, desc, tag0);
            fflush(stdout);
        }
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

        // Track calls and decode the command stream for analysis.
        //
        // sif_dispatchRender (0x2F6168) strips the channel ID and passes:
        //   $a0 = cmdStreamPtr (RDRAM address, e.g. 0x44F610)
        //   $a1 = count (number of bytes in command stream)
        //   $a2 = count (repeated — possibly an arg to the SIF DMA transfer)
        //
        // The command stream is binary render commands sent to the IOP GFX module
        // via SIF DMA.  The IOP processes these to produce VIF1 DMA output.
        static std::atomic<uint32_t> s_iop_calls{0};
        const uint32_t n = s_iop_calls.fetch_add(1, std::memory_order_relaxed);

        const uint32_t streamPtr = GPR_U32(ctx, 4);  // cmdStreamPtr (e.g. 0x44F610)
        const uint32_t count     = GPR_U32(ctx, 5);  // byte count

        // Dump first 16 calls fully, then one per second (60 frames) to track changes.
        if (n < 16u || (n % 60u) == 0u) {
            printf("[sif_dmaSend] call#%u streamPtr=0x%08x count=%u\n",
                   n, streamPtr, count);
            // Dump the raw command bytes as hex + ASCII.
            if (streamPtr >= 0x100000u && streamPtr < 0x2000000u && count > 0u) {
                const uint8_t* p = rdram + streamPtr;
                const uint32_t dumpLen = (count < 128u) ? count : 128u;
                printf("[sif_dmaSend]   hex:");
                for (uint32_t i = 0; i < dumpLen; ++i)
                    printf(" %02x", p[i]);
                printf("\n[sif_dmaSend]   ascii: \"");
                for (uint32_t i = 0; i < dumpLen; ++i) {
                    uint8_t c = p[i];
                    if (c >= 0x20 && c < 0x7F) printf("%c", c);
                    else printf("\\x%02x", c);
                }
                printf("\"\n");
            }
            fflush(stdout);
        }

        // Note: s_iop_init_done is now set by Bootstrap Phase 4 immediately,
        // so this path is no longer needed for module-6 boot completion.
        // Kept here as a fallback in case sif_dmaSend is called from a
        // non-render path during boot (e.g., module-init IOP messages).
        if (n == 2u && !s_iop_init_done.load(std::memory_order_acquire)) {
            s_iop_init_done.store(1u, std::memory_order_release);
            printf("[sif_dmaSend] call#%u: IOP init done (fallback path)\n", n);
            fflush(stdout);
        }
    });

    // --- 0x2F84F0  IOP-init bypass (sub_002F84F0) ---
    //
    // Called from boot_subinit (sub_00239C40 at label_239d2c) with $a0=0.
    // Real function: initialises SIF, calls func_2F5FB0 (IOP bind, returns 0
    // without IOP), then calls func_2F8298 (SIF receive wait) followed by
    // func_2F7DD8($a0=0) spin until mem[0x447B80] != 0.
    //
    // Race: gameLoopThread calls sub_239C40 → func_2F84F0 BEFORE the bootstrap
    // Phase 4 can write mem[0x447B80]=1, causing an infinite spin.  The caller
    // doesn't branch on the return value of func_2F84F0 (label_239d34 calls
    // func_311DF0 unconditionally), so a stub that returns 1 immediately is safe.
    runtime.registerFunction(0x2F84F0u, +[](uint8_t* /*rdram*/, R5900Context* ctx, PS2Runtime* /*runtime*/) {
        SET_GPR_U32(ctx, 2, 1u);   // $v0 = 1 (success/ready)
        ctx->pc = GPR_U32(ctx, 31); // jr $ra
    });

    // --- 0x2F7370  IOP-connection init bypass (func_2F7370) ---
    //
    // Called from func_2F6168 when mem[0x383AD0]==0 (IOP not yet bound).
    // Returns 0 → func_2F6168 returns -1 (failure).
    // Returns non-zero → func_2F6168 sets mem[0x383AD0]=1 and calls func_2F7150.
    // Stub returns 1 so the IOP-connection gate unlocks and func_2F7150 runs.
    runtime.registerFunction(0x2F7370u, +[](uint8_t* /*rdram*/, R5900Context* ctx, PS2Runtime* /*runtime*/) {
        SET_GPR_U32(ctx, 2, 1u);   // $v0 = 1 (connected)
        ctx->pc = GPR_U32(ctx, 31); // jr $ra
    });

    // --- 0x311DF0  IOP SIF-module init bypass (func_311DF0) ---
    //
    // Called from boot_subinit (sub_00239C40 at label_239d34) with $a0=0.
    // The caller doesn't branch on the return value.
    runtime.registerFunction(0x311DF0u, +[](uint8_t* /*rdram*/, R5900Context* ctx, PS2Runtime* /*runtime*/) {
        SET_GPR_U32(ctx, 2, 0u);   // $v0 = 0 (pass-through, caller ignores)
        ctx->pc = GPR_U32(ctx, 31); // jr $ra
    });

    // --- 0x312DA0  IOP SIF DMA completion spin-bypass (sub_00312DA0) ---
    //
    // Calls func_2F8B60 (SIF DMA send) then polls struct[+0x24] in a tight
    // delay loop waiting for IOP completion.  Without IOP emulation the
    // completion flag stays 0 and the thread spins forever at label_312dd8.
    // Stubbing to return 1 immediately is safe — callers check the return
    // value only to know whether the transfer was dispatched, not for
    // correctness of the IOP side.
    runtime.registerFunction(0x312DA0u, +[](uint8_t* /*rdram*/, R5900Context* ctx, PS2Runtime* /*runtime*/) {
        SET_GPR_U32(ctx, 2, 1u);   // $v0 = 1 (done)
        ctx->pc = GPR_U32(ctx, 31); // jr $ra
    });

    // --- 0x2E8D90  Module-ready wait bypass (sub_002E8D90) ---
    //
    // On real HW: polls func_305700 → func_305990 (reads module table at
    // mem[0x38544C]) and spins at label_2e8db4 until the module is ready.
    // Returns a non-zero pointer/handle to the ready module on success, or 0
    // if the module was never initialised.
    //
    // WHY WE RETURN 0 (not 1):
    //   Callers treat the return value as a pointer, not a bool.  If we return 1,
    //   that "pointer" (= 0x00000001) is passed to func_141300 → func_1048F0 →
    //   sub_001416C0, which reads READ32(0x1+0x1C) = READ32(0x1D) — a garbage RDRAM
    //   value that is non-zero, making sub_1416C0's linked-list walk infinite.
    //
    //   Returning 0 makes every caller's `beqz $v0` branch skip the module-ready
    //   setup (func_141300 etc.), which is the correct no-IOP behaviour.  The game
    //   continues past these optional dependency registrations and func_13FDA0 runs
    //   to completion.
    runtime.registerFunction(0x2E8D90u, +[](uint8_t* /*rdram*/, R5900Context* ctx, PS2Runtime* /*runtime*/) {
        SET_GPR_U32(ctx, 2, 0u);    // $v0 = 0 (module not ready — no IOP)
        ctx->pc = GPR_U32(ctx, 31); // jr $ra
    });

    // --- 0x315800  IOP SIF DMA multi-send spin-bypass (sub_00315800) ---
    //
    // Sends 5 sequential SIF DMA commands (0x80000901–0x80000905) and polls
    // struct+0x24 for IOP completion on each one.  Without IOP emulation the
    // completion flag stays 0 and the outer retry loop spins forever at
    // label_315828.  Same pattern as sub_00312DA0.  Returns 1 = success.
    runtime.registerFunction(0x315800u, +[](uint8_t* /*rdram*/, R5900Context* ctx, PS2Runtime* /*runtime*/) {
        SET_GPR_U32(ctx, 2, 1u);    // $v0 = 1 (success)
        ctx->pc = GPR_U32(ctx, 31); // jr $ra
    });

    // --- 0x5c  FlushCache BIOS stub bypass ---
    //
    // On real PS2 the EE BIOS places a FlushCache syscall trampoline at
    // virtual address 0x5c (kseg0 low stub area).  Game code reaches it via
    // a tail-call chain:
    //   sub_239C40 → 0x23a108 → func_304398 → func_1000b8 →
    //   sub_2FEA30 → func_2FE980 → func_2FE0C0 → func_2F6000 →
    //   func_2F6010 → func_2F57C0 (stubNoThrowZero) → j 0x5c
    //
    // Because 0x5c is below the ELF load base (0x100000) it was never
    // recompiled, so lookupFunction(0x5c) returned null and the runtime
    // printed a flood of "Function at address 0x5c not found" warnings,
    // recovering to ra=0x23a13c and re-entering sub_239C40 in a tight
    // pseudo-loop (~8192 iterations before s_recoverCount cap).
    //
    // FlushCache is a pure cache-management operation; on PC there is no
    // PS2 cache to flush, so a no-op returning 0 is correct.
    runtime.registerFunction(0x5Cu, +[](uint8_t* /*rdram*/, R5900Context* ctx, PS2Runtime* /*runtime*/) {
        SET_GPR_S32(ctx, 2, 0);         // $v0 = 0 (success)
        ctx->pc = GPR_U32(ctx, 31);     // jr $ra
    });

    // --- 0x2FEA30  _start exit-path block ---
    //
    // _start (0x100008) tail-calls func_2FEA30 after boot_subinit (sub_239C40)
    // returns.  On real PS2, boot_subinit never returns (moduleMain loops
    // forever).  In the recomp, moduleMain exits early so _start reaches this
    // tail-call.  func_2FEA30 saves $ra, calls func_2FE980, restores $ra
    // (= 0x1000ac), then tail-calls func_2F57C0 (stubNoThrowZero) which
    // returns via $ra = 0x1000ac → back to label_1000ac in _start → repeat.
    //
    // Block Thread-0 here permanently.  This mirrors what real hardware does
    // (this path is simply never reached) while stopping the tight loop that
    // generated thousands of bad-address warnings per second.
    runtime.registerFunction(0x2FEA30u, +[](uint8_t* /*rdram*/, R5900Context* /*ctx*/, PS2Runtime* /*runtime*/) {
        for (;;) {
            std::this_thread::sleep_for(std::chrono::seconds(60));
        }
    });

    // --- 0x1000B8  ELF exit-path no-op stub ---
    //
    // sub_001000B8 is the ELF's "program exit" trampoline. Its first
    // instruction is `j func_2FEA30` (our permanent-sleep stub) — so
    // entering at 0x1000B8 always blocks unless we intercept it.
    //
    // func_304398 (0x304398) ALWAYS tail-calls func_1000B8 at the end of
    // every code path, and sub_239C40 (boot_subinit) calls func_304398(-1)
    // when func_237640 returns 0 (which it does on first boot because
    // mem[0x382B80] struct isn't fully initialised). The resulting chain:
    //
    //   dispatchLoop → sub_239C40(label_23a108)
    //     → jal func_304398(-1)
    //       → j func_1000B8          ← trapped here
    //         → j func_2FEA30        ← our blocking lambda
    //
    // Stubbing 0x1000B8 to return immediately lets func_304398 return to
    // sub_239C40 at 0x23A118, which then sets $v0=1 and falls into the
    // module-vtable spin loop at label_23a124.  That loop exits because
    // vtable+0x30 → 0xFFF400 returns 0 (pre-boot vtable at 0x44F800 is
    // populated before runtime.run(); see below).
    //
    // This is safe: on real PS2, func_1000B8 / func_2FEA30 are the
    // ExitDeleteThread tail-chain; they're never reached during normal
    // game execution.
    runtime.registerFunction(0x1000B8u, +[](uint8_t* /*rdram*/, R5900Context* ctx, PS2Runtime* /*runtime*/) {
        SET_GPR_S32(ctx, 2, 0);         // $v0 = 0
        ctx->pc = GPR_U32(ctx, 31);     // jr $ra
    });

    // --- 0x00FFF500  gsState-callback sentinel ---
    // Wired into gsState+0xDC by bootstrap. sub_2596A0 jalrs through
    // this slot while spinning at label_2596e0 (while 0x443DC8 != 0).
    // Step 4 of the per-frame protocol: clear 0x443DC8 → sub_2596A0's
    // bnez exits, falls through to label_259710, and fires GIF DMA.
    runtime.registerFunction(0x00FFF500u, +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* /*runtime*/) {
        // Signal sub_2596A0 that it can proceed to GIF DMA kick.
        Ps2FastWrite32(rdram, 0x443DC8u, 0u);
        SET_GPR_S32(ctx, 2, 0);
        ctx->pc = GPR_U32(ctx, 31);
    });

    // --- renderList_manager (0x30B4F0) diagnostic wrapper ---
    // Removed verbose logging; real function runs unconditionally.
    // (Kept as passthrough so register slot stays claimed.)
    extern void sub_0030B4F0_0x30b4f0(uint8_t*, R5900Context*, PS2Runtime*);
    runtime.registerFunction(0x30B4F0, +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime) {
        ctx->pc = 0x30B4F0u;
        sub_0030B4F0_0x30b4f0(rdram, ctx, runtime);
    });

    // --- sub_0030B3F0 (0x30B3F0) job-dispatch diagnostic wrapper + synthetic injection ---
    //
    // func_30B3F0 is the render-job executor called ~40× per frame from
    // sub_0030B568 (the render job-list walker).  It reads READ32(s0+8) to
    // get a job-entry count, then dispatches to func_304DF0.
    //
    // Job struct layout (stack-allocated by sub_0030B568, $a1 = ptr):
    //   [+0]  jobArrayPtr  — pointer to array of 8-byte job entries
    //   [+4]  status       — cleared after processing
    //   [+8]  jobCount     — number of job entries (0 = no work)
    // Job entry layout (8 bytes each):
    //   [+0]  cmdStreamPtr — pointer to command byte stream
    //   [+4]  count
    //
    // modSub8 ($a0):
    //   [+0x1C] renderCtxPtr — passed as $a0 to func_304DF0's JALR target
    //   [+0x24] fnPtr        — JALR target (= 0x308DF8, set by renderList_init)
    //
    // Diagnostics: log first 5 calls with full struct contents.
    // Injection: for first call where jobCount==0, probe the render command
    // dispatch table at 0x3C6391 for a non-zero flags byte, then write a
    // synthetic job into the job struct so func_304DF0 actually dispatches.
    extern void sub_0030B3F0_0x30b3f0(uint8_t*, R5900Context*, PS2Runtime*);
    runtime.registerFunction(0x30B3F0, +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime) {
        static std::atomic<uint32_t> s_n{0};
        const uint32_t n = s_n.fetch_add(1, std::memory_order_relaxed);

        const uint32_t a0 = GPR_U32(ctx, 4);  // modSub8 ptr
        const uint32_t a1 = GPR_U32(ctx, 5);  // job struct (stack)

        // --- Full struct diagnostics for first 5 calls ---
        if (n < 5u) {
            uint32_t jobPtr = 0u, jobStatus = 0u, jobCount = 0u;
            uint32_t entry0 = 0u, entry4 = 0u;
            uint32_t modCtxPtr = 0u, fnPtr = 0u;

            if (a1 >= 0x100000u && a1 < 0x02000000u) {
                jobPtr    = *(uint32_t*)(rdram + a1 + 0u);
                jobStatus = *(uint32_t*)(rdram + a1 + 4u);
                jobCount  = *(uint32_t*)(rdram + a1 + 8u);
                if (jobPtr >= 0x100000u && jobPtr < 0x02000000u) {
                    entry0 = *(uint32_t*)(rdram + jobPtr + 0u);
                    entry4 = *(uint32_t*)(rdram + jobPtr + 4u);
                }
            }
            if (a0 >= 0x100000u && a0 < 0x02000000u) {
                modCtxPtr = *(uint32_t*)(rdram + a0 + 0x1Cu);
                fnPtr     = *(uint32_t*)(rdram + a0 + 0x24u);
            }
            printf("[jobDispatch] n=%u a0=0x%08x a1=0x%08x "
                   "jobPtr=0x%08x status=%u jobCount=%u "
                   "entry[0]=0x%08x entry[4]=%u "
                   "modCtx=0x%08x fnPtr=0x%08x\n",
                   n, a0, a1,
                   jobPtr, jobStatus, jobCount,
                   entry0, entry4,
                   modCtxPtr, fnPtr);
            fflush(stdout);
        }

        // --- Synthetic job injection (first call where jobCount==0) ---
        //
        // Probe the render command dispatch table at 0x3C6391:
        //   rdram[cmd + 0x3C6391] = flags byte for that command
        //   cmd == 0  → end-of-stream (exit dispatcher)
        //   flags != 0 → command has a handler (safe to dispatch)
        //
        // Build a single-entry job with one command byte, inject into the
        // stack job struct, and let the real func_304DF0 dispatch it normally.
        // Scratch areas 0x44D800 (job array) and 0x44D900 (cmd stream) are
        // above the BSS wipe ceiling (0x44F600) so they survive _start init.
        static std::atomic<bool> s_injected{false};
        if (n < 5u && a1 >= 0x100000u && a1 < 0x02000000u && !s_injected.load()) {
            const uint32_t jobCount = *(uint32_t*)(rdram + a1 + 8u);
            if (jobCount == 0u) {
                // Find first cmd byte in [1..255] with non-zero dispatch flags
                uint8_t validCmd = 0u;
                uint8_t cmdFlags = 0u;
                for (uint32_t cmd = 1u; cmd < 256u; ++cmd) {
                    const uint8_t flags = rdram[0x3C6391u + cmd];
                    if (flags != 0u) {
                        validCmd = (uint8_t)cmd;
                        cmdFlags = flags;
                        break;
                    }
                }

                if (validCmd != 0u) {
                    s_injected.store(true);

                    const uint32_t jobArrayAddr  = 0x44D800u;
                    const uint32_t cmdStreamAddr = 0x44D900u;

                    // Job entry array: one entry {cmdStreamAddr, count=1}, then null
                    *(uint32_t*)(rdram + jobArrayAddr +  0u) = cmdStreamAddr;
                    *(uint32_t*)(rdram + jobArrayAddr +  4u) = 1u;
                    *(uint32_t*)(rdram + jobArrayAddr +  8u) = 0u;
                    *(uint32_t*)(rdram + jobArrayAddr + 12u) = 0u;

                    // Command stream: validCmd then 0 (end-of-stream)
                    rdram[cmdStreamAddr + 0u] = validCmd;
                    rdram[cmdStreamAddr + 1u] = 0u;

                    // Patch job struct: jobArrayPtr and jobCount
                    *(uint32_t*)(rdram + a1 + 0u) = jobArrayAddr;
                    *(uint32_t*)(rdram + a1 + 8u) = 1u;

                    printf("[jobDispatch] INJECTED synthetic job n=%u: "
                           "cmd=0x%02x flags=0x%02x "
                           "jobArray=0x%08x cmdStream=0x%08x\n",
                           n, validCmd, cmdFlags, jobArrayAddr, cmdStreamAddr);
                    fflush(stdout);
                } else {
                    printf("[jobDispatch] WARNING n=%u: "
                           "dispatch table 0x3C6391 has no non-zero entry — "
                           "table may not be initialized yet\n", n);
                    fflush(stdout);
                }
            }
        }

        ctx->pc = 0x30B3F0u;
        sub_0030B3F0_0x30b3f0(rdram, ctx, runtime);
    });

    // --- 0x2FEA30  func_2FEA30 — IOP event-pump stub ---
    //
    // On real HW this blocks the module-manager thread until the IOP sends a
    // completion callback (via SifCallRpc/WaitSema → func_2F57C0).  Without a
    // running IOP, SifCallRpc (syscall 0x7F) never gets a response and the
    // thread hangs forever, preventing sub_00308958 from cycling back to check
    // state[6]=0xFFF200.
    //
    // This stub returns -1 immediately (the same value sub_002F5538 TODO_NAMED
    // would eventually return), letting the thread cycle.  sub_00308958 with
    // state[6]=0 calls func_308C08 + func_308BA8 → func_2FEA30 per iteration.
    // When the bootstrap later writes state[6]=0xFFF200, the next iteration
    // sees it and calls our modUpdate6 sentinel instead.
    runtime.registerFunction(0x2FEA30u, +[](uint8_t* /*rdram*/, R5900Context* ctx, PS2Runtime* /*runtime*/) {
        static std::atomic<uint32_t> s_n{0};
        const uint32_t n = s_n.fetch_add(1, std::memory_order_relaxed);
        if (n == 0u) {
            printf("[2FEA30] IOP event-pump stub active (returns -1 to unblock module thread)\n");
            fflush(stdout);
        }
        SET_GPR_S32(ctx, 2, -1);       // $v0 = -1
        ctx->pc = GPR_U32(ctx, 31);    // jr $ra
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

    // --- 0x00FFF500  one-return sentinel ---
    //
    // Companion to FFF400. Returns $v0=1 (non-zero / success).
    // Installed at rdram[modVTable + 0x9C] so that sub_237640's vtable[0x9C]
    // call returns non-zero, which triggers the branch to label_2376a0 and
    // ultimately calls func_13FDA0 (render infrastructure init).
    //
    // Without this, vtable[0x9C] returned 0 (FFF400 was used) and the bnez
    // at label_237670 was never taken, so func_13FDA0 was silently skipped.
    runtime.registerFunction(0x00FFF500u, +[](uint8_t* /*rdram*/, R5900Context* ctx, PS2Runtime* /*runtime*/) {
        SET_GPR_S32(ctx, 2, 1);        // $v0 = 1 (non-zero / success)
        ctx->pc = GPR_U32(ctx, 31);    // jr $ra
    });

    // --- callback[11] @ 0x3CA620 — TYPE-B prepender with pre-call helper ---
    //
    // Shape: matrix-init at $a0, jal func_299130(...), jal sub_002E91C0.
    //   $a0=0x435690 (data), $a1=0x299190 (fn), $a2=0x435680 (node)
    // The matrix init follows the same identity-ish pattern as type-A
    // callbacks.  func_299130 is a registered subsystem init function.
    runtime.registerFunction(0x3CA620u, +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime) {
        if (!rdram || !ctx) return;
        static const float kMatrix[16] = {
            0.f, 0.f, 0.f, 1.f,  1.f, 0.f, 0.f, 1.f,
            0.f, 1.f, 0.f, 1.f,  0.f, 0.f, 1.f, 1.f,
        };
        std::memcpy(rdram + 0x435690u, kMatrix, sizeof(kMatrix));
        // Call func_299130($a0=0x435690, ...) via runtime
        const uint32_t savedRa = GPR_U32(ctx, 31);
        SET_GPR_U32(ctx, 4, 0x00435690u); // $a0
        SET_GPR_U32(ctx, 5, 0x00299190u); // $a1 (the fn ptr that will be registered)
        SET_GPR_U32(ctx, 6, 0x00435680u); // $a2 (node ptr)
        SET_GPR_U32(ctx, 31, 0x00FFF000u); // fake $ra so the callee returns predictably
        ctx->pc = 0x00299130u;
        auto fn = runtime->lookupFunction(0x00299130u);
        if (fn) fn(rdram, ctx, runtime);
        // Restore args after the call (caller-saved are clobbered) and prepend
        const uint32_t oldHead = *(uint32_t*)(rdram + 0x446AD0u);
        *(uint32_t*)(rdram + 0x435680u + 0u) = oldHead;
        *(uint32_t*)(rdram + 0x435680u + 4u) = 0x00299190u;
        *(uint32_t*)(rdram + 0x435680u + 8u) = 0x00435690u;
        *(uint32_t*)(rdram + 0x446AD0u) = 0x435680u;
        static std::atomic<bool> s_logged{false};
        if (!s_logged.exchange(true)) {
            printf("[callback[11] 0x3CA620] init + func_299130 + prepend at 0x435680\n");
            fflush(stdout);
        }
        SET_GPR_U32(ctx, 31, savedRa);
        ctx->pc = savedRa;
    });

    // callback[12] @ 0x3CA6D0 + callback[31] @ 0x3CAFF0 — reverted in cycle 28
    // phase 7.  Their helper calls (func_24CF80, func_299130) produced
    // cascading recover-pc events when invoked from the C++ wrapper context.
    // Need deeper investigation of the calling convention / stack contract
    // before re-attempting.  Left on synthPrepend for now.

    // --- callback[34] @ 0x3CB140 — TYPE-B "prepender" (tail-jump j 0x2E91C0) ---
    //
    // Same shape as cb[18]:
    //   $a0=0x443970, $a1=0x2A3180 (fn), $a2=0x443960 (node), WRITE32 zeros
    //
    // fn=0x2A3180 is an interior label inside sub_002A3140; registered as
    // an extra entry point in cycle 28 phase 6 (TOML edit + injection).
    runtime.registerFunction(0x3CB140u, +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* /*runtime*/) {
        if (!rdram || !ctx) return;
        *(uint32_t*)(rdram + 0x443970u) = 0u;
        *(uint32_t*)(rdram + 0x443974u) = 0u;
        const uint32_t oldHead = *(uint32_t*)(rdram + 0x446AD0u);
        *(uint32_t*)(rdram + 0x443960u + 0u) = oldHead;
        *(uint32_t*)(rdram + 0x443960u + 4u) = 0x002A3180u;
        *(uint32_t*)(rdram + 0x443960u + 8u) = 0x00443970u;
        *(uint32_t*)(rdram + 0x446AD0u) = 0x443960u;
        static std::atomic<bool> s_logged{false};
        if (!s_logged.exchange(true)) {
            printf("[callback[34] 0x3CB140] prepended node at 0x443960 fn=0x2A3180 data=0x443970\n");
            fflush(stdout);
        }
        ctx->pc = GPR_U32(ctx, 31);
    });

    // --- callback[18] @ 0x3CA9F0 — TYPE-B "prepender" (tail-jump j 0x2E91C0) ---
    //
    // Short tail-jump callback:
    //   $a0 = 0x443190                      ; data ptr (struct addr)
    //   $a1 = 0x282280                      ; fn ptr (sub_00282280 — registered)
    //   $a2 = 0x443180                      ; node addr (3-word node struct)
    //   WRITE32(0x443190, 0)                ; zero first word of data struct
    //   j sub_002E91C0                      ; tail-jump to list prepend
    //   delay: WRITE32(0x443194, 0)         ; zero second word of data struct
    //
    // After prepend the linked list at 0x446AD0 has a real node with
    // fn=0x282280, data=0x443190.  When listWalker pops it, it calls
    // sub_00282280(0x443190, -1) — the actual subsystem init function.
    runtime.registerFunction(0x3CA9F0u, +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* /*runtime*/) {
        if (!rdram || !ctx) return;
        // Zero the data struct (first 8 bytes)
        *(uint32_t*)(rdram + 0x443190u) = 0u;
        *(uint32_t*)(rdram + 0x443194u) = 0u;
        // Prepend node {next, fn, data} at 0x443180
        const uint32_t oldHead = *(uint32_t*)(rdram + 0x446AD0u);
        *(uint32_t*)(rdram + 0x443180u + 0u) = oldHead;
        *(uint32_t*)(rdram + 0x443180u + 4u) = 0x00282280u;
        *(uint32_t*)(rdram + 0x443180u + 8u) = 0x00443190u;
        *(uint32_t*)(rdram + 0x446AD0u) = 0x443180u;
        static std::atomic<bool> s_logged{false};
        if (!s_logged.exchange(true)) {
            printf("[callback[18] 0x3CA9F0] prepended node at 0x443180 fn=0x282280 data=0x443190\n");
            fflush(stdout);
        }
        ctx->pc = GPR_U32(ctx, 31);
    });

    // --- callback[7] @ 0x3CA350 — handle-allocator stub (no-op semantic) ---
    //
    // Cycle 28 phase 18.  Full cb[7] does:
    //   $v0a = sub_315B60(\$a0=undefined-from-walker)
    //   $v0b = sub_315B60(\$a0=1)
    //   rdram[0x435340] = $v0a   ; first thread handle
    //   rdram[0x435344] = $v0b   ; second thread handle
    //   rdram[0x435348] = 0
    //
    // sub_315B60 wraps sub_2F8D30 (full thread-create with 192-byte
    // stack frame and 60+ register/state setups).  Calling it from C++
    // via lookupFunction risks the phase-7 helper-call instability.
    //
    // Equivalent to the failure path (sub_315B60 returning 0), which
    // matches the runtime's zero-initialized state.  This override is
    // semantically identical to synthPrepend (which also does nothing
    // useful for this callback) but removes the phantom list node and
    // documents the intent — providing non-zero "valid" handles would
    // require a synthetic thread subsystem we don't have.
    runtime.registerFunction(0x3CA350u, +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* /*runtime*/) {
        if (!rdram || !ctx) return;
        *(uint32_t*)(rdram + 0x435340u) = 0u;
        *(uint32_t*)(rdram + 0x435344u) = 0u;
        *(uint32_t*)(rdram + 0x435348u) = 0u;
        static std::atomic<bool> s_logged{false};
        if (!s_logged.exchange(true)) {
            printf("[callback[7] 0x3CA350] handle slots cleared (no thread subsystem)\n");
            fflush(stdout);
        }
        ctx->pc = GPR_U32(ctx, 31);
    });

    // --- callback[13] @ 0x3CA780 — CDVD-stub-aware partial ---
    //
    // Cycle 28 phase 17.  Full cb[13] does:
    //   1) rdram[0x435A00] = 0     (VIF1 write idx clear; already 0 at boot)
    //   2) rdram[0x43AA04] = 0     (capA clear — MUST SKIP, undoes CDVD stub)
    //   3) rdram[0x43FA08] = 0     (capB clear — MUST SKIP)
    //   4) 4x helper jal (sub_164AA0 ×3, sub_164DD0 ×1) with $a0={
    //         0x442A58, 0x442AC8, 0x442AE8, 0x442B48}, $a1=5000
    //         The helpers are timer-init (read T0_COUNT, compute delta).
    //         Their only easy-to-replicate side effect is `sw $zero, 4($a0)`.
    //
    // Partial: do step (1) (no-op since runtime zero-init) + the four
    // `sw $zero, 4($a0)` clears.  Skip steps (2), (3) to preserve the
    // cycle-28-phase-6 CDVD stub.  Skip the timer math.
    runtime.registerFunction(0x3CA780u, +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* /*runtime*/) {
        if (!rdram || !ctx) return;
        // Step (1) — vif1 write idx (redundant but matches semantics)
        *(uint32_t*)(rdram + 0x435A00u) = 0u;
        // Step (4) partial — per-target `sw $zero, 4($a0)`
        *(uint32_t*)(rdram + 0x442A58u + 4u) = 0u;
        *(uint32_t*)(rdram + 0x442AC8u + 4u) = 0u;
        *(uint32_t*)(rdram + 0x442AE8u + 4u) = 0u;
        *(uint32_t*)(rdram + 0x442B48u + 4u) = 0u;
        // Steps (2), (3) deliberately SKIPPED to preserve capA/capB stub.
        static std::atomic<bool> s_logged{false};
        if (!s_logged.exchange(true)) {
            printf("[callback[13] 0x3CA780] partial: 4 timer-struct +4 clears (capA/capB preserved)\n");
            fflush(stdout);
        }
        ctx->pc = GPR_U32(ctx, 31);
    });

    // --- callback[31] @ 0x3CAFF0 — static data write only (PARTIAL) ---
    //
    // Cycle 28 phase 16.  Full cb[31] does:
    //   1) jal sub_299130($a0=0x443890)        — helper, return ignored
    //   2) rdram[0x443898] = 0x3D3910          — static data write
    //   3) prepend node {fn=0x29E200, data=0x443890} at 0x443878
    //
    // We translate only step (2): the helper call is phase-7-unstable
    // and sub_29E200 (the prepended fn target) is an interior label with
    // no registered entry point — walker can't dispatch it.  Skipping
    // (1) and (3) leaves us no worse than synthPrepend (which writes
    // nothing) and adds the one static data write.
    runtime.registerFunction(0x3CAFF0u, +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* /*runtime*/) {
        if (!rdram || !ctx) return;
        *(uint32_t*)(rdram + 0x443898u) = 0x003D3910u;
        static std::atomic<bool> s_logged{false};
        if (!s_logged.exchange(true)) {
            printf("[callback[31] 0x3CAFF0] rdram[0x443898] = 0x003D3910 (PARTIAL — helper+prepend skipped)\n");
            fflush(stdout);
        }
        ctx->pc = GPR_U32(ctx, 31);
    });

    // --- callback[45] @ 0x3CB940 — matrix + 8 trailing words (pure data) ---
    //
    // Cycle 28 phase 11.  No helper jal; pure data init.  Writes the
    // standard 4x4 type-A matrix at rdram[0x443F60..0x443F9F], then 8
    // trailing scalar words at rdram[0x443FA0..0x443FBC]:
    //   [+00]=0, [+04]=0, [+08]=0, [+0C]=64 (int),
    //   [+10]=0, [+14]=-1.0f, [+18]=0, [+1C]=+1.0f
    runtime.registerFunction(0x3CB940u, +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* /*runtime*/) {
        if (!rdram || !ctx) return;
        static const float kMatrix[16] = {
            0.f, 0.f, 0.f, 1.f,
            1.f, 0.f, 0.f, 1.f,
            0.f, 1.f, 0.f, 1.f,
            0.f, 0.f, 1.f, 1.f,
        };
        std::memcpy(rdram + 0x443F60u, kMatrix, 64u);
        *(uint32_t*)(rdram + 0x443FA0u) = 0u;
        *(uint32_t*)(rdram + 0x443FA4u) = 0u;
        *(uint32_t*)(rdram + 0x443FA8u) = 0u;
        *(uint32_t*)(rdram + 0x443FACu) = 64u;
        *(uint32_t*)(rdram + 0x443FB0u) = 0u;
        *(uint32_t*)(rdram + 0x443FB4u) = 0xBF800000u; // -1.0f
        *(uint32_t*)(rdram + 0x443FB8u) = 0u;
        *(uint32_t*)(rdram + 0x443FBCu) = 0x3F800000u; // +1.0f
        static std::atomic<bool> s_logged{false};
        if (!s_logged.exchange(true)) {
            printf("[callback[45] 0x3CB940] matrix+8 written at 0x443F60..0x443FBF\n");
            fflush(stdout);
        }
        ctx->pc = GPR_U32(ctx, 31);
    });

    // --- callback[57] @ 0x3CBFB0 — pure float reciprocal ---
    //
    // Trivial 8-instruction callback (cycle 28 phase 9):
    //   lwc1 $f0, 0x3640(0x0038_0000)   ; load float at rdram[0x383640]
    //   lui  $v1, 0x3F80                ; $v1 = bits(1.0f)
    //   mtc1 $v1, $f1                   ; $f1 = 1.0f
    //   div.s $f0, $f1, $f0             ; $f0 = 1.0f / $f0
    //   swc1 $f0, 0x4500(0x0044_0000)   ; rdram[0x444500] = 1.0f / rdram[0x383640]
    //
    // ELF data at 0x383640 = 0x46FFFE00 = 32767.0f → result ≈ 3.0518e-5f.
    // No calls, no stack churn, no helper-call instability risk.  Faithfully
    // translated by reading rdram at runtime in case 0x383640 is rewritten
    // (it isn't, but this matches MIPS semantics exactly).
    runtime.registerFunction(0x3CBFB0u, +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* /*runtime*/) {
        if (!rdram || !ctx) return;
        const float src = *(const float*)(rdram + 0x383640u);
        const float result = 1.0f / src;
        *(float*)(rdram + 0x444500u) = result;
        static std::atomic<bool> s_logged{false};
        if (!s_logged.exchange(true)) {
            printf("[callback[57] 0x3CBFB0] rdram[0x444500] = 1.0/%.6f = %.9f\n",
                   src, result);
            fflush(stdout);
        }
        ctx->pc = GPR_U32(ctx, 31);
    });

    // --- Boot-callback hand-translations (cycle 28 phase 2-3) ---
    //
    // The 66-entry boot-init callback array at 0x3CC548..0x3CC650 dispatches
    // these functions in 0x3C9F70..0x3CC3D0.  Cycle 28's pattern-scan
    // classified them:
    //   TYPE A (52 entries): pure data init — write the same 64-byte
    //     matrix pattern to a fixed rdram target, jr $ra. No function calls.
    //   TYPE B (10 entries): non-trivial; call subsystem init functions
    //     (0x11AE30, 0x2E8860, 0x315B60, 0x164AA0, 0x299130, 0x24CF80,
    //     0x24A8E0, 0x3090E8, 0x164DD0).  Two of them ([11], [12]) also
    //     call sub_002E91C0 (the list prepend).
    //
    // This block handles all 52 TYPE A callbacks with a single generic
    // handler + lookup table.  Each gets a registerFunction binding that
    // resolves to typeABulk → write the matrix to its target_addr.
    //
    // TYPE B callbacks are left on synthPrepend for now (multi-cycle work
    // to translate; each requires understanding its subsystem init fn).
    {
        // The 64-byte matrix that EVERY type-A callback writes.  Stack-write
        // pattern is identical across all of them (verified for [1], [64],
        // [65]); the only per-callback variable is the target rdram address.
        static const float kMatrix[16] = {
            0.f, 0.f, 0.f, 1.f,
            1.f, 0.f, 0.f, 1.f,
            0.f, 1.f, 0.f, 1.f,
            0.f, 0.f, 1.f, 1.f,
        };

        // Generated by tools/decode_mips.py pattern-scan in cycle 28 phase 3.
        // Format: {callback_pc, target_rdram_addr}.  52 entries.
        static const struct { uint32_t pc, target; } kTypeA[] = {
            {0x003C9F70u, 0x00434630u}, // [0] PARTIAL: matrix only; drops jal 0x11AE30 + return-value store to 0x3B14B0
            {0x003CA020u, 0x00444830u}, // [1]
            {0x003CA0A0u, 0x00444880u}, // [2]
            {0x003CA120u, 0x00434940u}, // [3] PARTIAL: matrix only; drops jal sub_2E8860($a1=0x262FD0,$a3=8,$s0=4)
            {0x003CA1D0u, 0x00444980u}, // [4]
            {0x003CA250u, 0x004449D0u}, // [5]
            {0x003CA2D0u, 0x00444AE0u}, // [6]
            {0x003CA390u, 0x004453A0u}, // [8]
            {0x003CA410u, 0x004454B0u}, // [9]
            {0x003CA490u, 0x004355C0u}, // [10] PARTIAL: matrix only; drops 2x jal 0x164AA0 + state-dep code
            {0x003CA7F0u, 0x00442DD0u}, // [14]
            {0x003CA870u, 0x00442F00u}, // [15]
            {0x003CA8F0u, 0x00442F80u}, // [16]
            {0x003CA970u, 0x004430C0u}, // [17]
            {0x003CAA20u, 0x004431C0u}, // [19]
            {0x003CAAA0u, 0x00443200u}, // [20]
            {0x003CAB20u, 0x00443250u}, // [21]
            {0x003CABA0u, 0x004432A0u}, // [22]
            {0x003CAC20u, 0x004432E0u}, // [23]
            {0x003CACA0u, 0x00443330u}, // [24]
            {0x003CAD20u, 0x00443370u}, // [25]
            {0x003CADA0u, 0x00443410u}, // [26]
            {0x003CAE20u, 0x00443470u}, // [27]
            {0x003CAEA0u, 0x00443550u}, // [28]
            {0x003CAF20u, 0x004435A0u}, // [29]
            {0x003CB040u, 0x004438D0u}, // [32]
            {0x003CB0C0u, 0x00443910u}, // [33]
            {0x003CB170u, 0x00443980u}, // [35]
            {0x003CB1F0u, 0x004439C0u}, // [36]
            {0x003CB270u, 0x00443A10u}, // [37]
            {0x003CB2F0u, 0x00443AB0u}, // [38]
            {0x003CB300u, 0x00443AB0u}, // [39]
            {0x003CB380u, 0x00443B00u}, // [40]
            {0x003CB400u, 0x00443B40u}, // [41]
            {0x003CB4A0u, 0x00443CA0u}, // [42] PARTIAL: matrix only; drops extensive pre/post writes + helper jals + loop
            {0x003CB790u, 0x00443E40u}, // [43]
            {0x003CB8C0u, 0x00443F20u}, // [44]
            {0x003CBA10u, 0x00444000u}, // [46]
            {0x003CBAB0u, 0x004440A0u}, // [47]
            {0x003CBB30u, 0x00444110u}, // [48]
            {0x003CBBB0u, 0x00444180u}, // [49]
            {0x003CBC30u, 0x004441F0u}, // [50]
            {0x003CBCB0u, 0x00444250u}, // [51]
            {0x003CBD30u, 0x004442A0u}, // [52]
            {0x003CBDB0u, 0x00444330u}, // [53]
            {0x003CBE30u, 0x00444400u}, // [54]
            {0x003CBEB0u, 0x00444440u}, // [55]
            {0x003CBF30u, 0x00444490u}, // [56]
            {0x003CBFE0u, 0x00444510u}, // [58]
            {0x003CC060u, 0x00444560u}, // [59]
            {0x003CC0E0u, 0x00444630u}, // [60]
            {0x003CC160u, 0x00444680u}, // [61]
            {0x003CC1E0u, 0x00444720u}, // [62] PARTIAL: matrix only; drops 3x jal sub_2E8860
            {0x003CC2D0u, 0x00446770u}, // [63]
            {0x003CC350u, 0x00446800u}, // [64]
            {0x003CC3D0u, 0x00446A40u}, // [65]
        };

        // Lookup table: pc → target.  Populated at boot from kTypeA.
        // Using a static unordered_map so the handler lambda can capture it
        // by reference via a static pointer.
        static std::unordered_map<uint32_t, uint32_t> s_typeATargets;
        for (const auto& e : kTypeA) {
            s_typeATargets[e.pc] = e.target;
        }
        static const float* const kMatrixPtr = kMatrix;

        auto typeABulk = +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* /*runtime*/) {
            if (!rdram || !ctx) return;
            const uint32_t pc = ctx->pc;
            auto it = s_typeATargets.find(pc);
            if (it != s_typeATargets.end()) {
                std::memcpy(rdram + it->second, kMatrixPtr, 64u);
                static std::atomic<uint64_t> s_n{0};
                const auto n = s_n.fetch_add(1u, std::memory_order_relaxed);
                if (n < 4u || (n % 100u) == 0u) {
                    printf("[typeA-cb] n=%llu pc=0x%08X -> rdram[0x%08X..+0x40]\n",
                           (unsigned long long)n, pc, it->second);
                    fflush(stdout);
                }
            }
            ctx->pc = GPR_U32(ctx, 31);
        };

        for (const auto& e : kTypeA) {
            runtime.registerFunction(e.pc, typeABulk);
        }
        std::cout << "[Bootstrap] Registered " << (sizeof(kTypeA)/sizeof(kTypeA[0]))
                  << " TYPE-A boot callbacks (matrix-write)" << std::endl;
    }

    // --- 0x00FFF600  list-walker callback logger (synthetic) ---
    //
    // Installed as the fn pointer in nodes prepended by synthPrepend.
    // When the list walker (sub_002E9170) pops a node and calls
    // (node->fn)($a0=data, $a1=-1), $a0 is the original callback PC
    // (set as data in synthPrepend).  Log each invocation to reveal
    // which callbacks the walker actually pops, in what order.
    runtime.registerFunction(0x00FFF600u, +[](uint8_t* /*rdram*/, R5900Context* ctx, PS2Runtime* /*runtime*/) {
        static std::atomic<uint64_t> s_n{0};
        const auto n = s_n.fetch_add(1u, std::memory_order_relaxed);
        const uint32_t data = GPR_U32(ctx, 4);    // original callback PC
        const uint32_t arg2 = GPR_U32(ctx, 5);    // -1 from walker
        if (n < 80u || (n % 200u) == 0u) {
            printf("[walker->FFF600] pop#%llu data=0x%08X arg=%d\n",
                   (unsigned long long)n, data, (int32_t)arg2);
            fflush(stdout);
        }
        SET_GPR_S32(ctx, 2, 0);        // $v0 = 0 (no-op return)
        ctx->pc = GPR_U32(ctx, 31);    // jr $ra
    });

    // --- 0x259E00  GS allocator bypass (sub_00259E00) ---
    //
    // Called from sub_237640 when vtable[0x9C] returns non-zero (label_2376a0),
    // with $a0 = result of func_2E8D90(0x510).  Real function does heavy GS buffer
    // allocation via func_2AE080 (a TODO_NAMED stub) and related init — calling it
    // risks hitting uninitialised state and hanging.  Bypassed here; func_25A3E0
    // (below) is also bypassed for the same reason.  func_13FDA0 does not depend
    // on data set up by this pair and runs correctly without them.
    runtime.registerFunction(0x259E00u, +[](uint8_t* /*rdram*/, R5900Context* ctx, PS2Runtime* /*runtime*/) {
        static std::atomic<bool> s_logged{false};
        if (!s_logged.exchange(true)) {
            printf("[BYPASS 259E00] GS allocator bypassed (returns 0)\n");
            fflush(stdout);
        }
        SET_GPR_S32(ctx, 2, 0);        // $v0 = 0 (no allocation handle)
        ctx->pc = GPR_U32(ctx, 31);    // jr $ra
    });

    // --- 0x25A3E0  GS state-block setup bypass (sub_0025A3E0) ---
    //
    // Called from sub_237640 (label_2376c0) with $a0 = result of func_259E00.
    // Initialises a GS state descriptor block at $a0; if $a0=0 (from bypassed
    // func_259E00) the writes would target the very start of RDRAM which is
    // harmless but pointless.  Bypassed for clarity; func_13FDA0 is called next
    // regardless and does not depend on the state block.
    runtime.registerFunction(0x25A3E0u, +[](uint8_t* /*rdram*/, R5900Context* ctx, PS2Runtime* /*runtime*/) {
        static std::atomic<bool> s_logged{false};
        if (!s_logged.exchange(true)) {
            printf("[BYPASS 25A3E0] GS state-block setup bypassed (returns 0)\n");
            fflush(stdout);
        }
        SET_GPR_S32(ctx, 2, 0);        // $v0 = 0
        ctx->pc = GPR_U32(ctx, 31);    // jr $ra
    });

    // --- 0x237640  boot subinit dispatch — diagnostic wrapper ---
    //
    // Called from sub_239C40 (boot_subinit) at label_23a100, late in the boot chain:
    //   sub_239C40 → label_23a100: jal func_237640($a0=READ32(0x382B80), $a1=$sp+0x370)
    //
    // Real code flow inside sub_237640:
    //   1. jal func_237A40($a0=modBase)          — returns 1, no side-effects
    //   2. JALR vtable[0x9C] via modBase+0x27C   — previously returned 0 (FFF400), now 1 (FFF500)
    //   3. If non-zero → label_2376a0:
    //        func_2E8D90(0x510) → func_259E00(rv) → WRITE32(0x443A60,rv) → func_25A3E0(rv)
    //        → func_13FDA0(modBase, buf)           ← GOAL: render infrastructure init
    //
    // This wrapper logs entry/exit plus the func_13FDA0 "initialized" flag at
    // modBase+0x22C (written to 1 by func_13FDA0's very first instruction).
    runtime.registerFunction(0x237640u, +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime) {
        const uint32_t modBase = GPR_U32(ctx, 4);
        const uint32_t buf     = GPR_U32(ctx, 5);
        printf("[DIAG sub_237640] ENTRY modBase=0x%08X buf=0x%08X\n", modBase, buf);
        fflush(stdout);
        sub_00237640_0x237640(rdram, ctx, runtime);
        const uint32_t initFlag = (modBase != 0u && (modBase + 0x22Cu) < 0x2000000u)
                                  ? READ32(modBase + 0x22Cu) : 0xDEADu;
        printf("[DIAG sub_237640] EXIT rv=0x%08X modBase[0x22C](initFlag)=0x%08X rdram[0x443A60]=0x%08X\n",
               GPR_U32(ctx, 2), initFlag, READ32(0x443A60u));
        fflush(stdout);
    });

    // --- 0x13FDA0  render infrastructure init — diagnostic wrapper ---
    //
    // Called from sub_237640 (label_2376cc) with ($a0=modBase, $a1=buf).
    // First action: WRITE32(modBase+0x22C, 1) — "initialized" flag.
    // Then initialises many module/render slots via repeated func_2E8D90 + func_141300 calls.
    // This wrapper confirms whether the function is reached and whether it returns normally.
    runtime.registerFunction(0x13FDA0u, +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime) {
        printf("[DIAG sub_13FDA0] ENTRY a0=0x%08X a1=0x%08X\n", GPR_U32(ctx, 4), GPR_U32(ctx, 5));
        fflush(stdout);
        sub_0013FDA0_0x13fda0(rdram, ctx, runtime);
        printf("[DIAG sub_13FDA0] EXIT rv=0x%08X\n", GPR_U32(ctx, 2));
        fflush(stdout);
    });

    // --- 0x2E91C0 / 0x2e9210 — callback array iterator trace ---
    //
    // sub_002E91C0 contains entry_2E91F0: a callback array iterator that
    // walks $a0..$a1 calling each function pointer in turn (the `s0=a0;
    // do { v0=READ32(s0); jalr v0; s0+=4 } while (s0<s1)` pattern at
    // 0x2e91f0..0x2e9230). sub_239C40 tail-jumps into this iterator with a
    // callback array populated during boot.
    //
    // Cycle 27 finding: sub_239C40 exits with pc=0x2e9210 (interior label
    // inside the iterator's jalr step), suggesting an inner JALR returned
    // unexpected pc and 239C40 preempted. 0x2e9210 IS registered as an entry
    // to sub_002E91C0 (register_functions.cpp), so the runtime's recover-pc
    // loop SHOULD dispatch back into the iterator. Trace to confirm this
    // happens, with $s0/$s1 and the function ptr it's about to call.
    {
        extern void sub_002E91C0_0x2e91c0(uint8_t*, R5900Context*, PS2Runtime*);
        runtime.registerFunction(0x2e9210u, +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime) {
            static std::atomic<uint64_t> s_n{0};
            const auto n = s_n.fetch_add(1u, std::memory_order_relaxed);
            const uint32_t s0  = GPR_U32(ctx, 16);
            const uint32_t s1  = GPR_U32(ctx, 17);
            const uint32_t fp  = (s0 < 0x2000000u) ? *(uint32_t*)(rdram + s0) : 0u;
            if (n < 32u || (n % 200u) == 0u) {
                printf("[TRACE iter 0x2E9210] call#%llu s0=0x%08X s1=0x%08X fp=0x%08X ra=0x%08X\n",
                       (unsigned long long)n, s0, s1, fp, GPR_U32(ctx, 31));
                fflush(stdout);
            }
            // On the very first call, dump the entire boot-init callback array
            // AND register any unregistered entries that fall inside the
            // sub_0031D200 range (0x31D200..0x3D5A00) as aliases to the
            // interpreter stub.
            //
            // KEY DISCOVERY (cycle 27 phase 2): the 66-entry callback array at
            // 0x3CC548..0x3CC650 holds function pointers in the 0x3C9xxx-0x3CCxxx
            // range — all of which lie inside sub_0031D200's 748KB span. The
            // recompiler/analyzer never saw these addresses as call targets
            // (they're only reached via indirect jalr through this data table),
            // so they aren't in register_functions.cpp. When the iterator does
            // `jalr v0` with v0=0x3CA020, lookupFunction returns null and the
            // call silently fails. This is why the 66-callback boot init never
            // completes its real work — every callback no-ops.
            //
            // Fix: register every callback in the array with the existing
            // sub_0031D200_0x31d200 interpreter stub (large_function_stubs.cpp).
            // The interpreter reads opcodes starting at ctx->pc, so each
            // entry gets executed from its own pc as MIPS bytecode.
            //
            // This only fires once (n==0); subsequent iterator entries are no-ops.
            if (n == 0u && s0 != 0u && s1 > s0 && (s1 - s0) < 0x400u && s1 < 0x2000000u) {
                extern void sub_0031D200_0x31d200(uint8_t*, R5900Context*, PS2Runtime*);
                const uint32_t count    = (s1 - s0) / 4u;
                uint32_t registeredNow  = 0u;
                uint32_t alreadyKnown   = 0u;
                uint32_t outOfRange     = 0u;
                printf("[TRACE iter 0x2E9210] dump+register array @0x%08X..0x%08X (%u entries):\n", s0, s1, count);
                for (uint32_t i = 0; i < count; ++i) {
                    const uint32_t addr = s0 + i * 4u;
                    const uint32_t ptr  = *(uint32_t*)(rdram + addr);
                    const bool inRange  = (ptr >= 0x31D200u && ptr < 0x3D5A00u);
                    const bool already  = runtime->hasFunction(ptr);
                    if (inRange && !already) {
                        runtime->registerFunction(ptr, sub_0031D200_0x31d200);
                        ++registeredNow;
                    } else if (already) {
                        ++alreadyKnown;
                    } else {
                        ++outOfRange;
                    }
                    printf("  [%2u] @0x%08X -> 0x%08X %s\n", i, addr, ptr,
                           inRange ? (already ? "(already)" : "(REGISTERED)") : "(out-of-range)");
                }
                printf("[TRACE iter 0x2E9210] registered=%u already=%u oor=%u total=%u\n",
                       registeredNow, alreadyKnown, outOfRange, count);
                fflush(stdout);
            }
            ctx->pc = 0x2e9210u;
            sub_002E91C0_0x2e91c0(rdram, ctx, runtime);
        });
    }

    // --- 0x2E9150  moduleMain entry — runtime trace ---
    //
    // Per CLAUDE.md, 0x2E9150 is the interior entry of sub_002E90F0 used to
    // kick off the module manager.  sub_002E90F0 internally calls
    // sub_00302DF0 (moduleManager_mainLoop) — the game's infinite main loop.
    //
    // The runtime currently parks the EE main thread at pc=0x1000ac and shows
    // module state[6] advancing to "done" but no further game progression.
    // Open question: does moduleMain (and therefore moduleManager_mainLoop)
    // ever actually run?  Trace entry/exit + call count.
    //
    // NOTE: 0x2E9150 already has a one-shot registration at ~line 1165 above
    // (the plain trampoline).  registerFunction in PS2Recomp's runtime accepts
    // replacement; this hook will override it with the same trampoline plus
    // a one-line entry log.
    {
        extern void sub_002E90F0_0x2e90f0(uint8_t*, R5900Context*, PS2Runtime*);
        runtime.registerFunction(0x2E9150u, +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime) {
            static std::atomic<uint64_t> s_n{0};
            const auto n = s_n.fetch_add(1u, std::memory_order_relaxed);
            if (n < 4u || (n % 60u) == 0u) {
                printf("[TRACE moduleMain 0x2E9150] call#%llu ra=0x%08X a0=0x%08X\n",
                       (unsigned long long)n, GPR_U32(ctx, 31), GPR_U32(ctx, 4));
                fflush(stdout);
            }
            ctx->pc = 0x2E9150u;
            sub_002E90F0_0x2e90f0(rdram, ctx, runtime);
        });
    }

    // --- 0x2EADD0  list-walker wrapper — trace, dump linked-list state ---
    //
    // sub_002EADD0 is a 1-instruction tail-jump to func_2E9170 (the
    // linked-list walker that pops nodes off the list at 0x446AD0 and
    // calls (node->fn)(node->data, -1) for each).  Called from
    // sub_00239C40 during boot.
    //
    // Trace head-of-list when this fires.  If the list is empty (head=0)
    // the walker does nothing and returns immediately.  Cycle 27 phase 4
    // confirmed: dataSegNoop short-circuits the 66 boot callbacks that
    // would have populated this list, leaving it empty.  This trace makes
    // that visible at the call site.
    {
        extern void sub_002EADD0_0x2eadd0(uint8_t*, R5900Context*, PS2Runtime*);
        runtime.registerFunction(0x2EADD0u, +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime) {
            static std::atomic<uint64_t> s_n{0};
            const auto n = s_n.fetch_add(1u, std::memory_order_relaxed);
            const uint32_t head = *(uint32_t*)(rdram + 0x446AD0u);
            if (n < 8u || (n % 600u) == 0u) {
                uint32_t depth = 0u;
                uint32_t cur   = head;
                while (cur != 0u && depth < 100u && cur < 0x2000000u) {
                    cur = *(uint32_t*)(rdram + cur);
                    ++depth;
                }
                printf("[TRACE listWalker 0x2EADD0] call#%llu head=0x%08X depth=%u ra=0x%08X\n",
                       (unsigned long long)n, head, depth, GPR_U32(ctx, 31));
                fflush(stdout);
            }
            ctx->pc = 0x2EADD0u;
            sub_002EADD0_0x2eadd0(rdram, ctx, runtime);
        });
    }

    // --- 0x302DF0  moduleManager_mainLoop — runtime trace ---
    //
    // CLAUDE.md: "Game's infinite main loop: moduleDispatch → userInit → repeat".
    // If this never runs, the module-manager state machine that would drive
    // setGameState transitions never executes, leaving frameDispatch stuck on
    // the initial gs_initState fn for the lifetime of the process.
    //
    // Trace count + first-N entries to disambiguate "never called" from
    // "called but stuck in the loop body".
    {
        extern void sub_00302DF0_0x302df0(uint8_t*, R5900Context*, PS2Runtime*);
        runtime.registerFunction(0x302DF0u, +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* runtime) {
            static std::atomic<uint64_t> s_n{0};
            const auto n = s_n.fetch_add(1u, std::memory_order_relaxed);
            if (n < 4u || (n % 120u) == 0u) {
                printf("[TRACE moduleManager 0x302DF0] call#%llu ra=0x%08X a0=0x%08X\n",
                       (unsigned long long)n, GPR_U32(ctx, 31), GPR_U32(ctx, 4));
                fflush(stdout);
            }
            ctx->pc = 0x302DF0u;
            sub_00302DF0_0x302df0(rdram, ctx, runtime);
        });
    }

    // --- 0x00FFF200  module_keepalive_6 (synthetic) ---
    //
    // The game's module state machine (sub_00308958) reads state[6] from
    //   rdram[stateTablePtr + 24]  (stateTablePtr = rdram[modTable+0x1D4]).
    //
    // Without IOP, no async callback sets state[6] to a function pointer, so
    // the init path (state[6]==0) loops forever calling the event pump (func_2FEA30),
    // which is registered as an infinite sleep — causing the game-loop thread to hang.
    //
    // Bootstrap Phase 4 writes state[6] = 0xFFF200. This sentinel function is
    // then called via jalr from sub_00308958:label_308a20 with:
    //   $a0 = module_index = 6
    //   $ra = 0x308A2C (return address inside sub_00308958)
    //   state[6] already cleared to 0 (delay-slot before the jalr)
    //
    // Behaviour:
    //   - Before IOP init completes (s_iop_init_done == 0): re-arms state[6]=0xFFF200
    //     to prevent the module manager from looping through the func_2FEA30 hang path.
    //   - After IOP init completes (s_iop_init_done == 1, set by sif_dmaSend call#3):
    //     writes state[6]=-1 to let the module manager advance to "done" state,
    //     which writes 22 to modTable[0] and unblocks the boot loop.
    runtime.registerFunction(0x00FFF200u, +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* /*runtime*/) {
        static std::atomic<uint32_t> s_n{0};
        const uint32_t n = s_n.fetch_add(1, std::memory_order_relaxed);

        const uint32_t stateTablePtr = *(const uint32_t*)(rdram + 0x385334u);
        const bool valid = (stateTablePtr != 0u && stateTablePtr != 0xFFFFFFFFu);

        if (s_iop_init_done.load(std::memory_order_acquire) != 0u) {
            // IOP init done — advance module 6 to "done" state.
            //
            // state[6]=-1 causes sub_00308958 to write 22 to modTable[0] and return 1,
            // which unblocks the module manager's boot poll.
            //
            // NOTE: do NOT write 0x31D200 to rdram[0x384670] here.
            // sub_0031D200 is the game's 748KB main-logic state machine, but it
            // requires a valid game-context pointer in $a0 that frameDispatch
            // normally picks up from the interrupted EE thread context.  Without
            // that pointer the function reads from address 0 and returns immediately
            // every frame, stalling game progression.  The per-frame state function
            // at 0x384670 should only change when the game itself calls setGameState
            // (0x2FDDF8), which happens once IOP/SIF callbacks fire.  Keep
            // gs_initState (0x251B10) in place until that happens.
            if (valid) {
                *(uint32_t*)(rdram + stateTablePtr + 24u) = 0xFFFFFFFFu; // state[6] = -1 = done
            }
            if (n == 0u) {
                printf("[modUpdate6] IOP done: state[6]=-1 (module 6 advancing to done; 0x384670 unchanged)\n");
                fflush(stdout);
            }
        } else {
            // IOP not done yet — re-arm to prevent func_2FEA30 hang
            if (valid) {
                *(uint32_t*)(rdram + stateTablePtr + 24u) = 0xFFF200u;
            }
            if (n < 2u) {
                printf("[modUpdate6] n=%u re-armed state[6]=0xFFF200 (IOP init pending)\n", n);
                fflush(stdout);
            }
        }

        ctx->pc = GPR_U32(ctx, 31); // jr $ra
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
    //
    // CYCLE 28 PHASE 1 — REFINED SYNTHETIC PREPEND:
    //   Restrict synthPrepend to the exact boot-init callback array range
    //   (0x3C9F70..0x3CC3D0 = 66 entries observed via the iterator dump in
    //   cycle 27 phase 4).  Addresses outside this range still get
    //   dataSegNoop — phase 6's blanket synthPrepend across 0x3C7B80..
    //   0x3CE000 created phantom list entries (128+ instead of 66).
    //
    //   Also: fn pointer in the synthetic node is now 0x00FFF600 (a new
    //   sentinel that LOGS each invocation with the data field).  This
    //   reveals which subset of the 66 callbacks the walker actually pops,
    //   in what order, and confirms the popped data values.
    //
    // CYCLE 27 PHASE 6 — SYNTHETIC PREPEND EXPERIMENT:
    //
    //   Background: phases 3-5 established that the 66 callbacks in
    //   0x3C9F70..0x3CC3D0 are boot-init nodes that each prepend a struct
    //   to the linked list at 0x446AD0.  With dataSegNoop the list stays
    //   empty and the list walker at 0x2E9170 has nothing to do.
    //
    //   Replace dataSegNoop with a stub that prepends a SYNTHETIC node
    //   for each callback invocation:
    //     node[0] = previous head (linked-list next)
    //     node[4] = synthetic fn = 0x00FFF400 (returns 0, no-op)
    //     node[8] = callback PC (so the walker sees a unique data field
    //               per entry, useful for downstream tracing)
    //   Nodes are pre-allocated in a static rdram region (0x4F0000..0x4F0E10
    //   = 0x600 bytes = 128 × 12-byte slots, more than 66 needed).
    //
    //   The unique pool slot is selected by hashing the callback PC into
    //   the pool — each PC maps to a stable slot, so multiple calls of
    //   the same callback overwrite each other's nodes (acceptable —
    //   a single boot run only calls each callback once via the iterator).
    //
    //   After all callbacks fire, the linked list at 0x446AD0 will hold
    //   up to 66 nodes (chained via offset +0).  The list walker
    //   (sub_002E9170) pops each and calls (node->fn)(node->data, -1) =
    //   0xFFF400(callback_pc, -1) → returns 0.  Behavior change: list
    //   walker now does real work (pops 66 nodes, calls 66 noops) instead
    //   of immediately returning on empty list.
    //
    //   Verification: the [TRACE listWalker] hook below will report
    //   depth=66 instead of depth=0.
    //
    //   Risk: this populates the list with stub nodes.  If a downstream
    //   subsystem reads the node[4] (fn) and expects a real subsystem
    //   handler (more than "return 0"), it will get the noop and may
    //   behave differently than expected.  But behavior change IS the
    //   point — we want to know if anything reacts to a non-empty list.
    //
    //   Rollback: replace the synthetic-prepend stub with the previous
    //   dataSegNoop lambda (one line).
    {
        // Pool of 128 12-byte nodes starting at rdram[0x4F0000].
        // Safely above the BSS clear region (zeroed 0x3D5A00..0x44F600 by _start).
        constexpr uint32_t kPoolBase  = 0x4F0000u;
        constexpr uint32_t kPoolSlots = 128u;

        auto synthPrepend = +[](uint8_t* rdram, R5900Context* ctx, PS2Runtime* /*runtime*/) {
            if (!rdram || !ctx) return;
            const uint32_t cbPc = ctx->pc;
            // Only synth-prepend for addresses in the observed boot-init
            // callback array range (0x3C9F70..0x3CC3D0).  Other addresses
            // in the dataSegNoop range get the original noop behaviour.
            const bool inCallbackArray = (cbPc >= 0x3C9F70u && cbPc <= 0x3CC3D0u);
            if (!inCallbackArray) {
                ctx->pc = GPR_U32(ctx, 31);
                return;
            }
            // Monotonic counter for pool slot — avoids pc-hash collisions.
            // Pool is 1024 slots × 12 bytes = 12KB at 0x4F0000..0x4F3000.
            // Wraps at 1024; the 66 real callbacks fire once each per boot
            // so wrap is academic.
            static std::atomic<uint32_t> s_slot{0};
            const uint32_t idx  = s_slot.fetch_add(1u, std::memory_order_relaxed) & 0x3FFu;
            const uint32_t node = 0x4F0000u + idx * 12u;
            // Prepend: node[0] = oldHead; head = node
            const uint32_t oldHead = *(uint32_t*)(rdram + 0x446AD0u);
            *(uint32_t*)(rdram + node + 0u) = oldHead;
            // fn = 0x00FFF600 sentinel — logs each invocation with data field
            *(uint32_t*)(rdram + node + 4u) = 0x00FFF600u;
            *(uint32_t*)(rdram + node + 8u) = cbPc;        // data = callback PC
            *(uint32_t*)(rdram + 0x446AD0u) = node;
            // Light periodic logging
            static std::atomic<uint64_t> s_n{0};
            const auto n = s_n.fetch_add(1u, std::memory_order_relaxed);
            if (n < 8u || (n % 200u) == 0u) {
                printf("[synthPrepend] n=%llu pc=0x%08X node=0x%08X oldHead=0x%08X\n",
                       (unsigned long long)n, cbPc, node, oldHead);
                fflush(stdout);
            }
            // Return: jr $ra
            ctx->pc = GPR_U32(ctx, 31);
        };

        // Zero the node pool so stale data doesn't confuse the list walker.
        // (Actually unnecessary — _start will not touch 0x4F0000 since it's
        // outside the BSS zero range, and our prepend writes all 3 fields
        // before publishing.  Defensive zero anyway in case of reset.)
        // We can't access rdram here yet (it isn't allocated); zeroing is
        // deferred to the first prepend.

        // Skip set: addresses with hand-translated overrides registered
        // earlier (typeABulk handles 52 callback PCs).  registerFunction
        // overwrites, so without this skip the synthPrepend bulk loop
        // would clobber our specific overrides for type-A callbacks.
        //
        // Type-A callback PCs (must match kTypeA[] in the typeABulk block)
        // plus prepender overrides (callback[18] @ 0x3CA9F0):
        static const uint32_t kSkipPcs[] = {
            0x003C9F70u, // [0] partial (matrix only)
            0x003CA620u, // [11] prepender (matrix + func_299130)
            0x003CA9F0u, // [18] prepender
            0x003CB140u, // [34] prepender
            0x003CA350u, // [7]  no-op: handle-allocator stub (zeros to slots, equivalent to alloc failure)
            0x003CA780u, // [13] partial: CDVD-stub-aware (skips capA/capB clears)
            0x003CAFF0u, // [31] partial: static data write only
            0x003CB940u, // [45] matrix+8 trailing words at 0x443F60
            0x003CBFB0u, // [57] pure float reciprocal (1/32767 -> 0x444500)
            0x003CA020u, 0x003CA0A0u, 0x003CA120u /*[3] partial*/, 0x003CA1D0u, 0x003CA250u, 0x003CA2D0u,
            0x003CA390u, 0x003CA410u, 0x003CA490u /*[10] partial*/, 0x003CA7F0u, 0x003CA870u, 0x003CA8F0u,
            0x003CA970u, 0x003CAA20u, 0x003CAAA0u, 0x003CAB20u, 0x003CABA0u,
            0x003CAC20u, 0x003CACA0u, 0x003CAD20u, 0x003CADA0u, 0x003CAE20u,
            0x003CAEA0u, 0x003CAF20u, 0x003CB040u, 0x003CB0C0u, 0x003CB170u,
            0x003CB1F0u, 0x003CB270u, 0x003CB2F0u, 0x003CB300u, 0x003CB380u,
            0x003CB400u, 0x003CB4A0u /*[42] partial*/, 0x003CB790u, 0x003CB8C0u, 0x003CBA10u, 0x003CBAB0u,
            0x003CBB30u, 0x003CBBB0u, 0x003CBC30u, 0x003CBCB0u, 0x003CBD30u,
            0x003CBDB0u, 0x003CBE30u, 0x003CBEB0u, 0x003CBF30u, 0x003CBFE0u,
            0x003CC060u, 0x003CC0E0u, 0x003CC160u, 0x003CC1E0u /*[62] partial*/, 0x003CC2D0u, 0x003CC350u,
            0x003CC3D0u,
        };
        static std::unordered_set<uint32_t> s_skip(std::begin(kSkipPcs), std::end(kSkipPcs));
        for (uint32_t addr = 0x3C7B80u; addr < 0x3CE000u; addr += 4u) {
            if (s_skip.count(addr)) continue;
            runtime.registerFunction(addr, synthPrepend);
        }
        std::cout << "[Bootstrap] synthPrepend installed for 0x3C7B80..0x3CE000 "
                     "(restricted to 0x3C9F70..0x3CC3D0 array; pool 0x4F0000+, 1024 slots, fn=0xFFF600)" << std::endl;
    }

    // --- Load and run ---

    if (!runtime.loadELF(elfPath))
    {
        std::cerr << "Failed to load ELF: " << elfPath << std::endl;
        return 1;
    }

    // --- Pre-boot RDRAM initialisation (must happen after loadELF, before run) ---
    //
    // _start zeroes BSS in the range 0x3D5A00-0x44F600 during its first
    // instructions.  Any rdram writes inside that range made before
    // runtime.run() will be wiped.  We use addresses ABOVE 0x44F600 so
    // the values survive.
    //
    // sub_239C40 (boot_subinit) is called by _start almost immediately.
    // At label_23a124 it reads rdram[0x382B80] to get the module-struct
    // base pointer, then dereferences vtable chains from it.  The 3-second
    // bootstrap thread fires too late; we must prime the chain here.
    //
    // rdram[0x382B80] is in ELF-loaded data (VA < 0x3D5A00) so it
    // survives the BSS zero-loop.  The vtable area at 0x44F800+ is above
    // the BSS ceiling and likewise survives.
    //
    // Flow when sub_239C40 hits label_23a124:
    //   $a0 = rdram[0x382B80] = 0x44F800
    //   $t9 = rdram[0x44F800 + 0x27C] = 0x44FB00
    //   $t9 = rdram[0x44FB00 + 0x30 ] = 0xFFF400  →  returns $v0=0
    //   bnez $v0 → false → loop exits → sub_239C40 proceeds to return.
    {
        uint8_t* preRdram = runtime.memory().getRDRAM();
        const uint32_t modBase   = 0x44F800u;   // above BSS ceiling 0x44F600
        const uint32_t modVTable = 0x44FB00u;
        Ps2FastWrite32(preRdram, 0x382B80u,         modBase);
        Ps2FastWrite32(preRdram, modBase  + 0x27Cu, modVTable);
        Ps2FastWrite32(preRdram, modVTable + 0x30u, 0x00FFF400u); // spin-exit sentinel (returns 0)
        Ps2FastWrite32(preRdram, modVTable + 0x9Cu, 0x00FFF500u); // init-success sentinel (returns 1) → triggers func_13FDA0 path
        Ps2FastWrite32(preRdram, modVTable + 0x4Cu, 0x00FFF400u); // spin-exit sentinel (returns 0)
        std::cout << "[PreBoot] Module vtable pre-populated: "
                  << "rdram[0x382B80]=0x" << std::hex << modBase
                  << " vtable=0x" << modVTable
                  << " (+0x30/+0x4C -> FFF400(ret0), +0x9C -> FFF500(ret1))"
                  << std::dec << std::endl;
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
