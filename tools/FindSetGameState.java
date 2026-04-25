// Ghidra headless script: Find all references to setGameState (0x2FDDF8)
// and what writes to the game state pointer at 0x384670.
// @category Analysis

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.*;
import ghidra.program.model.listing.*;
import ghidra.program.model.symbol.*;

public class FindSetGameState extends GhidraScript {

    @Override
    public void run() throws Exception {
        Address setGameStateAddr = toAddr(0x2FDDF8);
        Address statePointerAddr = toAddr(0x384670);

        println("=== FindSetGameState Analysis ===");

        // Ensure function exists at 0x2FDDF8
        Function fn = getFunctionAt(setGameStateAddr);
        if (fn == null) {
            println("Creating function at 0x2FDDF8...");
            createFunction(setGameStateAddr, "setGameState");
            fn = getFunctionAt(setGameStateAddr);
        }
        if (fn != null) {
            println("Function: " + fn.getName() + " @ " + fn.getEntryPoint());
        }

        // Find all references TO 0x2FDDF8
        println("\n=== References to setGameState (0x2FDDF8) ===");
        ReferenceManager refMgr = currentProgram.getReferenceManager();
        ReferenceIterator refIter = refMgr.getReferencesTo(setGameStateAddr);
        int refCount = 0;
        while (refIter.hasNext()) {
            Reference ref = refIter.next();
            Address fromAddr = ref.getFromAddress();
            Function caller = getFunctionContaining(fromAddr);
            String callerName = caller != null ?
                caller.getName() + " @ " + caller.getEntryPoint() : "unknown";
            println("  From " + fromAddr + " [" + ref.getReferenceType() + "] in " + callerName);
            refCount++;
        }
        println("Total refs to 0x2FDDF8: " + refCount);

        // Find all references TO 0x384670 (the state pointer itself)
        println("\n=== References to state pointer (0x384670) ===");
        ReferenceIterator stateIter = refMgr.getReferencesTo(statePointerAddr);
        int stateRefCount = 0;
        while (stateIter.hasNext()) {
            Reference ref = stateIter.next();
            Address fromAddr = ref.getFromAddress();
            Function caller = getFunctionContaining(fromAddr);
            String callerName = caller != null ?
                caller.getName() + " @ " + caller.getEntryPoint() : "unknown";
            Instruction instr = getInstructionAt(fromAddr);
            String instrStr = instr != null ? instr.toString() : "?";
            println("  From " + fromAddr + " [" + ref.getReferenceType() + "] " +
                    instrStr + " in " + callerName);
            stateRefCount++;
        }
        println("Total refs to 0x384670: " + stateRefCount);

        // Search for 0x2FDDF8 as a 32-bit data value in the ELF
        println("\n=== Data references containing 0x002FDDF8 ===");
        byte[] pattern = new byte[] {
            (byte)0xF8, (byte)0xDD, (byte)0x2F, (byte)0x00
        };
        Address searchStart = toAddr(0x100000);
        Address searchEnd = toAddr(0x44F600);
        Address found = find(searchStart, pattern);
        while (found != null && found.compareTo(searchEnd) < 0) {
            println("  Data value at " + found);
            found = find(found.add(1), pattern);
        }

        // Check callers via the function's calling functions
        println("\n=== Callers of setGameState ===");
        if (fn != null) {
            for (Function caller : fn.getCallingFunctions(null)) {
                println("  " + caller.getName() + " @ " + caller.getEntryPoint());
            }
        }

        // Also check the functions that CALL the callers (2 levels deep)
        println("\n=== 2-level callers of setGameState ===");
        if (fn != null) {
            for (Function caller : fn.getCallingFunctions(null)) {
                for (Function grandCaller : caller.getCallingFunctions(null)) {
                    println("  " + grandCaller.getName() + " @ " + grandCaller.getEntryPoint() +
                            " -> " + caller.getName() + " @ " + caller.getEntryPoint());
                }
            }
        }

        println("\n=== Done ===");
    }
}
