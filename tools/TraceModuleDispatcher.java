// Trace callers of 0x308A48 and find the path from thread 2 command dispatch
// @category Analysis
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.*;
import java.util.*;

public class TraceModuleDispatcher extends GhidraScript {
    @Override
    public void run() throws Exception {
        // 0x308A48 = module callback dispatcher (jalr to callbacks)
        // 0x2FDDF8 = setGameState  
        // Thread 2 command handlers call SetEventFlag(0x2F5B10) or WakeupThread(0x2F5A50)
        
        println("=== Reverse trace from 0x308A48 (module callback dispatcher) ===");
        Function mcd = getFunctionAt(toAddr(0x308A48));
        if (mcd != null) {
            println("Function: " + mcd.getName() + " @ " + mcd.getEntryPoint());
            Set<Long> visited = new HashSet<>();
            reverseTrace(mcd, visited, 0, 5);
        }
        
        // Also check: what functions call 0x308A48 through the thunk at 0x2FE980?
        println("\n=== Functions calling thunk_FUN_002fe0c0 (0x2FE980) ===");
        Function thunk = getFunctionAt(toAddr(0x2FE980));
        if (thunk != null) {
            Set<Long> visited = new HashSet<>();
            reverseTrace(thunk, visited, 0, 4);
        }
        
        // Check what calls 0x2FEA30 (which calls the thunk)
        println("\n=== Callers of FUN_002fea30 ===");
        Function fea30 = getFunctionAt(toAddr(0x2FEA30));
        if (fea30 != null) {
            for (Function caller : fea30.getCallingFunctions(null)) {
                println("  " + caller.getName() + " @ " + caller.getEntryPoint());
                // Check if any of these are called from thread 2 dispatch area
                for (Function gc : caller.getCallingFunctions(null)) {
                    println("    <- " + gc.getName() + " @ " + gc.getEntryPoint());
                }
            }
        }
        
        // Key: find functions that both (a) are callerless and (b) call 0x308A48
        println("\n=== Callerless functions that call module dispatcher chain ===");
        long[] dispatchers = {0x308A48, 0x308958, 0x2FEA30};
        for (long addr : dispatchers) {
            Function fn = getFunctionAt(toAddr(addr));
            if (fn == null) continue;
            for (Function caller : fn.getCallingFunctions(null)) {
                boolean hasCaller = false;
                for (Function cc : caller.getCallingFunctions(null)) { hasCaller = true; break; }
                if (!hasCaller) {
                    println("  CALLERLESS: " + caller.getName() + " @ " + caller.getEntryPoint() + 
                            " calls " + fn.getName());
                }
            }
        }
        
        // Check what WakeupThread would wake — find functions that call SleepThread(0x2F57D0)
        // and are near the module dispatcher
        println("\n=== Functions calling SleepThread near module area ===");
        Function sleepThread = getFunctionAt(toAddr(0x2F57D0));
        if (sleepThread != null) {
            for (Function caller : sleepThread.getCallingFunctions(null)) {
                long cAddr = caller.getEntryPoint().getOffset();
                if (cAddr >= 0x2F0000 && cAddr <= 0x310000) {
                    println("  " + caller.getName() + " @ " + caller.getEntryPoint());
                    // Does this function also call 0x308A48 or related?
                    for (Function callee : caller.getCalledFunctions(null)) {
                        long calleeAddr = callee.getEntryPoint().getOffset();
                        if (calleeAddr == 0x308A48 || calleeAddr == 0x308958 || 
                            calleeAddr == 0x2FEA30 || calleeAddr == 0x2FDDF8) {
                            println("    CALLS: " + callee.getName() + " @ " + callee.getEntryPoint());
                        }
                    }
                }
            }
        }
        
        println("\n=== Done ===");
    }
    
    private void reverseTrace(Function fn, Set<Long> visited, int depth, int maxDepth) {
        if (depth > maxDepth) return;
        long addr = fn.getEntryPoint().getOffset();
        if (visited.contains(addr)) return;
        visited.add(addr);
        
        String indent = "  ".repeat(depth);
        boolean hasCaller = false;
        for (Function caller : fn.getCallingFunctions(null)) {
            hasCaller = true;
            boolean callerHasCaller = false;
            for (Function cc : caller.getCallingFunctions(null)) { callerHasCaller = true; break; }
            println(indent + "<- " + caller.getName() + " @ " + caller.getEntryPoint() +
                    (callerHasCaller ? "" : " [CALLERLESS]"));
            reverseTrace(caller, visited, depth + 1, maxDepth);
        }
        if (!hasCaller) {
            println(indent + "(no callers — reached via function pointer)");
        }
    }
}
