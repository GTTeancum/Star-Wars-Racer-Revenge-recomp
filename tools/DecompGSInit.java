// @category Analysis
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.*;
import ghidra.program.model.listing.*;
public class DecompGSInit extends GhidraScript {
    @Override
    public void run() throws Exception {
        DecompInterface decomp = new DecompInterface();
        decomp.openProgram(currentProgram);
        // 0x251B10 is inside a larger function. Find the containing function.
        Function fn = getFunctionContaining(toAddr(0x251B10));
        if (fn == null) fn = getFunctionAt(toAddr(0x251A20));
        if (fn != null) {
            println("=== " + fn.getName() + " @ " + fn.getEntryPoint() + " ===");
            DecompileResults r = decomp.decompileFunction(fn, 120, null);
            if (r != null && r.decompileCompleted()) {
                ClangTokenGroup t = r.getCCodeMarkup();
                if (t != null) {
                    String c = t.toString();
                    // Print first 3000 chars
                    if (c.length() > 3000) c = c.substring(0, 3000) + "\n... (truncated)";
                    println(c);
                }
            }
        }
        // Also decompile 0x258E70 (the actual GS register writer)
        fn = getFunctionAt(toAddr(0x258E70));
        if (fn != null) {
            println("\n=== " + fn.getName() + " @ " + fn.getEntryPoint() + " ===");
            DecompileResults r = decomp.decompileFunction(fn, 120, null);
            if (r != null && r.decompileCompleted()) {
                ClangTokenGroup t = r.getCCodeMarkup();
                if (t != null) {
                    String c = t.toString();
                    if (c.length() > 3000) c = c.substring(0, 3000) + "\n... (truncated)";
                    println(c);
                }
            }
        }
        decomp.dispose();
    }
}
