// Ghidra script: Find the game's init chain, string refs, and call hierarchy
// @category Analysis

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.*;
import ghidra.program.model.listing.*;
import ghidra.program.model.symbol.*;
import ghidra.program.model.mem.*;
import java.util.*;

public class TraceGameInit extends GhidraScript {

    @Override
    public void run() throws Exception {
        println("=== TraceGameInit ===");

        // Find string references to key filenames
        String[] searchStrings = {"PANICSYS", "APPSTART", "SIO2MAN", "PADMAN"};
        for (String s : searchStrings) {
            println("\n--- Searching for string: " + s + " ---");
            Address found = find(toAddr(0x100000), s.getBytes("ASCII"));
            while (found != null && found.getOffset() < 0x44F600) {
                println("  Found at " + found);
                // Check xrefs to this address
                for (Reference ref : getReferencesTo(found)) {
                    Function fn = getFunctionContaining(ref.getFromAddress());
                    String fnName = fn != null ? fn.getName() + " @ " + fn.getEntryPoint() : "?";
                    println("    Ref from " + ref.getFromAddress() + " in " + fnName);
                }
                found = find(found.add(1), s.getBytes("ASCII"));
            }
        }

        // Trace the call chain for the asset loader at 0x270550
        println("\n--- Call chain for 0x270550 ---");
        traceCallers(toAddr(0x270550), 0);

        // List functions in the kernel area
        println("\n--- Kernel functions (0x2FD000-0x300000) ---");
        FunctionIterator fIter = currentProgram.getListing().getFunctions(toAddr(0x2FD000), true);
        while (fIter.hasNext()) {
            Function fn = fIter.next();
            if (fn.getEntryPoint().getOffset() > 0x300000) break;
            int callerCount = 0;
            for (Function c : fn.getCallingFunctions(null)) callerCount++;
            println("  " + fn.getName() + " @ " + fn.getEntryPoint() + " (callers=" + callerCount + ")");
        }

        println("\n=== Done ===");
    }

    private void traceCallers(Address addr, int depth) {
        if (depth > 3) return;
        String indent = "  ".repeat(depth);
        Function fn = getFunctionAt(addr);
        if (fn == null) fn = getFunctionContaining(addr);
        if (fn == null) {
            println(indent + "No function at " + addr);
            return;
        }
        println(indent + fn.getName() + " @ " + fn.getEntryPoint());
        for (Function caller : fn.getCallingFunctions(null)) {
            println(indent + "  <- " + caller.getName() + " @ " + caller.getEntryPoint());
            traceCallers(caller.getEntryPoint(), depth + 1);
        }
    }
}
