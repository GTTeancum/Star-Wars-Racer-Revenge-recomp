// Find all jalr instructions in the kernel area that could call setGameState
// and trace where the target register was loaded from
// @category Analysis
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.*;
import ghidra.program.model.address.*;

public class FindJalrTargets extends GhidraScript {
    @Override
    public void run() throws Exception {
        // Search for jalr instructions in the 0x2F0000-0x310000 range
        // that could call 0x2FDDF8
        println("=== jalr instructions in kernel area ===");
        Listing listing = currentProgram.getListing();
        InstructionIterator iter = listing.getInstructions(toAddr(0x2F5000), true);
        int count = 0;
        while (iter.hasNext()) {
            Instruction instr = iter.next();
            if (instr.getAddress().getOffset() > 0x310000) break;
            String mnemonic = instr.getMnemonicString();
            if (mnemonic != null && mnemonic.equals("jalr")) {
                Function fn = getFunctionContaining(instr.getAddress());
                String fnName = fn != null ? fn.getName() + "@" + fn.getEntryPoint() : "?";
                // Check if this function has callers
                boolean hasCaller = false;
                if (fn != null) {
                    for (Function c : fn.getCallingFunctions(null)) {
                        hasCaller = true;
                        break;
                    }
                }
                if (!hasCaller) {
                    println("  " + instr.getAddress() + ": " + instr +
                            " in " + fnName + " [NO CALLERS]");
                    count++;
                }
            }
        }
        println("Total jalr in callerless functions: " + count);

        // The key insight: look at data structures that store function pointers
        // Check the SIF RPC client data structures for stored callbacks
        println("\n=== SIF Module function pointers ===");
        // Check sceSifLoadModule calls and what happens after them
        Function sifLoad = getFunctionAt(toAddr(0x2F5A90)); // near SIF area
        if (sifLoad != null) {
            println("Function at 0x2F5A90: " + sifLoad.getName());
            for (Function caller : sifLoad.getCallingFunctions(null)) {
                println("  Called by: " + caller.getName() + "@" + caller.getEntryPoint());
            }
        }

        // Check functions near the callerless callbacks that have jalr
        // These jalr instructions are the ones that call 0x2FDDF8
        println("\n=== jalr in functions that call callbacks ===");
        long[] fnsNearCallbacks = {
            0x2FCDE8, 0x2FCEE8, 0x2FCFB0, 0x2FD5C0, 0x2FD828,
            0x2F8D30, 0x2F8440, 0x2F5E70, 0x2F5F60, 0x2F5FB0,
            0x2F6020, 0x2F7D48, 0x2F84F0, 0x2F8690
        };
        for (long addr : fnsNearCallbacks) {
            Function fn = getFunctionAt(toAddr(addr));
            if (fn == null) continue;
            // Check if this function has jalr instructions
            InstructionIterator fnIter = listing.getInstructions(fn.getBody(), true);
            while (fnIter.hasNext()) {
                Instruction i = fnIter.next();
                if (i.getMnemonicString() != null && i.getMnemonicString().equals("jalr")) {
                    println("  " + fn.getName() + "@" + fn.getEntryPoint() +
                            " has jalr at " + i.getAddress() + ": " + i);
                }
            }
        }

        println("\n=== Done ===");
    }
}
