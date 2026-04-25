# Module Manager & Game State Machine — Spec

Source of truth for how the game's module manager and frame-work state
machine are laid out in memory, and how they tick. Consolidates findings
from sessions 2–4 plus fresh xref data.

## TL;DR

The game has **three intertwined pieces of machinery** controlling boot flow:

1. **Module callback manager** at `sub_00308958` — walks a 32-slot state
   block at `mem[*(0x385334)]`, where each non-zero slot is a function
   pointer that gets invoked and cleared. Used to fire one-shot init
   callbacks in sequence.

2. **Frame-work dispatcher** at `0x2FE400` — the EE exception handler for
   VBlank. Reads `mem[0x384670]` (the "current work function") and
   `jalr`s it. Runs every VSync.

3. **Custom syscall dispatch table** at `0x384678` — 32 word-sized slots,
   indexed by `(syscall_id & 0x7C) / 4`. Read by the exception
   handler's syscall path (at 0x2FE640-0x2FE650). Populated via
   `SetSyscall` (syscall 0x74), and by the runtime-loaded overlay at
   0x80074000.

Boot sequence (simplified):

```
_start (0x100008)
  → zero BSS
  → InitMainThread, InitHeap (syscalls 0x3C, 0x3D)
  → 0x2FE7C0 (core kernel setup)
       → 0x2FE6C0         install a few syscall overrides; seed probe addrs
       → 0x2FE750         another SetSyscall pass
       → 0x2F68F8         thread 2 (RPC handler) setup; memcpy overlay#3
       → 0x2F6AA8         thread startup continuation
       → 0x2FE8D0         install memcpy handler; memcpy overlay#1 to 0x80074000
       → j 0x2FDF48       (tail call) memcpy overlay#2 to 0x80075000
  → 0x2F5E20              FlushCache
  → fall through to 0x100094 (our entry_point_sentinel)
```

After `_start` returns, the game expects VBlank interrupts to drive the
frame-work via `0x2FE400`. Our runtime simulates this with a VBlank worker
thread that signals a condition variable, driving `gameFrameLoop` in
`main.cpp`.

## Memory layout

| Addr | Name | Width | Purpose |
|---|---|---|---|
| `0x384670` | `game_state_fn` | 4B | Frame-work function pointer (read by frame dispatch at 0x2FE400) |
| `0x384674` | ? | 4B | Inspected during state-init chains (unverified) |
| `0x384678 + N` | `syscall_dispatch_table[N]` | 4B | Syscall handler for syscall_id where `(id & 0x7C) == N` |
| `mem[*(0x385334)]` | `module_state_block` | 128B | 32-slot callback table (4B each), driven by sub_00308958 |
| `0x3850xx` | overlay source data | — | Bytes the game memcpys into 0x8007xxxx at boot |
| `0x44BC80` | `exception_frame_buffer` | 0x200B | `$k0`-based EE exception save frame (0x2FE300 writes here) |
| `0x80074000..0x800747A8` | `overlay_kernel_74` | 0x7A8B | Kernel overlay #1. Dispatcher: lookup table at +0x780. |
| `0x80075000..0x80075328` | `overlay_kernel_75` | 0x328B | Kernel overlay #2. Dispatches syscalls 0x55–0x59. |
| `0x80076000..0x80076740` | `overlay_kernel_76` | 0x740B | Kernel overlay #3. Dispatches syscalls 0xFC–0xFF, 0x12C, 0x08. |

## Module callback manager

### `sub_00308958` — module status checker

Called with `$a0 = state_block_base, $a1 = slot_index`:

1. If `slot_index >= 0x20` → return -1.
2. `state_block = mem[state_block_base + 0x1D4]`  (the slot array pointer)
3. If `state_block == 0` → no slots allocated; fallthrough to init-trigger.
4. `fn = mem[state_block + slot_index * 4]`
5. If `fn == 0`:
   → call `sub_00308C08` (init trigger) then `sub_00308BA8` (dispatch).
6. If `fn == 1` (busy flag): skip, return.
7. Else: `(fn)()` — call it as a function pointer, then clear the slot.

This is a **one-shot callback** model. The module manager walks all 32
slots per tick; each non-zero callback fires exactly once. Our main.cpp's
Phase 2 pre-populates slot 6 with `0x2FDDF8` (setGameState) so it fires
naturally during normal module-dispatch iteration.

### `sub_00308C08 / sub_00308BA8` — init-trigger path

Reached when a slot is 0. Triggers deeper initialization. On real PS2 this
chain ultimately calls ExitThread (via 0x2F6378 → 0x2FEA30 → 0x2F57C0) —
our ExitThread override skips the termination and returns so the guest's
`while(1)` in `sub_00302DF0` keeps iterating. **Note:** this is the
specific chain that made our main loop exit prematurely; see `main.cpp`'s
`gameLoopThread` outer restart loop.

## Frame-work dispatcher (0x2FE400)

The EE exception return path. Simplified:

```
0x2FE400:
  ...save regs to exception frame at 0x44BC80...
  $at = mem[0x384670]              ; game_state_fn
  jalr $at                          ; run the frame work
  ...restore regs...
  eret
```

A `game_state_fn` of 0 short-circuits the `jalr` and only clears the
iClearEventFlag — the frame dispatch essentially becomes a no-op.

### Known frame-work functions

