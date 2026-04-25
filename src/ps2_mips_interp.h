#pragma once
#include <cstdint>

struct R5900Context;
class PS2Runtime;

// ---------------------------------------------------------------------------
// Minimal MIPS R5900 interpreter for kseg0 overlay code.
//
// Racer Revenge installs three overlay blobs into kseg0 RDRAM at boot:
//   0x80074000 (source 0x003849A0, 0x7A8 bytes) — main dispatcher
//   0x80075000 (source 0x00384348, 0x328 bytes) — IOP/module dispatcher
//   0x80076000 (source 0x00383AD8, 0x740 bytes) — kernel/thread dispatcher
//
// The recompiler only handles ELF code below 0x80000000; it can't emit C++
// for these dynamically-copied blobs.  This interpreter fills the gap: it
// reads MIPS words directly from rdram, executes them, and hands off to the
// recompiled function table when a jal/jalr targets ELF-range code.
//
// Registers are shared with ctx so callee-save state (s0..s7, $gp, $fp,
// $ra, $sp) is preserved across the interpreter→recompiled-code boundary.
//
// Supported:  all integer R5900 instructions used by the overlay blobs.
//             COP0 instructions are silently NOP'd (no real COP0 state).
//             COP2 / MMI instructions abort with a logged warning.
//
// Usage:
//   interpretMipsKseg0(rdram, ctx, runtime, 0x80074000u);
// ---------------------------------------------------------------------------
void interpretMipsKseg0(uint8_t* rdram, R5900Context* ctx,
                        PS2Runtime* runtime, uint32_t startVa);
