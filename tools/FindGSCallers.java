// Trace callers of GS init chain
// @category Analysis
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.*;
import java.util.*;

public class FindGSCallers extends GhidraScript {
    @Override
    public void run() throws Exception {
        // Trace from 0x251B10 upward
        println("=== Reverse trace from 0x251B10 (calls GS init 0x258E70) ===");
        Set<Long> visited = new HashSet<>();
        trace(toAddr(0x251B10), visited, 0, 5);
        
        // Check what the callerless GS functions call
        println("\n=== Callerless GS functions and their callees ===");
        long[] callerless = {0x2580F0, 0x2582B0, 0x258480, 0x2586C0, 0x258900, 0x258C70, 0x2591D0, 0x259230, 0x259380, 0x259BD0, 0x259E00};
        for (long addr : callerless) {
            Function fn = getFunctionAt(toAddr(addr));
            if (fn == null) continue;
            StringBuilder sb = new StringBuilder();
            for (Function callee : fn.getCalledFunctions(null)) {
                sb.append(callee.getName()).append(" ");
            }
            println("  " + fn.getName() + " @ 0x" + Long.toHexString(addr) + " calls: " + sb.toString().trim());
        }
        
        println("\n=== Done ===");
    }
    
    private void trace(ghidra.program.model.address.Address addr, Set<Long> visited, int depth, int maxDepth) {
        if (depth > maxDepth) return;
        long a = addr.getOffset();
        if (visited.contains(a)) return;
        visited.add(a);
        Function fn = getFunctionAt(addr);
        if (fn == null) fn = getFunctionContaining(addr);
        if (fn == null) return;
        String indent = "  ".repeat(depth);
        for (Function caller : fn.getCallingFunctions(null)) {
            boolean hasCaller = false;
            for (Function cc : caller.getCallingFunctions(null)) { hasCaller = true; break; }
            println(indent + "<- " + caller.getName() + " @ " + caller.getEntryPoint() + (hasCaller ? "" : " [CALLERLESS]"));
            trace(caller.getEntryPoint(), visited, depth + 1, maxDepth);
        }
        if (fn.getCallingFunctions(null).isEmpty()) {
            println(indent + "(no callers)");
        }
    }
}