| Address | Purpose |
|---|---|
| `0x251B10` | GS init (one-shot; checks `gs_init_flag` at 0x442B70) |
| ? | Real menu/gameplay state fns — not yet discovered. They get installed via `setGameState` from inside overlay sub-handlers (which we still can't execute for ELF-range jal targets outside the overlay). |

## setGameState (0x2FDDF8)

```
setGameState($a0 = new_state_fn):
  mem[0x384670] = $a0
  SetSyscall-like(1, 0x2FE300)
  SetSyscall-like(2, 0x2FE300)
  SetSyscall-like(3, 0x2FE300)
  return
```

- `mem[0x384670]` becomes the new frame-work function.
- The three `SetSyscall-like` calls fire syscall `0x0D` with args
  (state_id, handler). In our runtime this maps to: `mem[0x384678 +
  state_id*4] = handler` (see `ps2_syscalls.cpp`).
- So slots 1, 2, 3 of the dispatch table get `0x2FE300` as their handler.
  `0x2FE300` is the EE exception save-state routine (the first function
  at that label inside `entry_2fe100_0x2fe660`).

**Callers:** zero direct. `FUN_002fddf8_0x2fddf8` is only reachable via
indirect dispatch: the module slot we manually pre-populate in main.cpp.
On real PS2 the caller is whatever overlay code invokes `setGameState` to
advance past the initial GS-init state — we haven't located that caller
yet because it lives inside the overlay blobs.

## Syscall dispatch table (0x384678)

Populated via `SetSyscall` (syscall 0x74). Our runtime mirrors into this
region via `mirrorGuestSyscallEntryLocked` in ps2_syscalls_system.inl.

The game's EE kernel dispatch at 0x2FE640-0x2FE650 reads this table:

```
0x2FE640:  andi $v0, $a1, 0x7C
0x2FE644:  lui  $at, 0x38
0x2FE648:  addu $at, $at, $v0          ; $at = 0x380000 + (id & 0x7C)
0x2FE64C:  lw   $at, 0x4678($at)       ; load mem[0x384678 + (id & 0x7C)]
0x2FE650:  jalr $at
```

Note the `& 0x7C` mask: only 5 bits of `$a1` reach the table index. So
the table addresses syscall IDs 0–31 (each at offset `id*4`). Game
behavior for higher IDs depends on the CUSTOM kernel overlays — not this
fixed table.

Observed populations (from SetSyscall trace):

| ID | Handler | Notes |
|---|---|---|
| 0x54 | 0x2FE440 | Exception-return / ClearEventFlag handler |
| 0x83 | 0x2FE708 | FindAddress (memory scan) |
| 0x5A | 0x2F68B0, 0x2FE820, 0x2FDEF0 (swapped) | Memcpy variants |
| 0x5B | 0x80074000, 0x80075000, 0x80076000 (swapped) | Custom kernel overlays |
| 0x55-0x59, 0xFC-0xFF, 0x12C, 0x08 | 0x01F20000 | "No-op sentinel" — runtime detects and short-circuits |

## Custom kernel overlays (0x80074000/5000/6000)

See `config/memory_map.toml` for the destination-VA mapping. The recompiler
now compiles these at their dest VAs using the `[[overlays]]` feature —
internal `jal` targets resolve correctly. Functions named
`overlay_kernel_74` / `_75` / `_76` in `src/generated/`.

The 0x80074000 overlay is itself a DISPATCHER: its table at +0x780 maps
`$a0` (caller's first arg) to a sub-handler inside the same overlay. See
the analysis in session 4's project_boot_progress.md for the decoded
dispatch logic.

## Invariants / gotchas

- `game_state_fn` has exactly **one writer path** in the entire code:
  `setGameState`. Nothing else writes to `0x384670`. This is why, in our
  current runtime, boot sits at `0x251B10` indefinitely: `setGameState`
  fires once at boot (our main.cpp hack), and no subsequent caller exists
  in the ELF-recompiled code to advance state. The advance must come
  from the overlays.

- `game_state_fn` is read by the frame dispatcher ONLY. Any state
  transition logic lives inside the frame function itself (which on
  real hardware calls `setGameState(next_state)` when its work is done).

- The module callback block pointer lives at `*(0x385334)`. Our
  bootstrap allocates this at 0x44E000 if it's 0 at boot, then populates
  slot 6 with `setGameState`.

- The three kernel overlays are COPIED during boot — their destinations
  are initially zero. With the new `[[overlays]]` recompilation, we
  have valid code for these regions, but we still don't EXECUTE it
  during boot (the runtime needs `runtime.registerFunction(0x80074000,
  overlay_kernel_74_0x80074000)` etc.) — see "what's left" below.

## What's still needed to unblock visual progress

1. **Register overlay functions in the runtime.** After the recompiler
   now produces `overlay_kernel_74_0x80074000` etc., we need to ensure
   `lookupFunction(0x80074000)` returns that function pointer.
   `generateFunctionRegistration` already emits the normal
   `runtime.registerFunction(0x<start>, fn)` line for them (the start
   IS the dest_va), so this should work automatically — verify after
   build completes.

2. **Route the kseg0-dispatch short-circuit back through the overlay
   function when available.** Currently in `ps2_syscalls_system.inl`
   we short-circuit handlers in 0x80070000-0x80080000 with `return 0`
   because the code wasn't runnable. With the overlays now
   registered, remove that short-circuit and let the normal syscall
   override path dispatch to the overlay.

3. **Trace what `setGameState` call the overlays emit.** Once (1)+(2)
   are in place, boot should reach a syscall 0x5B that `jalr`s into
   `overlay_kernel_74`. The overlay will eventually call
   `setGameState(real_next_state)` — at that point `game_state_fn`
   updates and the frame dispatch starts running real game code.

Items (1)–(2) are a single-session follow-up; (3) is whatever progress
the game makes once those are in place.
