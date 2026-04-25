// Analyze the callerless callback functions and find which one calls setGameState
// @category Analysis
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.*;
import ghidra.program.model.address.*;

public class AnalyzeCallbacks extends GhidraScript {
    @Override
    public void run() throws Exception {
        // The callerless functions (IOP callbacks)
        long[] callbacks = {
            0x2FD1B8, 0x2FD3C0, 0x2FD450, 0x2FD4F0,
            0x2FD580, 0x2FD5A0, 0x2FD7E8, 0x2FD808,
            0x2FD930, 0x2FD978, 0x2FDA68, 0x2FDB48,
            0x2FDC88, 0x2FDCB0, 0x2FDCE8, 0x2FDDF8
        };

        // For each callback, find what functions it calls (direct callees)
        // and check if any of them eventually reach setGameState
        println("=== Callback Function Analysis ===");
        for (long addr : callbacks) {
            Function fn = getFunctionAt(toAddr(addr));
            if (fn == null) continue;

            StringBuilder callees = new StringBuilder();
            boolean callsSetGameState = false;
            for (Function callee : fn.getCalledFunctions(null)) {
                callees.append(callee.getName()).append("@").append(callee.getEntryPoint()).append(" ");
                if (callee.getEntryPoint().getOffset() == 0x2FDDF8) {
                    callsSetGameState = true;
                }
                // Check 2nd level
                for (Function callee2 : callee.getCalledFunctions(null)) {
                    if (callee2.getEntryPoint().getOffset() == 0x2FDDF8) {
                        callsSetGameState = true;
                    }
                }
            }
            println(fn.getName() + " @ 0x" + Long.toHexString(addr) +
                    (callsSetGameState ? " *** CALLS setGameState ***" : "") +
                    " calls: " + callees.toString().trim());
        }

        // Also check the game state candidates (0x302xxx)
        println("\n=== Game state function candidates (0x302D00-0x305000) ===");
        FunctionIterator fIter = currentProgram.getListing().getFunctions(toAddr(0x302D00), true);
        while (fIter.hasNext()) {
            Function fn = fIter.next();
            if (fn.getEntryPoint().getOffset() > 0x305000) break;
            int callerCount = 0;
            for (Function c : fn.getCallingFunctions(null)) callerCount++;
            boolean callsSetGameState = false;
            StringBuilder callees = new StringBuilder();
            for (Function callee : fn.getCalledFunctions(null)) {
                if (callee.getEntryPoint().getOffset() == 0x2FDDF8) {
                    callsSetGameState = true;
                }
                callees.append(callee.getName()).append(" ");
            }
            if (callsSetGameState || callerCount == 0) {
                println("  " + fn.getName() + " @ " + fn.getEntryPoint() +
                        " callers=" + callerCount +
                        (callsSetGameState ? " *** CALLS setGameState ***" : "") +
                        " calls: " + callees.toString().trim());
            }
        }

        println("\n=== Done ===");
    }
}
