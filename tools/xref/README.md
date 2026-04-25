# xref — global-variable cross-reference

A pair of Python tools for answering "who reads/writes this address?" without
multi-hour grep sessions.

## Quick start

```sh
# Build the database (walks the ELF, ~5s):
python tools/xref/build_xref.py \
    --elf SLUS_202.68 \
    --functions src/generated \
    --output build/xref/globals.json

# Look up an address:
python tools/xref/query.py 0x384670

# Every access in a range:
python tools/xref/query.py --range 0x443800 0x443900

# Rank writers in a range:
python tools/xref/query.py --writers-in 0x384000 0x385000

# All accesses from a specific function:
python tools/xref/query.py --function entry_13fda0_0x140230

# Hottest addresses in the project:
python tools/xref/query.py --top-readers 20
python tools/xref/query.py --top-writers 20
```

## What it does

`build_xref.py` reads the PS2 ELF's code segment, disassembles every MIPS
instruction inside each analyzer-discovered function, and performs simple
constant-folding over register values. When a load or store's base register
has a known value, the absolute effective address is recorded.

Output: `build/xref/globals.json`, keyed by address, with:
- `writers`: list of (function, pc, op, width) that stores to the address
- `readers`: same for loads
- `first_writer_function`: earliest writer by address sort (rough init hint)

## How the analysis works (and what it misses)

**Tracked:** `lui`, `addiu` / `daddiu` / `addi` / `daddi`, `ori`, `andi`,
`xori`, `addu` / `daddu` (when both operands known), `or` (same). This catches
the overwhelmingly common `lui $x, HI; addiu $x, $x, LO` pattern plus the
`lui $x, HI; addu $x, $x, $idx` struct-field pattern *if* both sides resolve.

**Invalidated:** any other instruction writing to a register, plus JAL (which
clobbers caller-saved registers) and JR/JALR (which we treat as a barrier for
all registers — pessimistic).

**Not followed:** branches. The tool walks instructions linearly, not along
control-flow edges. If register state diverges between branch paths, the tool
sees only the fallthrough view. This is conservative — it may miss some
writes/reads but never fabricates them.

**Not tracked:** COP1 (FPU), COP2 (VU), MMI register files. Loads/stores via
`lwc1`/`swc1`/`lqc2`/`sqc2` ARE recorded if the integer base register is
known, but instructions that set FPU registers don't propagate for later
constant folding.

**Not resolved:** addresses that require following jump tables, pointer
loads, or multi-instruction index arithmetic more complex than a single
`addu` of two known values. Practical consequence: dispatch-table accesses
like `lw $x, 0x4678($at+$idx)` won't resolve to the individual slots.

## Validating results

Known-correct checks to sanity-check the database:

| Address | Expected fact | Expected finding |
|---|---|---|
| `0x384670` | Current game-state function ptr | 1-2 writers (setGameState), 1 reader (frame dispatch @ 0x2FE404) |
| `0x443870` | GS state pointer | 1 writer (the GS allocator), many readers |
| `0x442B70` | GS init flag | Writer inside entry_251a20 (the GS init) |

If any of these disagree with the xref output, `build_xref.py` has a bug —
look at the constant-folding in `analyze_function`.

## Known follow-ups

- Teach the folder to handle `sll+addu` index-scaling patterns (common for
  array-of-word indexing).
- Basic-block aware analysis so branch-conditional writes are attributed to
  the correct path.
- Integrate with the eventual memory-map file so query output shows symbolic
  names (`game_state_fn`) instead of raw addresses.
