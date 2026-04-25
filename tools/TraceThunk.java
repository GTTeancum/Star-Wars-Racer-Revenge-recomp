// Find callers of thunk_FUN_002fe0c0 (0x2FE980) and FUN_002fe660/FUN_002fe6a8
// @category Analysis
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.*;

public class TraceThunk extends GhidraScript {
    @Override
    public void run() throws Exception {
        // Trace thunk_FUN_002fe0c0 callers
        println("=== Callers of thunk_FUN_002fe0c0 (0x2FE980) ===");
        Function thunk = getFunctionAt(toAddr(0x2FE980));
        if (thunk != null) {
            for (Function caller : thunk.getCallingFunctions(null)) {
                println("  " + caller.getName() + " @ " + caller.getEntryPoint());
                // One more level
                for (Function gc : caller.getCallingFunctions(null)) {
                    println("    <- " + gc.getName() + " @ " + gc.getEntryPoint());
                }
            }
        }

        // What is FUN_002fe660? (29 callers!)
        println("\n=== FUN_002fe660 info ===");
        Function fn660 = getFunctionAt(toAddr(0x2FE660));
        if (fn660 != null) {
            println("Name: " + fn660.getName());
            println("Body: " + fn660.getBody());
            // First 5 callers
            int count = 0;
            for (Function caller : fn660.getCallingFunctions(null)) {
                println("  Caller: " + caller.getName() + " @ " + caller.getEntryPoint());
                if (++count >= 10) break;
            }
        }

        // What is FUN_002fe6a8?
        println("\n=== FUN_002fe6a8 info ===");
        Function fn6a8 = getFunctionAt(toAddr(0x2FE6A8));
        if (fn6a8 != null) {
            println("Name: " + fn6a8.getName());
            // First 5 callers
            int count = 0;
            for (Function caller : fn6a8.getCallingFunctions(null)) {
                println("  Caller: " + caller.getName() + " @ " + caller.getEntryPoint());
                if (++count >= 10) break;
            }
        }

        // What are the callerless functions with 0 callers?
        // They might be callback targets. List the ones in 0x2FD000-0x2FE000
        println("\n=== Callerless functions (0x2FD000-0x2FE000) — potential callbacks ===");
        FunctionIterator fIter = currentProgram.getListing().getFunctions(toAddr(0x2FD000), true);
        while (fIter.hasNext()) {
            Function fn = fIter.next();
            if (fn.getEntryPoint().getOffset() > 0x2FE000) break;
            boolean hasCaller = false;
            for (Function c : fn.getCallingFunctions(null)) { hasCaller = true; break; }
            if (!hasCaller) {
                // These are probably called via function pointers (jalr)
                // Check what they call
                StringBuilder callees = new StringBuilder();
                for (Function callee : fn.getCalledFunctions(null)) {
                    callees.append(callee.getName()).append(" ");
                }
                println("  " + fn.getName() + " @ " + fn.getEntryPoint() +
                        " calls: " + callees.toString().trim());
            }
        }

        println("\n=== Done ===");
    }
}
