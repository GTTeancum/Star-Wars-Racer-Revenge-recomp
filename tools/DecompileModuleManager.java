// Decompile module manager functions
// @category Analysis
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.*;
import ghidra.program.model.listing.*;

public class DecompileModuleManager extends GhidraScript {
    @Override
    public void run() throws Exception {
        DecompInterface decomp = new DecompInterface();
        decomp.openProgram(currentProgram);
        decomp.setSimplificationStyle("decompile");

        long[] targets = {
            0x308858,  // Module state block allocator
            0x308958,  // Module status checker
            0x308A48,  // Module callback dispatcher
            0x308B00,  // Module manager entry
        };

        for (long addr : targets) {
            Function fn = getFunctionAt(toAddr(addr));
            if (fn == null) {
                println("No function at 0x" + Long.toHexString(addr));
                continue;
            }
            println("\n=== " + fn.getName() + " @ 0x" + Long.toHexString(addr) + " ===");

            DecompileResults results = decomp.decompileFunction(fn, 60, null);
            if (results != null && results.decompileCompleted()) {
                ClangTokenGroup tokens = results.getCCodeMarkup();
                if (tokens != null) {
                    println(tokens.toString());
                } else {
                    DecompiledFunction df = results.getDecompiledFunction();
                    if (df != null) {
                        String c = df.getC();
                        if (c != null && !c.isEmpty()) {
                            println(c);
                        } else {
                            println("(decompiled but no C output)");
                        }
                    } else {
                        println("(no decompiled function)");
                    }
                }
            } else {
                String err = results != null ? results.getErrorMessage() : "null results";
                println("Decompilation failed: " + err);
            }
        }

        decomp.dispose();
    }
}
